import datetime as dt
import io
import logging
import os
import stat
import sys
import threading

import keyring.errors
import pytest

from budgetapp import clock, logsetup
from budgetapp import secrets as sec
from budgetapp.config import load_settings
from budgetapp.secrets import (
    LEGACY_PRODUCTION_TOKEN,
    PRODUCTION_ITEM_INDEX,
    SANDBOX_ITEM_INDEX,
    AccessTokenConflict,
    KeychainApprovalNeeded,
    KeychainStore,
    MemoryStore,
    MissingSecretError,
    SecretStoreError,
    TokenNotSavedError,
)

PROD_TOKEN = "access-production-11111111-2222-3333-4444-555555555555"
OTHER_TOKEN = "access-production-99999999-2222-3333-4444-555555555555"
SANDBOX_TOKEN = "access-sandbox-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
#: Where store_access_token puts the production token of Item "item-1" (C6: one key per Item).
ITEM_1_KEY = "plaid.access_token.production.item-1"


@pytest.fixture
def prod(tmp_path):
    return load_settings("production", state_dir=tmp_path / "state")


@pytest.fixture
def sandbox(tmp_path):
    return load_settings("sandbox", state_dir=tmp_path / "state")


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class TestStores:
    def test_memory_store_and_helpers(self, secrets):
        assert sec.get_store() is secrets
        sec.set_secret("plaid.client_id", "0123456789abcdef01234567")
        assert secrets.values["plaid.client_id"] == "0123456789abcdef01234567"
        assert sec.get_secret("missing") is None
        with pytest.raises(MissingSecretError, match="Run ./run setup"):
            sec.require_secret("missing", hint="Run ./run setup")

    def test_use_store_restores_previous(self, secrets):
        other = MemoryStore()
        with sec.use_store(other):
            assert sec.get_store() is other
        assert sec.get_store() is secrets

    def test_set_secret_refuses_empty_and_verifies_read_back(self, secrets):
        with pytest.raises(ValueError):
            sec.set_secret("x", "")
        secrets.corrupt_reads.add("db.password.budgetapp_dev")
        with pytest.raises(SecretStoreError, match="read-back"):
            sec.set_secret("db.password.budgetapp_dev", "some-long-password")

    def test_key_names(self):
        assert sec.plaid_secret_key("sandbox") == "plaid.secret.sandbox"
        assert sec.access_token_key("production", "abc") == "plaid.access_token.production.abc"
        assert sec.production_access_token_key("item-1") == ITEM_1_KEY
        assert sec.access_token_key("sandbox", "abc") == "plaid.access_token.sandbox.abc"
        assert sec.item_index_key("production") == PRODUCTION_ITEM_INDEX
        assert sec.item_index_key("sandbox") == SANDBOX_ITEM_INDEX
        for bad in ("", "a b", "tab\tid", "ünï", "x" * 257, None):
            with pytest.raises(ValueError):
                sec.access_token_key("production", bad)
        # Plaid doesn't document which characters its item ids use (security-1): any printable
        # ASCII without spaces is a name, and it stays unambiguous (the environment comes first).
        assert sec.access_token_key("production", "a.b=c") == "plaid.access_token.production.a.b=c"
        assert sec.db_password_key("budgetapp_live") == "db.password.budgetapp_live"
        with pytest.raises(ValueError):
            sec.db_password_key("postgres")
        with pytest.raises(ValueError):
            sec.sandbox_access_token_key("bad id with spaces")
        with pytest.raises(ValueError):
            sec.plaid_secret_key("development")


class TestAccessTokens:
    def test_production_token_is_written_once_and_read_back(self, prod, secrets):
        sec.store_access_token(prod, "item-1", PROD_TOKEN)
        assert secrets.values[ITEM_1_KEY] == PROD_TOKEN
        sec.store_access_token(prod, "item-1", PROD_TOKEN)  # same token: no-op
        assert sec.get_access_token("production", "item-1") == PROD_TOKEN
        assert sec.production_item_ids() == ["item-1"]
        assert LEGACY_PRODUCTION_TOKEN not in secrets.values  # the v1 name is never written

    def test_each_production_item_keeps_its_own_token(self, prod, secrets):
        """BANK-4 (C6): several production Items, one key each; no token reaches another Item."""
        sec.store_access_token(prod, "item-1", PROD_TOKEN)
        sec.store_access_token(prod, "item-2", OTHER_TOKEN)
        assert sec.get_access_token("production", "item-1") == PROD_TOKEN
        assert sec.get_access_token("production", "item-2") == OTHER_TOKEN
        assert sec.get_access_token("production", "item-3") is None
        assert sec.production_item_ids() == ["item-1", "item-2"]
        assert sec.sandbox_item_ids() == []

    def test_a_different_token_is_never_overwritten(self, prod, secrets):
        sec.store_access_token(prod, "item-1", PROD_TOKEN)
        with pytest.raises(AccessTokenConflict) as info:
            sec.store_access_token(prod, "item-1", OTHER_TOKEN)
        assert secrets.values[ITEM_1_KEY] == PROD_TOKEN
        recovery = list((prod.state_dir / "recovery").iterdir())
        assert len(recovery) == 1 and OTHER_TOKEN in recovery[0].read_text()
        assert OTHER_TOKEN not in str(info.value)

    def test_failed_write_goes_to_a_private_recovery_file(self, prod, secrets):
        secrets.fail_on[("set", ITEM_1_KEY)] = KeychainApprovalNeeded(ITEM_1_KEY, -25308)
        with pytest.raises(TokenNotSavedError) as info:
            sec.store_access_token(prod, "item-1", PROD_TOKEN)
        path = info.value.recovery_path
        assert path.exists() and path.parent == prod.state_dir / "recovery"
        assert _mode(path) == 0o600 and _mode(path.parent) == 0o700
        content = path.read_text()
        assert PROD_TOKEN in content and "item-1" in content and "--access-token" in content
        assert PROD_TOKEN not in str(info.value)
        assert ITEM_1_KEY not in secrets.values

    def test_any_unexpected_error_still_saves_the_token(self, prod, secrets):
        secrets.fail_on[("set", None)] = RuntimeError("no keyring backend")
        with pytest.raises(TokenNotSavedError) as info:
            sec.store_access_token(prod, "item-1", PROD_TOKEN)
        assert PROD_TOKEN in info.value.recovery_path.read_text()

    def test_read_back_mismatch_goes_to_the_recovery_file(self, prod, secrets):
        secrets.corrupt_reads.add(ITEM_1_KEY)
        with pytest.raises(TokenNotSavedError):
            sec.store_access_token(prod, "item-1", PROD_TOKEN)

    @pytest.mark.parametrize("item_id", ["item.with.dot", "abc=", "a/b", "x" * 256])
    def test_an_item_id_of_other_printable_characters_gets_its_own_keychain_item(
        self, prod, secrets, item_id
    ):
        """Final review (security-1): Plaid doesn't document the characters of its item ids, so
        any printable ASCII without spaces (up to 256) names a Keychain item, which setup
        --access-token and Re-attach can use too. The environment comes before the id, so the
        name can't be another environment's item, the v1 name or the index."""
        sec.store_access_token(prod, item_id, PROD_TOKEN)
        key = sec.production_access_token_key(item_id)
        assert key == f"plaid.access_token.production.{item_id}"
        assert secrets.values[key] == PROD_TOKEN and sec.production_item_ids() == [item_id]
        assert not sec.is_sandbox_account(key) and sec.unsaved_production_tokens(prod) == []

    @pytest.mark.parametrize("item_id", ["a b", "tab\tid", "ünï", "x" * 257])
    def test_an_item_id_no_keychain_name_can_hold_still_keeps_the_token(
        self, prod, secrets, item_id
    ):
        """Final review (security-1): the Keychain name is worked out inside the guard, so an
        Item id it can't hold ends in the recovery file like any other failure. The token (a
        Trial connection Plaid has already created) is never lost, and it still counts."""
        with pytest.raises(TokenNotSavedError) as info:
            sec.store_access_token(prod, item_id, PROD_TOKEN)
        path = info.value.recovery_path
        assert sec.unsaved_production_tokens(prod) == [path]
        assert PROD_TOKEN in path.read_text() and _mode(path) == 0o600
        assert PROD_TOKEN not in str(info.value)
        assert PROD_TOKEN not in secrets.values.values()
        assert sec.recovery_item_ids(prod) == {item_id}

    def test_sandbox_tokens_are_per_item_with_an_index(self, sandbox, secrets):
        sec.store_access_token(sandbox, "item-a", SANDBOX_TOKEN)
        sec.store_access_token(sandbox, "item-b", SANDBOX_TOKEN + "x")
        sec.store_access_token(sandbox, "item-a", SANDBOX_TOKEN)
        assert sec.sandbox_item_ids() == ["item-a", "item-b"]
        assert sec.get_access_token("sandbox", "item-b") == SANDBOX_TOKEN + "x"
        sec.remove_sandbox_item("item-a")
        assert sec.sandbox_item_ids() == ["item-b"]
        assert sec.get_access_token("sandbox", "item-a") is None

    def test_recovery_files_never_overwrite(self, tmp_path):
        first = sec.write_recovery_file(tmp_path, "token", "one")
        second = sec.write_recovery_file(tmp_path, "token", "two")
        assert first != second
        assert first.read_text() == "one" and second.read_text() == "two"

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(1)])
    def test_ctrl_c_during_the_keychain_write_still_saves_the_token(self, prod, secrets, interrupt):
        secrets.fail_on[("set", ITEM_1_KEY)] = interrupt
        with pytest.raises(type(interrupt)):
            sec.store_access_token(prod, "item-1", PROD_TOKEN)
        [path] = sec.unsaved_production_tokens(prod)
        assert PROD_TOKEN in path.read_text() and _mode(path) == 0o600

    def test_unwritable_state_dir_falls_back_to_the_temp_folder(
        self, prod, secrets, monkeypatch, tmp_path
    ):
        fallback = tmp_path / "fallback"
        monkeypatch.setattr(sec, "fallback_recovery_dir", lambda: fallback)
        prod.state_dir.mkdir(parents=True)
        prod.state_dir.chmod(0o500)  # read-only: the recovery folder cannot be created
        secrets.fail_on[("set", None)] = RuntimeError("disk full")
        try:
            with pytest.raises(TokenNotSavedError) as info:
                sec.store_access_token(prod, "item-1", PROD_TOKEN)
        finally:
            prod.state_dir.chmod(0o700)
        path = info.value.recovery_path
        assert path.parent == fallback / "recovery" and PROD_TOKEN in path.read_text()
        assert sec.unsaved_production_tokens(prod) == [path]

    def test_no_recovery_file_at_all_keeps_the_token_out_of_the_message(
        self, prod, secrets, monkeypatch, tmp_path
    ):
        def refuse(*args, **kwargs):
            raise PermissionError("read-only volume")

        monkeypatch.setattr(sec, "write_access_token_recovery_file", refuse)
        secrets.fail_on[("set", None)] = RuntimeError("disk full")
        with pytest.raises(TokenNotSavedError) as info:
            sec.store_access_token(prod, "item-1", PROD_TOKEN)
        assert info.value.recovery_path is None
        assert PROD_TOKEN not in str(info.value) and "only in this process" in str(info.value)

    @pytest.mark.parametrize(
        ("env", "index", "key"),
        [
            ("sandbox", SANDBOX_ITEM_INDEX, "plaid.access_token.sandbox.item-a"),
            ("production", PRODUCTION_ITEM_INDEX, "plaid.access_token.production.item-a"),
        ],
    )
    def test_the_index_is_written_before_the_token(self, tmp_path, secrets, env, index, key):
        order = []
        original = secrets.set

        def recording_set(account, value):
            order.append(account)
            original(account, value)

        secrets.set = recording_set
        settings = load_settings(env, state_dir=tmp_path / "state")
        sec.store_access_token(settings, "item-a", SANDBOX_TOKEN)
        assert order == [index, key]

    def test_failed_index_write_never_leaves_an_unfindable_token(self, sandbox, secrets):
        secrets.fail_on[("set", SANDBOX_ITEM_INDEX)] = RuntimeError("index write failed")
        with pytest.raises(TokenNotSavedError) as info:
            sec.store_access_token(sandbox, "item-a", SANDBOX_TOKEN)
        assert "plaid.access_token.sandbox.item-a" not in secrets.values
        assert SANDBOX_TOKEN in info.value.recovery_path.read_text()


class TestProductionLinkGuard:
    def test_nothing_blocks_a_first_link(self, prod):
        assert sec.production_link_blockers(prod) == []

    def test_a_live_marker_blocks_but_a_saved_token_no_longer_does(
        self, prod, secrets, freeze_today
    ):
        """BANK-1: several production Items are allowed now; one link at a time still holds."""
        start = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
        freeze_today(now=start)
        sec.set_link_in_progress()
        assert ["started" in r for r in sec.production_link_blockers(prod)] == [True]
        freeze_today(now=start + dt.timedelta(hours=4, minutes=1))
        assert sec.production_link_blockers(prod) == []
        sec.store_access_token(prod, "item-1", PROD_TOKEN)
        assert sec.production_link_blockers(prod) == []

    def test_a_token_stuck_in_a_recovery_file_blocks_after_the_marker_expires(
        self, prod, secrets, freeze_today
    ):
        start = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
        freeze_today(now=start)
        sec.set_link_in_progress()
        secrets.fail_on[("set", ITEM_1_KEY)] = RuntimeError("Keychain refused")
        with pytest.raises(TokenNotSavedError):
            sec.store_access_token(prod, "item-1", PROD_TOKEN)
        freeze_today(now=start + dt.timedelta(hours=4, minutes=1))
        assert sec.get_link_in_progress() is None
        assert sec.get_access_token("production", "item-1") is None
        [reason] = sec.production_link_blockers(prod)
        assert "recovery file" in reason and PROD_TOKEN not in reason
        assert sec.recovery_item_ids(prod) == {"item-1"}

        del secrets.fail_on[("set", ITEM_1_KEY)]
        sec.store_access_token(prod, "item-1", PROD_TOKEN)
        [saved] = sec.mark_recovery_saved(prod, PROD_TOKEN)
        assert saved.suffix == ".saved" and _mode(saved) == 0o600
        assert sec.unsaved_production_tokens(prod) == []
        assert sec.recovery_item_ids(prod) == set()
        assert sec.production_link_blockers(prod) == []

    def test_only_files_holding_that_token_are_marked_saved(self, prod, secrets):
        secrets.fail_on[("set", None)] = RuntimeError("Keychain refused")
        for item_id, token in (("item-1", PROD_TOKEN), ("item-2", OTHER_TOKEN)):
            with pytest.raises(TokenNotSavedError):
                sec.store_access_token(prod, item_id, token)
        assert sec.recovery_item_ids(prod) == {"item-1", "item-2"}
        assert len(sec.mark_recovery_saved(prod, PROD_TOKEN)) == 1
        [left] = sec.unsaved_production_tokens(prod)
        assert OTHER_TOKEN in left.read_text()

    def test_connections_plaid_removed_are_noted_in_a_private_file(self, prod):
        """Final review (ops-2): Re-attach notes a connection Plaid answered ITEM_NOT_FOUND for,
        so it no longer waits to be re-attached. A note that can't be read counts as none (the
        next Re-attach asks Plaid again)."""
        assert sec.removed_production_items(prod) == set()
        for item_id in ("item-dead", "item-gone", "item-dead"):
            sec.mark_production_item_removed(prod, item_id)
        assert sec.removed_production_items(prod) == {"item-dead", "item-gone"}
        path = prod.state_dir / sec.REMOVED_ITEMS_FILE
        assert _mode(path) == 0o600 and list(prod.state_dir.glob(".removed-items-*")) == []
        path.write_text("{not json")
        assert sec.removed_production_items(prod) == set()


class TestLegacyTokenMove:
    """C6: the v1 key is moved once to its Item's key; on any doubt it stays and keeps working."""

    def test_nothing_to_move(self, secrets):
        assert sec.move_legacy_production_token("item-1") == sec.LegacyTokenMove("none")
        assert secrets.values == {}

    def test_the_v1_token_moves_to_its_items_key_and_the_old_key_goes(self, secrets):
        secrets.values[LEGACY_PRODUCTION_TOKEN] = PROD_TOKEN
        order = []
        original_set, original_delete = secrets.set, secrets.delete
        secrets.set = lambda a, v: (order.append(("set", a)), original_set(a, v))[1]
        secrets.delete = lambda a: (order.append(("delete", a)), original_delete(a))[1]
        move = sec.move_legacy_production_token("item-1")
        assert move == sec.LegacyTokenMove("moved", "item-1")
        assert order == [
            ("set", PRODUCTION_ITEM_INDEX),
            ("set", ITEM_1_KEY),
            ("delete", LEGACY_PRODUCTION_TOKEN),
        ]
        assert secrets.values[ITEM_1_KEY] == PROD_TOKEN
        assert sec.legacy_production_token() is None
        assert sec.production_item_ids() == ["item-1"]
        assert sec.move_legacy_production_token("item-1").status == "none"  # once

    def test_a_failed_write_keeps_the_old_key(self, secrets):
        secrets.values[LEGACY_PRODUCTION_TOKEN] = PROD_TOKEN
        secrets.fail_on[("set", ITEM_1_KEY)] = KeychainApprovalNeeded(ITEM_1_KEY, -25308)
        move = sec.move_legacy_production_token("item-1")
        assert move.status == "kept" and "KeychainApprovalNeeded" in move.reason
        assert secrets.values[LEGACY_PRODUCTION_TOKEN] == PROD_TOKEN
        assert ITEM_1_KEY not in secrets.values
        assert PROD_TOKEN not in repr(move)

    def test_a_read_back_mismatch_removes_the_bad_copy_and_keeps_the_old_key(self, secrets):
        secrets.values[LEGACY_PRODUCTION_TOKEN] = PROD_TOKEN
        secrets.corrupt_reads.add(ITEM_1_KEY)
        move = sec.move_legacy_production_token("item-1")
        assert move.status == "kept" and "read back" in move.reason
        assert ITEM_1_KEY not in secrets.values  # it can't shadow the good token
        assert secrets.values[LEGACY_PRODUCTION_TOKEN] == PROD_TOKEN

    def test_a_different_token_under_the_new_key_keeps_both(self, secrets):
        secrets.values[LEGACY_PRODUCTION_TOKEN] = PROD_TOKEN
        secrets.values[ITEM_1_KEY] = OTHER_TOKEN
        move = sec.move_legacy_production_token("item-1")
        assert move.status == "kept" and "different token" in move.reason
        assert (secrets.values[LEGACY_PRODUCTION_TOKEN], secrets.values[ITEM_1_KEY]) == (
            PROD_TOKEN,
            OTHER_TOKEN,
        )

    def test_an_old_copy_that_could_not_be_deleted_goes_next_time(self, secrets):
        secrets.values[LEGACY_PRODUCTION_TOKEN] = PROD_TOKEN
        secrets.fail_on[("delete", LEGACY_PRODUCTION_TOKEN)] = RuntimeError("locked")
        move = sec.move_legacy_production_token("item-1")
        assert move.status == "moved" and "couldn't be deleted" in move.reason
        assert secrets.values[ITEM_1_KEY] == secrets.values[LEGACY_PRODUCTION_TOKEN]
        del secrets.fail_on[("delete", LEGACY_PRODUCTION_TOKEN)]
        assert sec.move_legacy_production_token("item-1") == sec.LegacyTokenMove("moved", "item-1")
        assert LEGACY_PRODUCTION_TOKEN not in secrets.values

    @pytest.mark.parametrize(
        ("db_ids", "values", "expected"),
        [
            (["item-1"], {}, ("none", None)),
            (["item-1"], {LEGACY_PRODUCTION_TOKEN: PROD_TOKEN}, ("movable", "item-1")),
            (  # the move's delete failed: a copy, however many Items exist
                ["item-1", "item-2"],
                {LEGACY_PRODUCTION_TOKEN: PROD_TOKEN, ITEM_1_KEY: PROD_TOKEN},
                ("copy", "item-1"),
            ),
            (  # a copy of a token whose Item row is lost (in the index only)
                [],
                {LEGACY_PRODUCTION_TOKEN: PROD_TOKEN, ITEM_1_KEY: PROD_TOKEN},
                ("copy", "item-1"),
            ),
            (  # the one Item has another token: the v1 one may be a lost connection's
                ["item-1"],
                {LEGACY_PRODUCTION_TOKEN: PROD_TOKEN, ITEM_1_KEY: OTHER_TOKEN},
                ("unplaced", None),
            ),
            (["item-1", "item-2"], {LEGACY_PRODUCTION_TOKEN: PROD_TOKEN}, ("unplaced", None)),
            ([], {LEGACY_PRODUCTION_TOKEN: PROD_TOKEN}, ("unplaced", None)),
        ],
        ids=["none", "movable", "copy", "copy-of-orphan", "beside-own", "several", "no-item"],
    )
    def test_where_the_v1_token_stands(self, secrets, db_ids, values, expected):
        secrets.values.update(values)
        if ITEM_1_KEY in values:
            secrets.values[PRODUCTION_ITEM_INDEX] = '["item-1"]'
        assert sec.legacy_token_state(db_ids) == sec.LegacyTokenState(*expected)

    def test_a_copy_is_deleted_only_when_it_holds_that_token(self, secrets):
        """Safety review: the retry of a failed delete (every production use of a token)."""
        secrets.values[LEGACY_PRODUCTION_TOKEN] = PROD_TOKEN
        assert sec.delete_legacy_copy("item-2", OTHER_TOKEN) == sec.LegacyTokenMove("none")
        assert secrets.values[LEGACY_PRODUCTION_TOKEN] == PROD_TOKEN
        secrets.fail_on[("delete", LEGACY_PRODUCTION_TOKEN)] = KeychainApprovalNeeded(
            LEGACY_PRODUCTION_TOKEN, -25308
        )
        move = sec.delete_legacy_copy("item-1", PROD_TOKEN)
        assert move.status == "moved" and "KeychainApprovalNeeded" in move.reason
        assert PROD_TOKEN not in repr(move)
        del secrets.fail_on[("delete", LEGACY_PRODUCTION_TOKEN)]
        assert sec.delete_legacy_copy("item-1", PROD_TOKEN) == sec.LegacyTokenMove(
            "moved", "item-1"
        )
        assert LEGACY_PRODUCTION_TOKEN not in secrets.values
        assert sec.delete_legacy_copy("item-1", PROD_TOKEN) == sec.LegacyTokenMove("none")

    def test_deleting_a_copy_never_raises(self, secrets):
        secrets.fail_on[("get", LEGACY_PRODUCTION_TOKEN)] = KeychainApprovalNeeded(
            LEGACY_PRODUCTION_TOKEN, -25308
        )
        assert sec.delete_legacy_copy("item-1", PROD_TOKEN) == sec.LegacyTokenMove("none")

    def test_approval_needed_for_the_old_key_is_not_swallowed(self, secrets):
        secrets.fail_on[("get", LEGACY_PRODUCTION_TOKEN)] = KeychainApprovalNeeded(
            LEGACY_PRODUCTION_TOKEN, -25308
        )
        with pytest.raises(KeychainApprovalNeeded):
            sec.move_legacy_production_token("item-1")

    def test_an_index_that_is_not_a_list_of_ids_reads_as_empty(self, secrets):
        for raw in ("{}", '"item-1"', "[1, null]", '["ok", "bad id"]', "not json"):
            secrets.values[PRODUCTION_ITEM_INDEX] = raw
            assert sec.production_item_ids() in ([], ["ok"])


class TestLinkMarker:
    def test_expires_after_four_hours(self, freeze_today):
        start = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
        freeze_today(now=start)
        expires = sec.set_link_in_progress()
        assert expires == start + dt.timedelta(hours=4)
        assert sec.get_link_in_progress() == expires
        freeze_today(now=start + dt.timedelta(hours=3, minutes=59))
        assert sec.get_link_in_progress() == expires
        freeze_today(now=start + dt.timedelta(hours=4, seconds=1))
        assert sec.get_link_in_progress() is None

    def test_clear(self):
        sec.set_link_in_progress()
        sec.clear_link_in_progress()
        assert sec.get_link_in_progress() is None

    def test_unreadable_marker_counts_as_present(self, secrets):
        secrets.values["plaid.link_in_progress.production"] = "not json"
        assert sec.get_link_in_progress() is not None


class FakeKeyring:
    """keyring backend API, raising like keyring's macOS backend does."""

    def __init__(self, status=None, locked=False):
        self.status = status
        self.locked = locked
        self.values = {}

    def _maybe_raise(self, verb):
        if self.locked:
            raise keyring.errors.KeyringLocked("Can't get password from keychain: (-128, 'Denied')")
        if self.status is not None:
            cause = _ApiError(self.status, "Security Auth Failure")
            try:
                raise cause
            except _ApiError as exc:
                raise keyring.errors.KeyringError(
                    f"Can't {verb} password from keychain: {exc}"
                ) from exc

    def get_password(self, service, username):
        self._maybe_raise("get")
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self._maybe_raise("store")
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.values:
            cause = _ApiError(-25300, "Item not found")
            raise keyring.errors.PasswordDeleteError("Can't delete password") from cause
        del self.values[(service, username)]


class _ApiError(Exception):
    pass


class TestKeychainErrorMapping:
    @pytest.mark.parametrize("status", [-25293, -25308])
    def test_approval_statuses(self, status):
        store = KeychainStore(backend=FakeKeyring(status=status))
        with pytest.raises(KeychainApprovalNeeded) as info:
            store.get("db.password.budgetapp_live")
        assert info.value.status == status
        assert info.value.account == "db.password.budgetapp_live"
        assert "./run doctor --env production" in str(info.value)

    def test_locked_keychain(self):
        with pytest.raises(KeychainApprovalNeeded):
            KeychainStore(backend=FakeKeyring(locked=True)).set("x", "value-123456")

    def test_other_errors_are_not_approval_errors(self):
        with pytest.raises(SecretStoreError) as info:
            KeychainStore(backend=FakeKeyring(status=-34018)).get("x")
        assert not isinstance(info.value, KeychainApprovalNeeded)

    def test_round_trip_and_delete_of_missing_item(self):
        store = KeychainStore(backend=FakeKeyring())
        store.set("plaid.client_id", "0123456789abcdef01234567")
        assert store.get("plaid.client_id") == "0123456789abcdef01234567"
        store.delete("plaid.client_id")
        store.delete("plaid.client_id")  # already gone: fine
        assert store.get("plaid.client_id") is None

    def test_tests_can_never_reach_the_real_keychain(self):
        with pytest.raises(SecretStoreError, match="real Keychain"):
            KeychainStore().get("plaid.client_id")

    @pytest.mark.parametrize(
        "account",
        [
            LEGACY_PRODUCTION_TOKEN,
            "plaid.access_token.production.item-1",
            PRODUCTION_ITEM_INDEX,
            "plaid.secret.production",
            "db.password.budgetapp_live",
        ],
    )
    def test_live_sandbox_tests_cannot_touch_production_items(self, monkeypatch, account):
        monkeypatch.setenv("BUDGETAPP_LIVE_SANDBOX", "1")
        store = KeychainStore()
        for call in (lambda: store.get(account), lambda: store.set(account, "value-1234")):
            with pytest.raises(SecretStoreError, match="Sandbox Keychain items"):
                call()
        assert store._backend is None  # refused before the Keychain was opened

    def test_sandbox_accounts(self):
        assert sec.is_sandbox_account("plaid.client_id")
        assert sec.is_sandbox_account("plaid.access_token.sandbox.item-1")
        assert sec.is_sandbox_account(SANDBOX_ITEM_INDEX)
        assert not sec.is_sandbox_account("plaid.link_in_progress.production")
        assert not sec.is_sandbox_account("plaid.access_token.production.item-1")
        assert not sec.is_sandbox_account(PRODUCTION_ITEM_INDEX)

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_unattended_mode_binds_the_security_framework(self):
        assert sec.enable_unattended_mode() is True
        assert sec.is_unattended()

    def test_approval_message(self):
        title, message = sec.keychain_approval_message()
        assert "Keychain" in title and "./run doctor --env production" in message


class TestRedaction:
    def test_values_read_are_redacted(self, secrets):
        secrets.values["plaid.secret.sandbox"] = "abcdef0123456789abcdef01234567"
        value = sec.get_secret("plaid.secret.sandbox")
        assert logsetup.redact(f"secret={value}!") == "secret=[REDACTED]!"

    def test_plaid_token_shapes_are_redacted(self):
        text = f"token {SANDBOX_TOKEN} and public-production-123e4567 and link-sandbox-abc-1"
        redacted = logsetup.redact(text)
        assert "aaaaaaaa" not in redacted and "123e4567" not in redacted and "abc-1" not in redacted
        assert redacted.count("[REDACTED]") == 3

    def test_short_values_are_not_registered(self):
        logsetup.register_secret("abc")
        assert logsetup.redact("abc") == "abc"

    def test_log_records_and_tracebacks_are_redacted(self, secrets):
        secrets.values["db.password.budgetapp_dev"] = "hunter2hunter2"
        password = sec.get_secret("db.password.budgetapp_dev")
        stream = io.StringIO()
        handler = logsetup.make_handler(logging.StreamHandler(stream))
        logger = logging.getLogger("test.redaction")
        logger.addHandler(handler)
        logger.propagate = False
        try:
            logger.warning("password is %s", password)
            try:
                raise RuntimeError(f"failed with {password} and {SANDBOX_TOKEN}")
            except RuntimeError:
                logger.exception("boom")
        finally:
            logger.removeHandler(handler)
        output = stream.getvalue()
        assert "hunter2hunter2" not in output and "aaaaaaaa-bbbb" not in output
        assert output.count("[REDACTED]") >= 3

    def test_excepthook_redacts(self, capsys, secrets):
        secrets.values["plaid.client_id"] = "0123456789abcdef01234567"
        sec.get_secret("plaid.client_id")
        previous = sys.excepthook
        try:
            logsetup.install_excepthook()
            try:
                raise ValueError("leaked 0123456789abcdef01234567")
            except ValueError:
                sys.excepthook(*sys.exc_info())
        finally:
            sys.excepthook = previous
        assert "0123456789abcdef01234567" not in capsys.readouterr().err

    def test_uncaught_errors_in_other_threads_are_redacted(self, capsys, secrets):
        secrets.values["db.password.budgetapp_live"] = "kN3v9-xQ2_Zr8LmW4tYp6"
        password = sec.get_secret("db.password.budgetapp_live")
        previous = (sys.excepthook, threading.excepthook, sys.unraisablehook)

        def worker():
            raise ValueError(f"worker failed for {PROD_TOKEN} pw={password}")

        try:
            logsetup.install_excepthook()
            thread = threading.Thread(target=worker, name="sync-worker")
            thread.start()
            thread.join()
        finally:
            sys.excepthook, threading.excepthook, sys.unraisablehook = previous
        err = capsys.readouterr().err
        assert "sync-worker" in err and "ValueError" in err
        assert password not in err and "11111111-2222" not in err

    def test_setup_logging_writes_a_private_file(self, tmp_path):
        settings = load_settings("sandbox", logs_dir=tmp_path / "logs")
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            logsetup.setup_logging(settings, log_name="sync", console=False)
            logsetup.register_secret("topsecretvalue")
            logging.getLogger("budgetapp.test").warning("value topsecretvalue")
            for handler in root.handlers:
                handler.flush()
        finally:
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)
                    handler.close()
        log_file = tmp_path / "logs" / "sync.log"
        assert _mode(log_file) == 0o600
        assert "[REDACTED]" in log_file.read_text()
        assert "topsecretvalue" not in log_file.read_text()


class TestDailyMarker:
    def test_once_a_day(self, tmp_path):
        day = dt.date(2026, 10, 15)
        assert sec.claim_daily_marker(tmp_path, "keychain:production", today=day) is True
        assert sec.claim_daily_marker(tmp_path, "keychain:production", today=day) is False
        assert sec.claim_daily_marker(tmp_path, "backup:stale", today=day) is True
        assert sec.claim_daily_marker(tmp_path, "keychain:production", today=day + dt.timedelta(1))
        marker = next((tmp_path / "markers").iterdir())
        assert _mode(marker) == 0o600

    def test_concurrent_claims_notify_once(self, tmp_path):
        day = dt.date(2026, 10, 28)
        for round_ in range(20):
            barrier = threading.Barrier(8)
            results = []

            def claim(key=f"keychain-{round_}", barrier=barrier, results=results):
                barrier.wait()
                results.append(sec.claim_daily_marker(tmp_path, key, today=day))

            threads = [threading.Thread(target=claim) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert results.count(True) == 1

    def test_a_marker_another_process_created_counts_and_old_days_are_pruned(self, tmp_path):
        markers = tmp_path / "markers"
        markers.mkdir()
        (markers / "keychain.2026-10-27").touch()
        (markers / "keychain.extra.2026-10-27").touch()  # another key: kept
        fd = os.open(markers / "keychain.2026-10-28", os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        assert not sec.claim_daily_marker(tmp_path, "keychain", today=dt.date(2026, 10, 28))
        assert sec.claim_daily_marker(tmp_path, "keychain", today=dt.date(2026, 10, 29))
        names = sorted(p.name for p in markers.iterdir())
        assert names == ["keychain.2026-10-29", "keychain.extra.2026-10-27"]

    def test_uses_the_clock(self, tmp_path, freeze_today):
        freeze_today("2026-10-15")
        assert sec.claim_daily_marker(tmp_path, "k")
        assert not sec.claim_daily_marker(tmp_path, "k")
        freeze_today("2026-10-16")
        assert clock.today() == dt.date(2026, 10, 16)
        assert sec.claim_daily_marker(tmp_path, "k")
