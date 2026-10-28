import subprocess

from budgetapp.config import load_settings
from budgetapp.services.notify import (
    NullNotifier,
    OsascriptNotifier,
    RecordingNotifier,
    get_notifier,
)

INJECTION = '" & (do shell script "echo pwned") & "'


class FakeRun:
    def __init__(self, returncode=0, stderr="", raises=None):
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


def test_title_and_message_are_argv_data_never_script():
    runner = FakeRun()
    result = OsascriptNotifier(runner=runner).notify(INJECTION, "-5.00 refund", subtitle="sub")
    assert result.ok
    args, kwargs = runner.calls[0]
    assert args[0] == "/usr/bin/osascript"
    separator = args.index("--")
    script = " ".join(args[:separator])
    assert "on run argv" in script and "item 1 of argv" in script
    assert INJECTION not in script and "refund" not in script
    assert args[separator + 1 :] == [INJECTION, "-5.00 refund", "sub"]
    assert kwargs["timeout"] == 15 and kwargs["check"] is False
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}


def test_long_text_is_clipped_and_whitespace_collapsed():
    args = OsascriptNotifier(runner=FakeRun()).command("t", "line one\n\nline   two " + "x" * 900)
    message = args[args.index("--") + 2]
    assert "\n" not in message and len(message) <= 500 and message.endswith("…")


def test_failures_never_raise():
    assert not OsascriptNotifier(runner=FakeRun(returncode=1, stderr="no")).notify("t", "m").ok
    timeout = FakeRun(raises=subprocess.TimeoutExpired("osascript", 15))
    assert OsascriptNotifier(runner=timeout).notify("t", "m").error == "osascript timed out"
    missing = FakeRun(raises=FileNotFoundError("osascript"))
    assert "FileNotFoundError" in OsascriptNotifier(runner=missing).notify("t", "m").error


def test_recording_and_null_notifiers():
    recorder = RecordingNotifier()
    recorder.notify("A", "B", subtitle="C")
    assert recorder.titles == ["A"] and recorder.sent[0].subtitle == "C"
    recorder.fail_with = "nope"
    assert recorder.notify("A", "B").error == "nope"
    assert NullNotifier().notify("A", "B").ok


def test_get_notifier(settings):
    assert isinstance(get_notifier(settings), NullNotifier)
    assert isinstance(get_notifier(load_settings("sandbox")), OsascriptNotifier)
