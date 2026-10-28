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
    PRODUCTION_ACCESS_TOKEN,
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
        assert sec.access_token_key("production", "anything") == PRODUCTION_ACCESS_TOKEN
        assert sec.access_token_key("sandbox", "abc") == "plaid.access_token.sandbox.abc"
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
        assert secrets.values[PRODUCTION_ACCESS_TOKEN] == PROD_TOKEN
        sec.store_access_token(prod, "item-1", PROD_TOKEN)  # same token: no-op
        assert sec.get_access_token("production") == PROD_TOKEN

    def test_a_different_token_is_never_overwritten(self, prod, secrets):
        sec.store_access_token(prod, "item-1", PROD_TOKEN)
        with pytest.raises(AccessTokenConflict) as info:
            sec.store_access_token(prod, "item-2", OTHER_TOKEN)
        assert secrets.values[PRODUCTION_ACCESS_TOKEN] == PROD_TOKEN
        recovery = list((prod.state_dir / "recovery").iterdir())
        assert len(recovery) == 1 and OTHER_TOKEN in recovery[0].read_text()
        assert OTHER_TOKEN not in str(info.value)

    def test_failed_write_goes_to_a_private_recovery_file(self, prod, secrets):
        secrets.fail_on[("set", PRODUCTION_ACCESS_TOKEN)] = KeychainApprovalNeeded(
            PRODUCTION_ACCESS_TOKEN, -25308
        )
        with pytest.raises(TokenNotSavedError) as info:
            sec.store_access_token(prod, "item-1", PROD_TOKEN)
        path = info.value.recovery_path
        assert path.exists() and path.parent == prod.state_dir / "recovery"
        assert _mode(path) == 0o600 and _mode(path.parent) == 0o700
        content = path.read_text()
        assert PROD_TOKEN in content and "item-1" in content and "--access-token" in content
        assert PROD_TOKEN not in str(info.value)
        assert PRODUCTION_ACCESS_TOKEN not in secrets.values

    def test_any_unexpected_error_still_saves_the_token(self, prod, secrets):
        secrets.fail_on[("set", None)] = RuntimeError("no keyring backend")
        with pytest.raises(TokenNotSavedError) as info:
            sec.store_access_token(prod, "item-1", PROD_TOKEN)
        assert PROD_TOKEN in info.value.recovery_path.read_text()

    def test_read_back_mismatch_goes_to_the_recovery_file(self, prod, secrets):
        secrets.corrupt_reads.add(PRODUCTION_ACCESS_TOKEN)
        with pytest.raises(TokenNotSavedError):
            sec.store_access_token(prod, "item-1", PROD_TOKEN)

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
        secrets.fail_on[("set", PRODUCTION_ACCESS_TOKEN)] = interrupt
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

    def test_sandbox_index_is_written_before_the_token(self, sandbox, secrets):
        order = []
        original = secrets.set

        def recording_set(account, value):
            order.append(account)
            original(account, value)

        secrets.set = recording_set
        sec.store_access_token(sandbox, "item-a", SANDBOX_TOKEN)
        assert order == [SANDBOX_ITEM_INDEX, "plaid.access_token.sandbox.item-a"]

    def test_failed_index_write_never_leaves_an_unfindable_token(self, sandbox, secrets):
        secrets.fail_on[("set", SANDBOX_ITEM_INDEX)] = RuntimeError("index write failed")
        with pytest.raises(TokenNotSavedError) as info:
            sec.store_access_token(sandbox, "item-a", SANDBOX_TOKEN)
        assert "plaid.access_token.sandbox.item-a" not in secrets.values
        assert SANDBOX_TOKEN in info.value.recovery_path.read_text()


class TestProductionLinkGuard:
    def test_nothing_blocks_a_first_link(self, prod):
        assert sec.production_link_blockers(prod) == []

    def test_a_saved_token_or_a_live_marker_blocks(self, prod, secrets, freeze_today):
        start = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
        freeze_today(now=start)
        sec.set_link_in_progress()
        assert ["started" in r for r in sec.production_link_blockers(prod)] == [True]
        freeze_today(now=start + dt.timedelta(hours=4, minutes=1))
        assert sec.production_link_blockers(prod) == []
        secrets.values[PRODUCTION_ACCESS_TOKEN] = PROD_TOKEN
        assert ["re-attach" in r for r in sec.production_link_blockers(prod)] == [True]

    def test_a_token_stuck_in_a_recovery_file_blocks_after_the_marker_expires(
        self, prod, secrets, freeze_today
    ):
        start = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
        freeze_today(now=start)
        sec.set_link_in_progress()
        secrets.fail_on[("set", PRODUCTION_ACCESS_TOKEN)] = RuntimeError("Keychain refused")
        with pytest.raises(TokenNotSavedError):
            sec.store_access_token(prod, "item-1", PROD_TOKEN)
        freeze_today(now=start + dt.timedelta(hours=4, minutes=1))
        assert sec.get_link_in_progress() is None and sec.get_access_token("production") is None
        [reason] = sec.production_link_blockers(prod)
        assert "recovery file" in reason and PROD_TOKEN not in reason

        del secrets.fail_on[("set", PRODUCTION_ACCESS_TOKEN)]
        sec.store_access_token(prod, "item-1", PROD_TOKEN)
        [saved] = sec.mark_recovery_saved(prod, PROD_TOKEN)
        assert saved.suffix == ".saved" and _mode(saved) == 0o600
        assert sec.unsaved_production_tokens(prod) == []
        assert ["re-attach" in r for r in sec.production_link_blockers(prod)] == [True]

    def test_only_files_holding_that_token_are_marked_saved(self, prod, secrets):
        secrets.fail_on[("set", PRODUCTION_ACCESS_TOKEN)] = RuntimeError("Keychain refused")
        for token in (PROD_TOKEN, OTHER_TOKEN):
            with pytest.raises(TokenNotSavedError):
                sec.store_access_token(prod, "item-1", token)
        assert len(sec.mark_recovery_saved(prod, PROD_TOKEN)) == 1
        [left] = sec.unsaved_production_tokens(prod)
        assert OTHER_TOKEN in left.read_text()


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
        [PRODUCTION_ACCESS_TOKEN, "plaid.secret.production", "db.password.budgetapp_live"],
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
