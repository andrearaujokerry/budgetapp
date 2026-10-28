"""The user-owned state of a transaction and how it moves between rows (ARCHITECTURE §6).

User state = the category when ``category_source = 'user'``, ``notes``, ``tags`` and splits.
Every path that moves edits — pending → posted, churn, tombstones, CSV pairing, attaching a manual
account — uses :func:`capture` and :func:`apply` (and :func:`merge` when two sources meet), so
no path can forget a field.

When state lands on a row with a different amount, splits are rescaled proportionally (leftover
cents on the largest split) and the row is flagged ``needs_review``. The same happens when Plaid
modifies the amount of a split row (:func:`fit_splits_to_amount`).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from budgetapp import clock
from budgetapp.models import Transaction, TransactionSplit
from budgetapp.money import CENT, format_money, to_money

JSON_VERSION = 1


@dataclass(frozen=True, slots=True)
class SplitState:
    """One split part: category, exact amount, optional note."""

    category_id: int
    amount: Decimal
    note: str | None = None


@dataclass(frozen=True, slots=True)
class UserState:
    """Everything the user owns on a transaction.

    ``category_id`` is set only when the user chose the category. ``source_amount`` is the amount
    of the row the state was captured from (the splits sum to it).
    """

    category_id: int | None = None
    notes: str | None = None
    tags: tuple[str, ...] = ()
    splits: tuple[SplitState, ...] = ()
    source_amount: Decimal | None = None
    user_edited_at: dt.datetime | None = None

    @property
    def is_empty(self) -> bool:
        return self.category_id is None and not self.notes and not self.tags and not self.splits

    @property
    def has_splits(self) -> bool:
        return bool(self.splits)


# --------------------------------------------------------------------------- tags


def normalize_tags(*groups: Any) -> list[str]:
    """Trim, drop empties, de-duplicate case-insensitively; keep the first spelling and order."""
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for tag in group or ():
            cleaned = " ".join(str(tag).split())
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                out.append(cleaned)
    return out


# --------------------------------------------------------------------------- splits


def _largest_index(splits: tuple[SplitState, ...] | list[SplitState]) -> int:
    best = 0
    for i, split in enumerate(splits):
        if abs(split.amount) > abs(splits[best].amount):
            best = i
    return best


def rescale_splits(splits: tuple[SplitState, ...], new_total: Decimal) -> tuple[SplitState, ...]:
    """Scale ``splits`` proportionally so they sum exactly to ``new_total``.

    Each part except the largest is rounded half-up to the cent; the largest takes the leftover
    cents. Parts that round to zero are dropped. Returns ``()`` when scaling is impossible (the
    parts or the new total are zero).
    """
    new_total = to_money(new_total)
    if not splits:
        return ()
    old_total = sum((to_money(s.amount) for s in splits), Decimal("0"))
    if old_total == new_total:
        return tuple(splits)
    if old_total == 0 or new_total == 0:
        return ()
    largest = _largest_index(splits)
    amounts: list[Decimal | None] = []
    for i, split in enumerate(splits):
        if i == largest:
            amounts.append(None)
        else:
            scaled = (to_money(split.amount) * new_total / old_total).quantize(
                CENT, rounding=ROUND_HALF_UP
            )
            amounts.append(scaled)
    amounts[largest] = new_total - sum((a for a in amounts if a is not None), Decimal("0"))
    result = tuple(
        SplitState(s.category_id, to_money(a), s.note)
        for s, a in zip(splits, amounts, strict=True)
        if a is not None and a != 0
    )
    assert sum((s.amount for s in result), Decimal("0")) == new_total
    return result


def _set_splits(txn: Transaction, splits: tuple[SplitState, ...]) -> None:
    txn.splits.clear()
    for split in splits:
        txn.splits.append(
            TransactionSplit(category_id=split.category_id, amount=split.amount, note=split.note)
        )


def _flag(txn: Transaction, reason: str) -> None:
    txn.needs_review = True
    txn.review_reason = reason


def _collapse(txn: Transaction, splits: tuple[SplitState, ...], reason: str) -> None:
    """Replace splits by the largest part's category (a user choice) and flag the row."""
    largest = splits[_largest_index(splits)]
    _set_splits(txn, ())
    txn.category_id = largest.category_id
    txn.category_source = "user"
    txn.rule_id = None
    _flag(txn, reason)


def _land_splits(txn: Transaction, splits: tuple[SplitState, ...]) -> bool:
    """Put ``splits`` on ``txn``, rescaling or collapsing as needed; True if the row was flagged."""
    amount = to_money(txn.amount)
    total = sum((s.amount for s in splits), Decimal("0"))
    if txn.pending:
        _collapse(
            txn,
            splits,
            "This charge was split, but a pending charge cannot be split: it was put in the "
            "largest split's category. Re-split it once it posts.",
        )
        return True
    if total == amount:
        _set_splits(txn, splits)
        return False
    rescaled = rescale_splits(splits, amount)
    if len(rescaled) >= 2:
        _set_splits(txn, rescaled)
        _flag(
            txn,
            f"The amount changed from {format_money(total)} to {format_money(amount)}, so the "
            "splits were rescaled proportionally. Please check them.",
        )
        return True
    _collapse(
        txn,
        splits,
        f"The amount changed from {format_money(total)} to {format_money(amount)} and the splits "
        "could not be rescaled, so the charge was put in the largest split's category.",
    )
    return True


def fit_splits_to_amount(txn: Transaction) -> bool:
    """After Plaid changed a split row's amount: rescale its splits to the new amount.

    Returns ``True`` when the splits were changed (and the row flagged ``needs_review``).
    """
    if not txn.splits:
        return False
    current = tuple(SplitState(s.category_id, to_money(s.amount), s.note) for s in txn.splits)
    if sum((s.amount for s in current), Decimal("0")) == to_money(txn.amount) and not txn.pending:
        return False
    return _land_splits(txn, current)


# ------------------------------------------------------------------------ capture / apply / merge


def capture(txn: Transaction) -> UserState | None:
    """The user-owned state of ``txn``, or ``None`` when the user never edited it."""
    splits = tuple(SplitState(s.category_id, to_money(s.amount), s.note) for s in txn.splits)
    notes = txn.notes if txn.notes and txn.notes.strip() else None
    state = UserState(
        category_id=txn.category_id if txn.category_source == "user" else None,
        notes=notes,
        tags=tuple(normalize_tags(txn.tags)),
        splits=splits,
        source_amount=to_money(txn.amount),
        user_edited_at=txn.user_edited_at,
    )
    return None if state.is_empty else state


def apply(txn: Transaction, state: UserState | None, *, include_category: bool = True) -> bool:
    """Move ``state`` onto ``txn``. Returns ``True`` when the row was flagged ``needs_review``.

    * category: set with ``category_source = 'user'`` (unless ``include_category=False``);
    * notes: set; if the target already has different notes, both are kept (carried notes first);
    * tags: union (case-insensitive de-duplication);
    * splits: replace the target's splits, rescaled when the amounts differ (see module docs). A
      pending target cannot be split: it takes the largest split's category instead.
    """
    if state is None or state.is_empty:
        return False
    if include_category and state.category_id is not None:
        txn.category_id = state.category_id
        txn.category_source = "user"
        txn.rule_id = None
    if state.notes:
        existing = (txn.notes or "").strip()
        if not existing or existing == state.notes.strip():
            txn.notes = state.notes
        else:
            txn.notes = f"{state.notes}\n\n{txn.notes}"
    if state.tags:
        txn.tags = normalize_tags(txn.tags, state.tags)
    flagged = _land_splits(txn, state.splits) if state.splits else False
    stamps = [t for t in (txn.user_edited_at, state.user_edited_at) if t is not None]
    txn.user_edited_at = max(stamps) if stamps else clock.now()
    return flagged


def merge(primary: UserState | None, secondary: UserState | None) -> UserState | None:
    """Combine two states; ``primary`` wins for the category and the splits.

    Notes are concatenated when both exist and differ (user text is never dropped); tags are
    united.
    """
    if primary is None or primary.is_empty:
        return secondary if secondary is not None and not secondary.is_empty else None
    if secondary is None or secondary.is_empty:
        return primary
    notes = primary.notes
    if secondary.notes and (not notes or notes.strip() != secondary.notes.strip()):
        notes = f"{notes}\n\n{secondary.notes}" if notes else secondary.notes
    if primary.splits:
        splits, source_amount = primary.splits, primary.source_amount
    else:
        splits, source_amount = secondary.splits, secondary.source_amount
    stamps = [t for t in (primary.user_edited_at, secondary.user_edited_at) if t is not None]
    return UserState(
        category_id=primary.category_id
        if primary.category_id is not None
        else secondary.category_id,
        notes=notes,
        tags=tuple(normalize_tags(primary.tags, secondary.tags)),
        splits=splits,
        source_amount=source_amount if source_amount is not None else primary.source_amount,
        user_edited_at=max(stamps) if stamps else None,
    )


# --------------------------------------------------------------------------- JSON (tombstones)


def to_json(state: UserState | None) -> dict[str, Any] | None:
    """Serialize for ``transaction_tombstones.user_state`` (amounts as exact strings)."""
    if state is None or state.is_empty:
        return None
    return {
        "version": JSON_VERSION,
        "category_id": state.category_id,
        "notes": state.notes,
        "tags": list(state.tags),
        "splits": [
            {"category_id": s.category_id, "amount": str(to_money(s.amount)), "note": s.note}
            for s in state.splits
        ],
        "source_amount": str(to_money(state.source_amount))
        if state.source_amount is not None
        else None,
        "user_edited_at": state.user_edited_at.isoformat() if state.user_edited_at else None,
    }


def from_json(data: dict[str, Any] | None) -> UserState | None:
    """Inverse of :func:`to_json` (``None`` for an empty or missing state)."""
    if not data:
        return None
    edited = data.get("user_edited_at")
    source_amount = data.get("source_amount")
    state = UserState(
        category_id=data.get("category_id"),
        notes=data.get("notes") or None,
        tags=tuple(normalize_tags(data.get("tags") or ())),
        splits=tuple(
            SplitState(int(s["category_id"]), to_money(str(s["amount"])), s.get("note"))
            for s in data.get("splits") or ()
        ),
        source_amount=to_money(str(source_amount)) if source_amount is not None else None,
        user_edited_at=dt.datetime.fromisoformat(edited) if edited else None,
    )
    return None if state.is_empty else state


def remap_category_ids(
    data: dict[str, Any] | None, old_id: int, new_id: int
) -> dict[str, Any] | None:
    """Re-point category ids inside a serialized state (category merges re-point tombstones too)."""
    if not data:
        return data
    out = dict(data)
    if out.get("category_id") == old_id:
        out["category_id"] = new_id
    out["splits"] = [
        {**s, "category_id": new_id if s.get("category_id") == old_id else s.get("category_id")}
        for s in out.get("splits") or ()
    ]
    return out
