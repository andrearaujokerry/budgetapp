"""Multi-step syncs ("Sync now", the backfill after linking), ``sync_all`` and the background job
runner (ARCHITECTURE §8, §10; SYNC-2)."""

from __future__ import annotations

import datetime as dt
import threading

import pytest
from sqlalchemy import select

from budgetapp.models import SyncRun
from budgetapp.services import background, sync
from budgetapp.services.background import BackgroundJobs, run_steps
from tests.factories import make_sync_run
from tests.test_sync import NOW, Linked, no_sleep


@pytest.fixture
def at_now(freeze_today):
    freeze_today(now=NOW)


@pytest.fixture
def linked(db, session_factory, fake_plaid, secrets, settings, notifier, at_now) -> Linked:
    return Linked(db, session_factory, fake_plaid, secrets, settings, notifier)


def task_args(linked: Linked) -> tuple:
    return (linked.session_factory, linked.fake, linked.store, linked.settings, linked.item.id)


# --------------------------------------------------------------------------- Sync now


def test_sync_now_refreshes_then_syncs_now_and_three_more_times(linked, fake_plaid):
    fake_plaid.add_transaction(linked.fake_item)
    slept: list[float] = []
    outcomes = sync.refresh_then_sync(
        *task_args(linked), notifier=linked.notifier, sleep=slept.append
    )
    assert len(fake_plaid.calls_to("transactions_refresh")) == 1
    assert [o.status for o in outcomes] == ["success"] * 4
    assert [o.added for o in outcomes] == [1, 0, 0, 0]
    assert slept == [15.0, 30.0, 75.0]  # syncs at about +15 s, +45 s and +2 min
    assert {o.trigger for o in outcomes} == {"manual"}


def test_new_data_after_the_refresh_lands_in_a_follow_up(linked, fake_plaid):
    def late_data(item):  # Plaid's refresh is asynchronous: new rows show up a bit later
        def arrive(_seconds):
            if not fake_plaid.current_transactions(item):
                fake_plaid.add_transaction(item, amount="77.00")

        return arrive

    sleep = late_data(linked.fake_item)
    outcomes = sync.refresh_then_sync(*task_args(linked), notifier=linked.notifier, sleep=sleep)
    assert [o.added for o in outcomes] == [0, 1, 0, 0]


def test_sync_now_reuses_the_prepared_run(linked, db):
    prepared = make_sync_run(db, linked.item, status="running", trigger="manual", started_at=NOW)
    outcomes = sync.refresh_then_sync(
        *task_args(linked), notifier=linked.notifier, run_id=prepared.id, delays=(), sleep=no_sleep
    )
    assert outcomes[0].sync_run_id == prepared.id
    db.expire_all()
    assert [r.status for r in db.scalars(select(SyncRun))] == ["success"]


def test_a_rate_limited_refresh_still_syncs(linked, fake_plaid):
    fake_plaid.queue_error(
        "transactions_refresh", fake_plaid.plaid_error("TRANSACTIONS_REFRESH_LIMIT")
    )
    task = sync.RefreshThenSync(*task_args(linked), notifier=linked.notifier, sleep=no_sleep)
    assert task() == 15.0
    assert task.refresh_error.error_code == "TRANSACTIONS_REFRESH_LIMIT"
    assert task.outcomes[0].status == "success"


def test_a_failed_sync_ends_the_follow_ups(linked, fake_plaid):
    fake_plaid.set_item_error(linked.fake_item, "ITEM_LOGIN_REQUIRED")
    slept: list[float] = []
    outcomes = sync.refresh_then_sync(
        *task_args(linked), notifier=linked.notifier, sleep=slept.append
    )
    assert [o.status for o in outcomes] == ["error"] and slept == []
    linked.db.expire_all()
    assert linked.item.status == "needs_reconnect"


# --------------------------------------------------------------------------- backfill


def test_the_backfill_polls_until_history_is_complete(linked, fake_plaid):
    fake_plaid.set_update_status(linked.fake_item, "NOT_READY")
    fake_plaid.add_transaction(linked.fake_item)
    fake_plaid.script_sync(linked.fake_item, 2, update_status="INITIAL_UPDATE_COMPLETE")
    fake_plaid.script_sync(linked.fake_item, 3, update_status="HISTORICAL_UPDATE_COMPLETE")
    slept: list[float] = []
    outcomes = sync.backfill_until_complete(
        *task_args(linked), notifier=linked.notifier, sleep=slept.append
    )
    assert [o.update_status for o in outcomes] == [
        "NOT_READY",
        "INITIAL_UPDATE_COMPLETE",
        "HISTORICAL_UPDATE_COMPLETE",
    ]
    assert slept == list(sync.BACKFILL_DELAYS[:2])
    assert {o.trigger for o in outcomes} == {"link"}
    linked.db.expire_all()
    assert linked.item.history_complete_at is not None


def test_the_backfill_gives_up_at_the_cap(linked, fake_plaid):
    fake_plaid.set_update_status(linked.fake_item, "NOT_READY")
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    outcomes = sync.backfill_until_complete(
        *task_args(linked),
        notifier=linked.notifier,
        sleep=sleep,
        monotonic=lambda: clock["t"],
        cap=100.0,
    )
    # syncs at 0, 15, 30 and 60 s; the next would wait 60 s more, past the 100 s cap
    assert clock["t"] == 60.0 and len(outcomes) == 4
    assert all(o.status == "not_ready" for o in outcomes)


def test_the_backfill_stops_when_the_item_needs_reconnecting(linked, fake_plaid):
    fake_plaid.set_item_error(linked.fake_item, "ITEM_LOGIN_REQUIRED")
    outcomes = sync.backfill_until_complete(
        *task_args(linked), notifier=linked.notifier, sleep=no_sleep
    )
    assert len(outcomes) == 1 and outcomes[0].needs_reconnect


def test_the_backfill_retries_a_transient_failure(linked, fake_plaid):
    fake_plaid.queue_error("transactions_sync", fake_plaid.plaid_error("INSTITUTION_DOWN"))
    outcomes = sync.backfill_until_complete(
        *task_args(linked), notifier=linked.notifier, sleep=no_sleep
    )
    assert [o.status for o in outcomes] == ["error", "success"]


def test_after_update_mode_it_syncs_now_and_once_more(linked):
    slept: list[float] = []
    outcomes = sync.backfill_until_complete(
        *task_args(linked),
        notifier=linked.notifier,
        trigger="update",
        delays=sync.UPDATE_DELAYS,
        min_runs=2,
        sleep=slept.append,
    )
    assert [o.trigger for o in outcomes] == ["update", "update"] and slept == [60.0]


# --------------------------------------------------------------------------- sync_all


def test_sync_all_syncs_every_item_or_only_the_due_ones(
    db, session_factory, fake_plaid, secrets, settings, notifier, at_now
):
    fresh = Linked(db, session_factory, fake_plaid, secrets, settings, notifier, item_id="fresh")
    old = Linked(db, session_factory, fake_plaid, secrets, settings, notifier, item_id="old")
    fresh.item.last_sync_success_at = NOW - dt.timedelta(hours=1)
    old.item.last_sync_success_at = NOW - dt.timedelta(hours=6)
    db.flush()
    due = sync.sync_all(
        session_factory,
        fake_plaid,
        secrets,
        settings,
        trigger="schedule",
        notifier=notifier,
        if_due=True,
        sleep=no_sleep,
    )
    assert [o.item_id for o in due.outcomes] == [old.item.id] and due.not_due == (fresh.item.id,)
    assert due.ok
    everything = sync.sync_all(
        session_factory,
        fake_plaid,
        secrets,
        settings,
        trigger="cli",
        notifier=notifier,
        sleep=no_sleep,
    )
    assert [o.item_id for o in everything.outcomes] == [fresh.item.id, old.item.id]
    only = sync.sync_all(
        session_factory,
        fake_plaid,
        secrets,
        settings,
        trigger="cli",
        notifier=notifier,
        item_ids=[old.item.id],
        sleep=no_sleep,
    )
    assert [o.item_id for o in only.outcomes] == [old.item.id]
    with pytest.raises(LookupError):
        sync.sync_all(
            session_factory,
            fake_plaid,
            secrets,
            settings,
            trigger="cli",
            notifier=notifier,
            item_ids=[999999],
            sleep=no_sleep,
        )


def test_sync_all_finishes_the_other_items_before_re_raising(
    db, session_factory, fake_plaid, secrets, settings, notifier, at_now, monkeypatch
):
    first = Linked(db, session_factory, fake_plaid, secrets, settings, notifier, item_id="first")
    second = Linked(db, session_factory, fake_plaid, secrets, settings, notifier, item_id="second")
    real = sync.sync_item
    calls: list[int] = []

    def flaky(*args, **kwargs):
        calls.append(args[4])
        if args[4] == first.item.id:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(sync, "sync_item", flaky)
    with pytest.raises(RuntimeError):
        sync.sync_all(
            session_factory,
            fake_plaid,
            secrets,
            settings,
            trigger="cli",
            notifier=notifier,
            sleep=no_sleep,
        )
    assert calls == [first.item.id, second.item.id]


def test_sync_all_reports_failures(linked, fake_plaid):
    fake_plaid.set_item_error(linked.fake_item, "ITEM_LOGIN_REQUIRED")
    result = sync.sync_all(
        linked.session_factory,
        fake_plaid,
        linked.store,
        linked.settings,
        trigger="cli",
        notifier=linked.notifier,
        sleep=no_sleep,
    )
    assert not result.ok and result.outcomes[0].error_code == "ITEM_LOGIN_REQUIRED"


# --------------------------------------------------------------------------- background runner


def test_jobs_are_deduplicated_by_key():
    jobs = BackgroundJobs(start=False)
    ran: list[str] = []
    assert jobs.submit("a", lambda: ran.append("first"))
    assert not jobs.submit("a", lambda: ran.append("second"))
    assert jobs.submit("b", lambda: ran.append("b"))
    assert jobs.is_waiting("a") and jobs.waiting_keys() == ["a", "b"]
    assert jobs.run_all() == 2
    assert ran == ["first", "b"] and not jobs.is_busy("a")


def test_replace_swaps_the_waiting_job():
    jobs = BackgroundJobs(start=False)
    ran: list[str] = []
    jobs.submit("a", lambda: ran.append("old"), delay=60)
    assert jobs.submit("a", lambda: ran.append("new"), replace=True)
    jobs.run_all()
    assert ran == ["new"]


def test_a_job_returning_seconds_runs_again_under_its_key():
    jobs = BackgroundJobs(start=False)
    steps = iter([15.0, 30.0, None])
    jobs.submit("chain", lambda: next(steps))
    assert jobs.run_all() == 3
    assert jobs.history == [("chain", 0.0), ("chain", 15.0), ("chain", 30.0)]


def test_a_newer_submit_wins_over_a_chain_rescheduling_itself():
    jobs = BackgroundJobs(start=False)
    ran: list[str] = []

    def chain():
        ran.append("chain")
        jobs.submit("k", lambda: ran.append("fresh"))  # e.g. Sync now pressed again meanwhile
        return 15.0

    jobs.submit("k", chain)
    jobs.run_all()
    assert ran == ["chain", "fresh"]


def test_only_due_jobs_run():
    now = {"t": 100.0}
    jobs = BackgroundJobs(start=False, monotonic=lambda: now["t"])
    ran: list[str] = []
    jobs.submit("later", lambda: ran.append("later"), delay=30)
    jobs.submit("now", lambda: ran.append("now"))
    assert jobs.run_due() == 1 and ran == ["now"]
    now["t"] += 30
    assert jobs.run_due() == 1 and ran == ["now", "later"]


def test_a_failing_job_is_logged_and_the_runner_goes_on(caplog):
    jobs = BackgroundJobs(start=False)
    ran: list[str] = []

    def broken():
        raise RuntimeError("bad job")

    jobs.submit("broken", broken)
    jobs.submit("ok", lambda: ran.append("ok"))
    jobs.run_all()
    assert ran == ["ok"] and "Background job broken failed" in caplog.text


def test_cancel_and_shutdown():
    jobs = BackgroundJobs(start=False)
    jobs.submit("a", lambda: None)
    assert jobs.cancel("a") and not jobs.cancel("a")
    jobs.submit("b", lambda: None)
    jobs.shutdown()
    assert jobs.run_all() == 0 and not jobs.submit("c", lambda: None)


def test_the_worker_thread_runs_jobs_in_the_background():
    jobs = BackgroundJobs()
    done = threading.Event()
    seen: list[str] = []

    def job():
        seen.append(threading.current_thread().name)
        done.set()

    try:
        jobs.submit("bg", job)
        assert done.wait(5)
        assert seen == ["budgetapp-jobs"]
    finally:
        jobs.shutdown(wait=True, timeout=5)


def test_run_steps_sleeps_what_the_task_asks():
    steps = iter([1.0, 2.0, None])
    slept: list[float] = []
    run_steps(lambda: next(steps), slept.append)
    assert slept == [1.0, 2.0]
    assert background.run_steps is run_steps
