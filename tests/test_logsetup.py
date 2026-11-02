"""Log files stay bounded (final review ops-7): the app's own logs rotate, and the logs launchd
and PostgreSQL write (``sync.log``, ``backup.log``, ``web.log``, ``postgres.log``) are trimmed by
the backup agent, keeping their newest lines."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from budgetapp import logsetup
from budgetapp.config import load_settings


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.fixture
def root_handlers():
    """Remove whatever handlers a test's setup_logging adds."""
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def test_app_logs_rotate_and_stay_private(tmp_path, monkeypatch, root_handlers):
    """A plain FileHandler grew forever; under ``umask 022`` a new file must still be 0600."""
    monkeypatch.setattr(logsetup, "LOG_MAX_BYTES", 2_000)
    settings = load_settings("sandbox", logs_dir=tmp_path / "logs")
    previous = os.umask(0o022)
    try:
        logsetup.setup_logging(settings, log_name="sync-sandbox", console=False)
        log = logging.getLogger("budgetapp.test")
        for number in range(400):
            log.warning("line %04d of a sync that keeps failing", number)
    finally:
        os.umask(previous)
    files = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert files == [
        "sync-sandbox.log",
        *(f"sync-sandbox.log.{n}" for n in range(1, logsetup.LOG_BACKUPS + 1)),
    ]
    for name in files:
        path = tmp_path / "logs" / name
        assert _mode(path) == 0o600, name
        assert path.stat().st_size <= 2_000 + 200, name
    assert "line 0399" in (tmp_path / "logs" / "sync-sandbox.log").read_text()


def numbered(path: Path, lines: int) -> None:
    path.write_text(
        "".join(f"{n:06d} postgres FATAL: could not open data directory\n" for n in range(lines))
    )
    os.chmod(path, 0o600)


def test_trim_logs_keeps_the_tail_of_oversize_logs_only(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    big, small, other = logs / "postgres.log", logs / "backup.log", logs / "notes.txt"
    numbered(big, 1_000)  # 54 kB
    numbered(small, 10)
    numbered(other, 1_000)
    small_before = small.read_bytes()
    trimmed = logsetup.trim_logs(logs, max_bytes=10_000, keep_bytes=2_000)
    assert trimmed == [big]
    text = big.read_text()
    first, *kept = text.splitlines()
    assert first.startswith("--- older lines removed by budgetapp")
    assert kept[-1].startswith("000999 ") and kept[0][:6].isdigit()  # whole lines only
    assert len(text) <= 2_000 + len(first) + 1
    assert _mode(big) == 0o600 and small.read_bytes() == small_before
    assert other.stat().st_size > 10_000  # only *.log files


def test_a_writer_that_appends_keeps_writing_at_the_end(tmp_path):
    """launchd and pg_ctl hold the file open with O_APPEND: after trimming in place their next
    line lands after the kept tail (no hole of zero bytes, nothing lost to a renamed file)."""
    path = tmp_path / "web.log"
    numbered(path, 1_000)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        logsetup.trim_logs(tmp_path, max_bytes=10_000, keep_bytes=2_000)
        os.write(fd, b"after the trim\n")
    finally:
        os.close(fd)
    data = path.read_bytes()
    assert data.endswith(b"000999 postgres FATAL: could not open data directory\nafter the trim\n")
    assert b"\0" not in data and len(data) < 2_200


def test_a_missing_folder_trims_nothing(tmp_path):
    assert logsetup.trim_logs(tmp_path / "nowhere") == []
