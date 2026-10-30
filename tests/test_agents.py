"""launchd agents and the production install (ARCHITECTURE §2, §10; RESEARCH T5.2).

Everything happens in temporary folders with a fake ``launchctl``/``git``/``pip``: no test loads,
bootstraps, kickstarts or boots out a real agent, writes to ~/Library/LaunchAgents or touches the
real install. ``plutil -lint`` and ``/bin/sh`` (the postgres wrapper) run for real.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import plistlib
import subprocess
import tarfile
from pathlib import Path

import pytest

from budgetapp.commands import agents as agents_command
from budgetapp.config import ConfigError, check_private_path, load_settings, protected_folders
from budgetapp.db import code_head
from budgetapp.services import agents

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OLD_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
#: What the fake ``git archive`` exports for COMMIT.
TREE = {
    "requirements.txt": "fastapi==0.162.0\n",
    "pyproject.toml": '[project]\nname = "budgetapp"\n',
    "budgetapp/__init__.py": '__version__ = "0.1.0"\n',
}


def write_tar(path: Path, files: dict[str, str]) -> None:
    with tarfile.open(path, "w") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))


@pytest.fixture
def prod(tmp_path):
    """Production settings whose every path is a temporary folder (with a space, like the real
    "Application Support")."""
    return load_settings(
        "production",
        data_dir=tmp_path / "App Support" / "budgetapp",
        logs_dir=tmp_path / "Logs" / "budgetapp",
    )


@pytest.fixture
def layout(prod, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("fastapi==0.162.0\n")
    return agents.Layout.for_settings(
        prod, home=tmp_path / "home", repo_dir=repo, uid=501, python="/fake/python3.14"
    )


class Mac:
    """Fake launchctl / git / python / pip; plutil runs for real. Records an event log."""

    def __init__(self, layout, *, loaded=(), disabled=(), commit=COMMIT, status="", events=None):
        self.layout = layout
        self.loaded = set(loaded)
        self.disabled = set(disabled)
        self.commit = commit
        self.status = status
        self.events = events if events is not None else []
        self.calls: list[tuple[list[str], dict]] = []
        self.fail_pip = False
        self.venv_runs = True
        self.tree = dict(TREE)
        #: Agents whose bootstrap fails ("Bootstrap failed: 5"); ``…_once`` only the first time.
        self.fail_bootstrap: set[str] = set()
        self.fail_bootstrap_once: set[str] = set()

    def _done(self, argv, code=0, out="", err=""):
        return subprocess.CompletedProcess(argv, code, out, err)

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        program = Path(argv[0]).name
        if program == "launchctl":
            return self._launchctl(argv)
        if program == "plutil":
            return subprocess.run(argv, **kwargs)
        if program == "git":
            if "rev-parse" in argv:
                return self._done(argv, out=f"{self.commit}\n")
            if "status" in argv:
                return self._done(argv, out=self.status)
            if "archive" in argv:
                self.events.append("git archive")
                assert argv[-1] == self.commit  # the commit read at the start, never HEAD
                output = next(a for a in argv if a.startswith("--output="))
                write_tar(Path(output.split("=", 1)[1]), self.tree)
                return self._done(argv)
        if "-m" in argv and argv[argv.index("-m") + 1] == "venv":
            self.events.append("venv")
            self.venv_runs = True
            python = Path(argv[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\n")
            return self._done(argv)
        if "-c" in argv:
            if not self.venv_runs:
                return self._done(argv, 1, "", "dyld: Library not loaded")
            return self._done(argv, out="3.14.2\n")
        if "pip" in argv:
            self.events.append("pip --no-deps" if "--no-deps" in argv else "pip -r")
            if self.fail_pip:
                return self._done(argv, 1, "", "ERROR: No matching distribution found")
            return self._done(argv)
        if "migrate" in argv:
            self.events.append("migrate")
            return self._done(argv, out="budgetapp: 0003_f1_verification_fixes → 0004_x.\n")
        raise AssertionError(f"unexpected program: {argv}")

    def _launchctl(self, argv):
        verb = argv[1]
        name = argv[-1].rsplit(".", 2)[-2] if verb == "bootstrap" else argv[-1].rsplit(".", 1)[-1]
        if verb == "print":
            return self._done(argv, 0 if name in self.loaded else 113)
        if verb == "print-disabled":
            lines = [f'\t"{agents.label(n)}" => disabled' for n in self.disabled]
            lines.append('\t"com.example.other" => enabled')
            return self._done(argv, out="disabled services = {\n" + "\n".join(lines) + "\n}\n")
        self.events.append(f"{verb} {name}")
        if verb == "bootstrap":
            if name in self.fail_bootstrap or name in self.fail_bootstrap_once:
                self.fail_bootstrap_once.discard(name)
                return self._done(argv, 5, "", "Bootstrap failed: 5: Input/output error")
            self.loaded.add(name)
        elif verb == "bootout":
            if name not in self.loaded:
                return self._done(argv, 113)
            self.loaded.discard(name)
        return self._done(argv)

    def launchd_verbs(self) -> list[str]:
        return [e for e in self.events if e.split()[0] in ("bootstrap", "bootout", "kickstart")]


class FakeCluster:
    def __init__(self, events, running=True, ready=True):
        self.events = events
        self.running = running
        self.ready = ready

    def start(self, settings):
        self.events.append("cluster start")
        self.running = True
        return "already running"

    def stop(self, settings):
        self.events.append("cluster stop")
        self.running = False
        return "stopped"

    def is_running(self, settings):
        return self.running

    def wait(self, settings, timeout=120):
        self.events.append("cluster wait")
        return self.ready


def install_plists(layout, names=agents.AGENTS):
    agents.write_agent_files(layout, list(names), subprocess.run)


# --------------------------------------------------------------------------- plists


class TestPlists:
    def test_every_agent_runs_production_from_the_install_with_private_logs(self, layout):
        for name in agents.AGENTS:
            plist = agents.build_plist(name, layout)
            assert plist["Label"] == f"local.budgetapp.{name}"
            env = plist["EnvironmentVariables"]
            assert env["BUDGETAPP_ENV"] == "production" and env["LC_ALL"] == "C.UTF-8"
            assert env["HOME"] == str(layout.home)
            assert (
                env["PATH"]
                == f"{layout.pg_bin_dir}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            )
            assert env["BUDGETAPP_DATA_DIR"] == str(layout.data_dir)
            assert plist["WorkingDirectory"] == str(layout.data_dir)
            log = str(layout.logs_dir / f"{name}.log")
            assert plist["StandardOutPath"] == plist["StandardErrorPath"] == log
            assert plist["Umask"] == 63 and plist["RunAtLoad"] is True

    def test_postgres_runs_the_stale_lock_wrapper_in_the_foreground(self, layout):
        plist = agents.build_plist("postgres", layout)
        assert plist["ProgramArguments"] == ["/bin/sh", str(layout.bin_dir / "postgres-launchd.sh")]
        assert plist["KeepAlive"] == {"SuccessfulExit": False}
        assert plist["ThrottleInterval"] == 30 and plist["ExitTimeOut"] == 120

    def test_sync_runs_four_times_a_day_and_at_login(self, layout):
        plist = agents.build_plist("sync", layout)
        python = str(layout.install_dir / "bin" / "python")
        assert plist["ProgramArguments"] == [
            python, "-I", "-u", "-m", "budgetapp", "sync", "--env", "production", "--if-due",
            "--unattended",
        ]  # fmt: skip
        assert plist["StartCalendarInterval"] == [{"Hour": h, "Minute": 15} for h in (0, 6, 12, 18)]
        assert "StartInterval" not in plist  # missed during sleep; calendar runs on wake
        assert plist["ProcessType"] == "Background" and plist["LowPriorityIO"] is True

    def test_backup_runs_at_half_past_two_and_at_login(self, layout):
        plist = agents.build_plist("backup", layout)
        assert plist["ProgramArguments"][-4:] == ["--env", "production", "--if-due", "--unattended"]
        assert plist["ProgramArguments"][5] == "backup"
        assert plist["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
        assert plist["ProcessType"] == "Background" and plist["LowPriorityIO"] is True

    def test_web_serves_production_and_stays_up(self, layout):
        plist = agents.build_plist("web", layout)
        assert plist["ProgramArguments"][5:] == ["serve", "--env", "production", "--unattended"]
        assert plist["KeepAlive"] is True

    def test_the_real_layout_stays_outside_protected_folders(self):
        """The real paths (computed only, nothing written) never touch TCC/iCloud folders."""
        real = agents.Layout.for_settings(load_settings("production"))
        for name in agents.AGENTS:
            xml = agents.render_plist(agents.build_plist(name, real)).decode()
            assert "~" not in xml and "$HOME" not in xml  # launchd expands nothing
            assert str(real.repo_dir) not in xml  # never the git checkout (TCC: ~/Desktop)
            plist = agents.build_plist(name, real)
            paths = [
                plist["WorkingDirectory"],
                plist["StandardOutPath"],
                *plist["ProgramArguments"],
            ]
            paths += [
                v for k, v in plist["EnvironmentVariables"].items() if k.startswith("BUDGETAPP_")
            ]
            for value in paths:
                if value.startswith("/") and not value.startswith(("/bin/", "/opt/")):
                    check_private_path(Path(value), name)
        with pytest.raises(ConfigError):
            check_private_path(protected_folders()[0] / "budgetapp", "probe")

    def test_files_are_written_with_safe_modes_and_pass_plutil(self, layout):
        written = agents.write_agent_files(layout, list(agents.AGENTS), subprocess.run)
        assert layout.wrapper_path in written
        for name in agents.AGENTS:
            path = layout.plist_path(name)
            assert path.parent == layout.launch_agents_dir
            assert path.stat().st_mode & 0o777 == 0o644
            with path.open("rb") as fh:
                assert plistlib.load(fh) == agents.build_plist(name, layout)
            assert subprocess.run(["/usr/bin/plutil", "-lint", str(path)]).returncode == 0
        assert layout.wrapper_path.stat().st_mode & 0o777 == 0o700
        assert layout.logs_dir.stat().st_mode & 0o777 == 0o700
        assert not list(layout.launch_agents_dir.glob(".*.tmp"))

    def test_a_broken_plist_fails_the_lint(self, tmp_path):
        broken = tmp_path / "broken.plist"
        broken.write_text("<plist><dict><key>Label</key></dict>")
        with pytest.raises(agents.AgentError, match="plutil -lint"):
            agents.lint_plist(broken, subprocess.run)


class TestSafety:
    def test_the_real_launch_agents_folder_and_install_are_refused_in_tests(self):
        real = agents.Layout.for_settings(load_settings("production"))
        with pytest.raises(agents.AgentError, match="Refusing"):
            agents.guard_layout(real)
        with pytest.raises(agents.AgentError, match="Refusing"):
            agents.write_agent_files(real, ["sync"], subprocess.run)

    def test_programs_never_run_implicitly_in_tests(self, prod, layout):
        with pytest.raises(agents.AgentError, match="Tests must pass a runner"):
            agents.Launchctl(501)
        with pytest.raises(agents.AgentError, match="Tests must pass a runner"):
            agents.install_agents(prod, layout=layout, dry_run=True)

    def test_production_database_helpers_refuse_tests(self, prod):
        with pytest.raises(agents.AgentError, match="never touch the production database"):
            agents._default_revision(prod)
        with pytest.raises(agents.AgentError, match="never touch the production database"):
            agents._default_backup(prod)

    def test_a_failed_backup_before_the_upgrade_is_an_agent_error(self, prod, monkeypatch):
        """./run live reports AgentError only; a BackupError used to end in a traceback."""
        from budgetapp.services import backup

        def failing(settings, **kwargs):  # stands in for pg_dump: nothing touches real data
            raise backup.BackupError("pg_dump failed: disk full")

        monkeypatch.setattr(agents, "_refuse_production_in_tests", lambda settings: None)
        monkeypatch.setattr(backup, "run_backup", failing)
        with pytest.raises(agents.AgentError, match="disk full.*nothing was upgraded"):
            agents._default_backup(prod)


# --------------------------------------------------------------------------- wrapper


@pytest.fixture
def wrapper(layout, tmp_path):
    """The real wrapper script around a stub ``postgres`` that prints what it was given."""
    bin_dir = tmp_path / "pgbin"
    bin_dir.mkdir()
    stub = bin_dir / "postgres"
    stub.write_text('#!/bin/sh\necho "stub $*"\necho "LC_ALL=$LC_ALL"\numask\n')
    stub.chmod(0o755)
    data = tmp_path / "pg data dir"
    data.mkdir()
    test_layout = layout.replace(pg_bin_dir=bin_dir, pg_data_dir=data)
    script = tmp_path / "wrapper.sh"
    script.write_text(agents.wrapper_script(test_layout))

    def run(pid_text: str | None):
        pidfile = data / "postmaster.pid"
        if pid_text is not None:
            pidfile.write_text(pid_text)
        result = subprocess.run(
            ["/bin/sh", str(script)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}
        )
        return result, pidfile.exists()

    run.data = data
    return run


@contextlib.contextmanager
def process(*argv):
    proc = subprocess.Popen(list(argv))
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


def fake_postgres(data_dir: Path, name: str = "/opt/fake/bin/postgres"):
    """A live process that ``ps`` shows as ``<name> … -D <data_dir>``."""
    return process(
        "/bin/bash", "-c", 'exec -a "$0" /bin/sh -c "sleep 30; :" x -D "$1"', name, str(data_dir)
    )


class TestPostgresWrapper:
    def test_without_a_pid_file_it_execs_postgres_on_the_data_dir(self, wrapper):
        result, exists = wrapper(None)
        assert result.returncode == 0
        lines = result.stdout.splitlines()
        assert lines == [f"stub -D {wrapper.data}", "LC_ALL=C.UTF-8", "0077"]
        assert result.stderr == ""

    def test_a_pid_file_of_our_running_postgres_is_kept(self, wrapper):
        with fake_postgres(wrapper.data) as pid:
            result, exists = wrapper(f"{pid}\n{wrapper.data}\n")
        assert exists and "removed" not in result.stderr
        assert result.stdout.startswith("stub -D ")

    @pytest.mark.parametrize("kind", ["other program", "other cluster", "prefix cluster"])
    def test_a_live_pid_that_is_not_our_postgres_is_stale(self, wrapper, tmp_path, kind):
        if kind == "other program":
            proc = process("/bin/sleep", "30")
        elif kind == "other cluster":
            proc = fake_postgres(tmp_path / "elsewhere")
        else:  # "<data dir>2" must not pass for "<data dir>"
            proc = fake_postgres(Path(f"{wrapper.data}2"))
        with proc as pid:
            result, exists = wrapper(f"{pid}\n")
        assert not exists
        assert f"removed a stale postmaster.pid (pid {pid} is not ours)" in result.stderr
        assert result.stdout.startswith("stub -D ")

    def test_dead_foreign_and_garbage_pids_are_stale(self, wrapper):
        done = subprocess.Popen(["/usr/bin/true"])
        done.wait()
        for text in (f"{done.pid}\n", "1\n", "garbage\n", ""):  # pid 1 is root's launchd
            result, exists = wrapper(text)
            assert not exists, text
            assert "removed a stale postmaster.pid" in result.stderr


# --------------------------------------------------------------------------- install / uninstall


def run_install(prod, layout, mac, cluster, **kwargs):
    upgrades = []

    def upgrade(settings, **kw):
        mac.events.append(f"upgrade restart={kw['restart']}")
        upgrades.append(kw)

    kwargs.setdefault("say", lambda line: None)
    lines = agents.install_agents(
        prod, layout=layout, runner=mac, cluster=cluster, upgrade=upgrade, **kwargs
    )
    return lines, upgrades


class TestInstall:
    def test_dry_run_prints_the_plan_and_changes_nothing(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, events=events)
        said: list[str] = []
        lines, upgrades = run_install(
            prod, layout, mac, FakeCluster(events), dry_run=True, with_web=True, say=said.append
        )
        assert upgrades == [] and events == []
        assert not layout.launch_agents_dir.exists() and not layout.bin_dir.exists()
        assert said[0] == "Plan (dry run, nothing changes):"
        bootstraps = [line for line in lines if line.startswith("launchctl bootstrap gui/501 ")]
        order = [line.split("local.budgetapp.")[1].split(".plist")[0] for line in bootstraps]
        assert order == ["postgres", "sync", "backup", "web"]
        assert any(COMMIT[:12] in line and "never editable" in line for line in lines)
        assert any("pg_ctl stop -m fast" in line for line in lines)

    def test_install_upgrades_then_bootstraps_in_order(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, events=events)
        cluster = FakeCluster(events)
        run_install(prod, layout, mac, cluster)
        assert events == [
            "cluster start",
            "upgrade restart=False",
            "cluster stop",
            "enable postgres",
            "bootstrap postgres",
            "cluster wait",
            "enable sync",
            "bootstrap sync",
            "enable backup",
            "bootstrap backup",
        ]
        assert agents.installed_agents(layout) == ["postgres", "sync", "backup"]
        assert layout.wrapper_path.exists()

    def test_reinstall_boots_out_first_and_drops_an_unwanted_web_agent(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=agents.AGENTS, events=events)
        run_install(prod, layout, mac, FakeCluster(events))
        assert mac.launchd_verbs() == [
            "bootout web",
            "bootout postgres",
            "bootstrap postgres",
            "bootout sync",
            "bootstrap sync",
            "bootout backup",
            "bootstrap backup",
        ]
        assert events.index("cluster stop") < events.index("bootout postgres")
        assert not layout.plist_path("web").exists()

    def test_with_web_installs_the_web_agent_last(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, events=events)
        run_install(prod, layout, mac, FakeCluster(events), with_web=True)
        assert mac.launchd_verbs()[-1] == "bootstrap web"
        assert layout.plist_path("web").exists()

    def test_a_database_that_never_starts_under_launchd_stops_the_install(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, events=events)
        with pytest.raises(agents.AgentError, match="did not start under launchd"):
            run_install(prod, layout, mac, FakeCluster(events, ready=False))
        assert "bootstrap sync" not in events

    def test_the_command_prints_errors(self, monkeypatch, prod, layout, capsys):
        events: list[str] = []
        mac = Mac(layout, events=events)
        monkeypatch.setattr(agents, "default_layout", lambda settings: layout)
        monkeypatch.setattr(agents, "default_runner", lambda: mac)
        monkeypatch.setattr(agents, "Cluster", lambda: FakeCluster(events))
        args = argparse.Namespace(with_web=False, dry_run=True)
        assert agents_command.run_install(args, prod) == 0
        out = capsys.readouterr().out
        assert "Plan (dry run, nothing changes):" in out and events == []
        mac.status = " M budgetapp/cli.py\0"  # the real upgrade refuses a dirty tree
        monkeypatch.setattr(agents, "upgrade_install", agents.upgrade_install)
        args.dry_run = False
        assert agents_command.run_install(args, prod) == 1
        assert "uncommitted changes (budgetapp/cli.py)" in capsys.readouterr().err

    def test_the_command_always_uses_production_settings(self, monkeypatch):
        seen = []
        monkeypatch.setattr(agents, "install_agents", lambda settings, **kw: seen.append(settings))
        test_settings = load_settings("test")
        args = argparse.Namespace(with_web=False, dry_run=True)
        assert agents_command.run_install(args, test_settings) == 0
        assert seen[0].is_production


class TestUninstall:
    def test_reverse_order_with_a_fast_postgres_stop_first(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=agents.AGENTS, events=events)
        agents.uninstall_agents(
            prod, layout=layout, runner=mac, cluster=FakeCluster(events), say=lambda line: None
        )
        assert events == [
            "bootout web",
            "bootout backup",
            "bootout sync",
            "cluster stop",
            "bootout postgres",
        ]
        assert agents.installed_agents(layout) == [] and not layout.wrapper_path.exists()

    def test_dry_run_changes_nothing(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=agents.AGENTS, events=events)
        said: list[str] = []
        lines = agents.uninstall_agents(
            prod,
            dry_run=True,
            layout=layout,
            runner=mac,
            cluster=FakeCluster(events),
            say=said.append,
        )
        assert events == [] and agents.installed_agents(layout) == list(agents.AGENTS)
        assert lines[0].startswith("launchctl bootout gui/501/local.budgetapp.web")
        assert "pg_ctl stop -m fast" in lines[-3]
        assert said[-1].startswith("Kept: the install")

    def test_nothing_installed(self, prod, layout):
        said: list[str] = []
        mac = Mac(layout)
        assert agents.uninstall_agents(prod, layout=layout, runner=mac, say=said.append) == []
        assert said == ["No budgetapp launchd agents are installed."]


# --------------------------------------------------------------------------- upgrade


class FakeDb:
    def __init__(self, events, revision="0003_f1_verification_fixes", error=None):
        self.events = events
        self.revision_value = revision
        self.error = error

    def backup(self, settings):
        self.events.append("backup")
        return Path("/backups/budgetapp-x.dump")

    @contextlib.contextmanager
    def lock(self, settings):
        self.events.append("lock")
        yield
        self.events.append("unlock")

    def revision(self, settings):
        if self.error:
            raise self.error
        return self.revision_value


def upgrade(prod, layout, mac, fake_db, **kwargs):
    return agents.upgrade_install(
        prod,
        layout=layout,
        runner=mac,
        backup=fake_db.backup,
        lock=fake_db.lock,
        revision=fake_db.revision,
        say=lambda line: None,
        **kwargs,
    )


class TestUpgrade:
    def test_the_whole_sequence(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=("postgres", "sync", "backup", "web"), events=events)
        result = upgrade(prod, layout, mac, FakeDb(events))
        assert events == [
            "git archive",  # before anything stops: a failed export changes nothing
            "bootout web",
            "bootout backup",
            "bootout sync",
            "backup",
            "venv",
            "pip -r",
            "pip --no-deps",
            "lock",
            "migrate",
            "unlock",
            "bootstrap sync",
            "bootstrap backup",
            "bootstrap web",
        ]
        assert result.commit == COMMIT and result.stopped == ("web", "backup", "sync")
        stamp = agents.read_stamp(layout)
        assert stamp.commit == COMMIT and stamp.python == "3.14.2"
        assert layout.stamp_path.stat().st_mode & 0o777 == 0o600
        pips = [argv for argv, _ in mac.calls if "pip" in argv]
        assert all("-e" not in argv and "--editable" not in argv for argv in pips)
        # Both pip steps read the exported commit, never the working tree.
        source = Path(pips[1][-1])
        assert pips[0][-2:] == ["-r", str(source / "requirements.txt")]
        assert pips[1][-3:-1] == ["--no-deps", "--force-reinstall"]
        assert source.name == "source" and layout.repo_dir not in (source, *source.parents)
        assert not source.exists()  # the temporary export is gone afterwards
        [(migrate, kwargs)] = [(a, k) for a, k in mac.calls if "migrate" in a]
        python = str(layout.install_dir / "bin" / "python")
        assert migrate == [python, "-I", "-m", "budgetapp", "migrate", "--env", "production"]
        assert "BUDGETAPP_ENV" not in kwargs["env"] and "BUDGETAPP_REPO_DIR" not in kwargs["env"]
        assert kwargs["env"]["BUDGETAPP_DATA_DIR"] == str(layout.data_dir)
        assert kwargs["cwd"] == str(layout.data_dir)
        venv = next(a for a, _ in mac.calls if "venv" in a)
        assert venv == ["/fake/python3.14", "-m", "venv", "--clear", str(layout.install_dir)]

    def test_a_working_install_is_reused(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, events=events)
        layout.venv_python.parent.mkdir(parents=True)
        layout.venv_python.write_text("")
        upgrade(prod, layout, mac, FakeDb(events))
        assert "venv" not in events and events[:2] == ["git archive", "backup"]

    def test_a_broken_install_is_rebuilt(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, events=events)
        mac.venv_runs = False
        layout.venv_python.parent.mkdir(parents=True)
        layout.venv_python.write_text("")
        upgrade(prod, layout, mac, FakeDb(events))
        assert events[:4] == ["git archive", "backup", "venv", "pip -r"]
        assert agents.read_stamp(layout).python == "3.14.2"

    @pytest.mark.parametrize(
        ("status", "dirty"),
        [
            (" M budgetapp/cli.py\0", ["budgetapp/cli.py"]),
            ("M  requirements.txt\0", ["requirements.txt"]),
            ("?? budgetapp/new_module.py\0", ["budgetapp/new_module.py"]),
            ("R  budgetapp/new.py\0budgetapp/old.py\0?? notes.md\0", ["budgetapp/new.py"]),
            ("?? Amex Budget Tracker — Requirements & Workplan.md\0?? scratch/x.txt\0", []),
        ],
    )
    def test_which_changes_make_the_tree_dirty(self, layout, status, dirty):
        assert agents.dirty_paths(layout, Mac(layout, status=status)) == dirty

    def test_a_dirty_tree_is_refused_before_anything_stops(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, loaded=("sync",), status=" M budgetapp/db.py\0", events=events)
        with pytest.raises(agents.DirtyTreeError, match="uncommitted changes"):
            upgrade(prod, layout, mac, FakeDb(events))
        assert events == []

    def test_a_database_from_newer_code_is_refused(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, loaded=("sync",), events=events)
        with pytest.raises(agents.AgentError, match="doesn't know"):
            upgrade(prod, layout, mac, FakeDb(events, revision="9999_from_the_future"))
        assert events == []

    def test_an_unreachable_database_is_refused(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, events=events)
        with pytest.raises(agents.AgentError, match="can't be read.*./run db start"):
            upgrade(prod, layout, mac, FakeDb(events, error=OSError("connection refused")))
        assert events == []

    def test_a_failure_restarts_the_agents_and_keeps_the_old_stamp(self, prod, layout):
        install_plists(layout)
        agents.write_stamp(layout, OLD_COMMIT, "3.14.2")
        events: list[str] = []
        mac = Mac(layout, loaded=("sync", "backup"), events=events)
        mac.fail_pip = True
        with pytest.raises(agents.AgentError, match="pip could not install"):
            upgrade(prod, layout, mac, FakeDb(events))
        assert events[-2:] == ["bootstrap sync", "bootstrap backup"]
        assert agents.read_stamp(layout).commit == OLD_COMMIT
        assert "migrate" not in events

    def test_restart_false_leaves_agents_stopped(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=("sync",), events=events)
        upgrade(prod, layout, mac, FakeDb(events), restart=False)
        assert "bootstrap sync" not in events and "sync" not in mac.loaded

    def test_only_production(self, layout):
        with pytest.raises(agents.AgentError, match="production"):
            agents.upgrade_install(load_settings("test"), layout=layout, runner=Mac(layout))

    def test_a_failure_restarts_the_agents_even_for_a_caller_that_reloads_them(self, prod, layout):
        """``restart=False`` (install-agents) only means "after a successful upgrade"."""
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=("sync", "backup"), events=events)
        mac.fail_pip = True
        with pytest.raises(agents.AgentError, match="pip could not install"):
            upgrade(prod, layout, mac, FakeDb(events), restart=False)
        assert events[-2:] == ["bootstrap sync", "bootstrap backup"]
        assert mac.loaded == {"sync", "backup"}

    def test_a_restart_that_fails_never_hides_the_upgrade_error(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=("sync", "backup"), events=events)
        mac.fail_pip = True
        mac.fail_bootstrap = {"sync"}
        fake_db = FakeDb(events)
        said: list[str] = []
        with pytest.raises(agents.AgentError, match="pip could not install"):
            agents.upgrade_install(
                prod,
                layout=layout,
                runner=mac,
                backup=fake_db.backup,
                lock=fake_db.lock,
                revision=fake_db.revision,
                say=said.append,
            )
        assert mac.loaded == {"backup"}
        assert any(line.startswith("Could not start the sync agent(s)") for line in said)
        assert "Started the backup agent(s) again." in said

    def test_an_export_that_fails_changes_nothing(self, prod, layout):
        events: list[str] = []
        mac = Mac(layout, loaded=("sync",), events=events)

        def runner(argv, **kwargs):
            if "archive" in argv:
                return subprocess.CompletedProcess(argv, 128, "", "fatal: not a valid object")
            return mac(argv, **kwargs)

        fake_db = FakeDb(events)
        with pytest.raises(agents.AgentError, match="Could not export commit 0123456789ab"):
            agents.upgrade_install(
                prod,
                layout=layout,
                runner=runner,
                launchctl=agents.Launchctl(501, mac),
                backup=fake_db.backup,
                lock=fake_db.lock,
                revision=fake_db.revision,
                say=lambda line: None,
            )
        assert events == [] and mac.loaded == {"sync"}


def git_repo(path: Path, files: dict[str, str]) -> str:
    """A real git repository with ``files`` committed (isolated from the user's git config);
    returns the commit."""
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    env = {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "HOME": str(path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    git = ["git", "-C", str(path)]
    subprocess.run(["git", "init", "-q", str(path)], env=env, check=True)
    subprocess.run([*git, "add", "--", *files], env=env, check=True)
    subprocess.run(
        [*git, "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
        + ["commit", "-q", "--no-verify", "--no-gpg-sign", "-m", "test"],
        env=env,
        check=True,
    )
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], env=env, check=True, capture_output=True, text=True
    )
    return head.stdout.strip()


class TestBuildFromTheCommit:
    """Review minor: pip built the install from the working tree after minutes of dependency
    installs, so a file saved meanwhile reached production under the clean commit's stamp."""

    def test_a_file_saved_during_the_upgrade_never_reaches_the_install(self, prod, tmp_path):
        repo = tmp_path / "repo"
        head = git_repo(repo, {**TREE, "budgetapp/__init__.py": 'VERSION = "committed"\n'})
        layout = agents.Layout.for_settings(
            prod, home=tmp_path / "home", repo_dir=repo, uid=501, python="/fake/python3.14"
        )
        events: list[str] = []
        mac = Mac(layout, events=events, commit=head)
        seen: dict[str, object] = {}

        def runner(argv, **kwargs):
            if Path(argv[0]).name == "git":
                return subprocess.run(argv, **kwargs)  # the real repository
            if "pip" in argv and "-r" in argv:  # dependencies install for minutes; a file is saved
                (repo / "budgetapp" / "__init__.py").write_text('VERSION = "edited"\n')
                (repo / "budgetapp" / "extra.py").write_text("NEW = 1\n")
                seen["requirements"] = Path(argv[argv.index("-r") + 1]).read_text()
            if "pip" in argv and "--no-deps" in argv:
                source = Path(argv[-1])
                seen["source"] = source
                seen["installed"] = (source / "budgetapp" / "__init__.py").read_text()
                seen["extra"] = (source / "budgetapp" / "extra.py").exists()
            return mac(argv, **kwargs)

        fake_db = FakeDb(events)
        result = agents.upgrade_install(
            prod,
            layout=layout,
            runner=runner,
            launchctl=agents.Launchctl(501, mac),
            backup=fake_db.backup,
            lock=fake_db.lock,
            revision=fake_db.revision,
            say=lambda line: None,
        )
        assert seen["installed"] == 'VERSION = "committed"\n' and seen["extra"] is False
        assert seen["requirements"] == TREE["requirements.txt"]
        assert result.commit == head and agents.read_stamp(layout).commit == head
        assert not Path(seen["source"]).exists()  # the private export is removed
        # The edit is still in the checkout (for the next upgrade to refuse), not in production.
        assert set(agents.dirty_paths(layout, runner)) == {
            "budgetapp/__init__.py",
            "budgetapp/extra.py",
        }


class TestAgentController:
    def test_one_agent_that_cannot_start_does_not_keep_the_others_down(self, layout):
        install_plists(layout)
        mac = Mac(layout)
        mac.fail_bootstrap = {"sync"}
        controller = agents.AgentController(layout, agents.Launchctl(501, mac))
        with pytest.raises(agents.AgentStartError, match="Could not start the sync agent") as info:
            controller.start_agents(["web", "sync", "backup"])
        assert info.value.started == ("backup", "web") and info.value.failed == ("sync",)
        assert mac.loaded == {"backup", "web"}


class TestInstallFailure:
    """Review major: a failed ./run install-agents left the sync, backup and web agents booted
    out, so syncs and nightly backups stopped, and so did the alerts that would report it."""

    @staticmethod
    def real_upgrade(events):
        fake_db = FakeDb(events)
        upgrade_install = agents.upgrade_install  # the real one, even once it is patched

        def run(settings, **kwargs):
            return upgrade_install(
                settings,
                backup=fake_db.backup,
                lock=fake_db.lock,
                revision=fake_db.revision,
                **kwargs,
            )

        return run

    @staticmethod
    def install(prod, layout, mac, events, said, **kwargs):
        return agents.install_agents(
            prod,
            layout=layout,
            runner=mac,
            cluster=kwargs.pop("cluster", None) or FakeCluster(events),
            say=said.append,
            **kwargs,
        )

    @pytest.mark.parametrize("with_web", [False, True])
    def test_a_failed_upgrade_leaves_every_agent_as_it_was(self, prod, layout, with_web):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=agents.AGENTS, events=events)
        mac.fail_pip = True  # offline, PyPI down…
        said: list[str] = []
        with pytest.raises(agents.AgentError, match="pip could not install"):
            self.install(
                prod,
                layout,
                mac,
                events,
                said,
                upgrade=self.real_upgrade(events),
                with_web=with_web,
            )
        assert mac.loaded == set(agents.AGENTS)
        assert "Started the sync, backup, web agent(s) again." in said
        assert not any(line.startswith("NOT running") for line in said)

    def test_a_failure_after_the_upgrade_loads_the_agents_again(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=agents.AGENTS, events=events)
        mac.fail_bootstrap_once = {"sync"}  # launchd refused it this once
        said: list[str] = []
        with pytest.raises(agents.AgentError, match="bootstrap local.budgetapp.sync failed"):
            self.install(
                prod, layout, mac, events, said, upgrade=self.real_upgrade(events), with_web=True
            )
        assert mac.loaded == set(agents.AGENTS)
        assert (
            said[-1] == "Started the sync, backup, web agent(s) again (loaded before the install)."
        )

    def test_an_agent_that_still_cannot_load_is_named(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=agents.AGENTS, events=events)
        mac.fail_bootstrap = {"sync"}
        said: list[str] = []
        with pytest.raises(agents.AgentError, match="bootstrap local.budgetapp.sync failed"):
            self.install(prod, layout, mac, events, said, upgrade=self.real_upgrade(events))
        assert mac.loaded == {"postgres", "backup"}  # web: removed on purpose (no --with-web)
        assert not layout.plist_path("web").exists()
        assert said[-2] == "Started the backup agent(s) again (loaded before the install)."
        assert said[-1].startswith(
            "NOT running now: the sync agent(s), which ran before the install, so scheduled "
            "syncs and backups may be off."
        )
        assert said[-1].endswith("then run ./run install-agents again.")

    def test_postgres_is_started_again_when_the_install_stopped_it(self, prod, layout):
        """First install: PostgreSQL ran under pg_ctl; the hand-over stopped it, then launchd
        refused the new agent."""
        events: list[str] = []
        mac = Mac(layout, events=events)
        mac.fail_bootstrap = {"postgres"}
        said: list[str] = []
        with pytest.raises(agents.AgentError, match="bootstrap local.budgetapp.postgres failed"):
            self.install(prod, layout, mac, events, said, upgrade=lambda settings, **kw: None)
        assert events[-2:] == ["bootstrap postgres", "cluster start"]
        assert "Started PostgreSQL again (it was running before the install)." in said
        assert mac.loaded == set()

    def test_a_loaded_postgres_agent_is_never_doubled_by_pg_ctl(self, prod, layout):
        """PostgreSQL that launchd loaded but that didn't come up stays launchd's to start."""
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=("postgres",), events=events)
        cluster = FakeCluster(events, ready=False)
        said: list[str] = []
        with pytest.raises(agents.AgentError, match="did not start under launchd"):
            self.install(
                prod, layout, mac, events, said, upgrade=lambda s, **kw: None, cluster=cluster
            )
        assert "postgres" in mac.loaded and events.count("cluster start") == 0

    def test_a_failure_before_anything_stopped_changes_nothing(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=agents.AGENTS, status=" M budgetapp/db.py\0", events=events)
        said: list[str] = []
        with pytest.raises(agents.DirtyTreeError):
            self.install(prod, layout, mac, events, said, upgrade=self.real_upgrade(events))
        assert mac.loaded == set(agents.AGENTS) and mac.launchd_verbs() == []
        assert not any("again" in line or "NOT running" in line for line in said)

    def test_ctrl_c_during_the_install_loads_the_agents_again(self, prod, layout):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=agents.AGENTS, events=events)

        def interrupted(settings, **kwargs):  # the upgrade had stopped the app agents
            mac.loaded.difference_update({"sync", "backup", "web"})
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            self.install(prod, layout, mac, events, [], upgrade=interrupted, with_web=True)
        assert mac.loaded == set(agents.AGENTS)

    def test_the_command_says_what_is_still_stopped(self, monkeypatch, prod, layout, capsys):
        install_plists(layout)
        events: list[str] = []
        mac = Mac(layout, loaded=agents.AGENTS, events=events)
        mac.fail_bootstrap = {"sync", "backup"}
        monkeypatch.setattr(agents, "default_layout", lambda settings: layout)
        monkeypatch.setattr(agents, "default_runner", lambda: mac)
        monkeypatch.setattr(agents, "Cluster", lambda: FakeCluster(events))
        monkeypatch.setattr(agents, "upgrade_install", self.real_upgrade(events))
        args = argparse.Namespace(with_web=True, dry_run=False)
        assert agents_command.run_install(args, prod) == 1
        out, err = capsys.readouterr()
        assert "Started the web agent(s) again (loaded before the install)." in out
        assert "NOT running now: the sync, backup agent(s)" in out
        assert "error: launchctl bootstrap local.budgetapp.sync failed" in err


class TestStamp:
    def test_round_trip_and_damage(self, layout):
        assert agents.read_stamp(layout) is None
        stamp = agents.write_stamp(layout, COMMIT, "3.14.2")
        assert agents.read_stamp(layout) == stamp and stamp.short == COMMIT[:12]
        layout.stamp_path.write_text("{not json")
        assert agents.read_stamp(layout) is None
        layout.stamp_path.write_text('{"commit": "x"}')
        assert agents.read_stamp(layout) is None


# --------------------------------------------------------------------------- ./run live


class Child:
    def __init__(self, code=0, interrupt=False):
        self.code = code
        self.interrupt = interrupt
        self.waits = 0

    def wait(self):
        self.waits += 1
        if self.interrupt and self.waits == 1:
            raise KeyboardInterrupt
        return self.code

    def poll(self):
        return None


class TestLaunchProduction:
    @pytest.fixture
    def installed(self, layout):
        layout.venv_python.parent.mkdir(parents=True)
        layout.venv_python.write_text("")
        agents.write_stamp(layout, COMMIT, "3.14.2")
        return layout

    def launch(self, prod, layout, mac, *, revision=None, ready=True, upgrade=None, popen=None):
        opened: list[str] = []
        said: list[str] = []
        code = agents.launch_production(
            prod,
            layout=layout,
            runner=mac,
            cluster=FakeCluster(mac.events),
            upgrade=upgrade,
            revision=revision or (lambda s: code_head()),
            ready=lambda url: ready,
            open_browser=opened.append,
            popen=popen or (lambda *a, **k: pytest.fail("no serve expected")),
            say=said.append,
        )
        return code, opened, said

    def test_an_up_to_date_install_opens_the_web_agent(self, prod, installed):
        install_plists(installed, ["web"])
        mac = Mac(installed, loaded=("web",))
        code, opened, said = self.launch(prod, installed, mac)
        assert code == 0 and opened == ["http://127.0.0.1:8765"]
        assert mac.launchd_verbs() == ["kickstart web"]
        assert "Budget Tracker (live) is open at http://127.0.0.1:8765" in said[-1]

    def test_a_web_agent_that_is_not_loaded_is_bootstrapped(self, prod, installed):
        install_plists(installed, ["web"])
        mac = Mac(installed)
        code, opened, _ = self.launch(prod, installed, mac)
        assert code == 0 and mac.launchd_verbs() == ["bootstrap web"]

    def test_a_web_agent_that_never_answers(self, prod, installed):
        install_plists(installed, ["web"])
        code, opened, said = self.launch(
            prod, installed, Mac(installed, loaded=("web",)), ready=False
        )
        assert code == 1 and opened == [] and "web.log" in said[-1]

    @pytest.mark.parametrize(
        ("commit", "revision", "reason"),
        [
            (OLD_COMMIT, None, "the code changed (fedcba987654 → 0123456789ab)"),
            (COMMIT, "0002_seed_defaults", "the database is at schema 0002_seed_defaults"),
        ],
    )
    def test_upgrades_first_when_needed(self, prod, installed, commit, revision, reason):
        agents.write_stamp(installed, commit, "3.14.2")
        install_plists(installed, ["web"])
        calls = []
        mac = Mac(installed, loaded=("web",))
        code, opened, said = self.launch(
            prod,
            installed,
            mac,
            revision=(lambda s: revision) if revision else None,
            upgrade=lambda settings, **kw: calls.append(kw),
        )
        assert code == 0 and len(calls) == 1 and calls[0]["layout"] == installed
        assert any(reason in line for line in said)

    def test_no_install_yet_means_upgrade(self, prod, layout):
        calls = []
        install_plists(layout, ["web"])
        code, _, said = self.launch(
            prod, layout, Mac(layout, loaded=("web",)), upgrade=lambda s, **kw: calls.append(kw)
        )
        assert calls and "isn't installed yet" in said[1]

    def test_an_unreadable_database_does_not_block_the_start(self, prod, installed):
        install_plists(installed, ["web"])

        def broken(settings):
            raise OSError("connection refused")

        code, _, said = self.launch(
            prod,
            installed,
            Mac(installed, loaded=("web",)),
            revision=broken,
            upgrade=lambda s, **kw: pytest.fail("no upgrade"),
        )
        assert code == 0

    def test_a_dirty_tree_stops_an_upgrade_with_a_message(self, prod, installed):
        agents.write_stamp(installed, OLD_COMMIT, "3.14.2")
        mac = Mac(installed, status=" M budgetapp/cli.py\0")
        code, _, said = self.launch(prod, installed, mac, upgrade=agents.upgrade_install)
        assert code == 1 and "uncommitted changes" in said[-1]

    def test_without_a_web_agent_it_serves_from_the_install(self, prod, installed):
        started = []
        child = Child(code=0)

        def popen(argv, **kwargs):
            started.append((argv, kwargs))
            return child

        code, opened, _ = self.launch(prod, installed, Mac(installed), popen=popen)
        assert code == 0
        [(argv, kwargs)] = started
        python = str(installed.install_dir / "bin" / "python")
        assert argv == [python, "-I", "-u", "-m", "budgetapp", "serve", "--env", "production"]
        assert kwargs["cwd"] == str(installed.data_dir)
        assert "BUDGETAPP_REPO_DIR" not in kwargs["env"]

    def test_ctrl_c_waits_for_the_server_to_stop(self, prod, installed):
        child = Child(code=0, interrupt=True)
        code, _, _ = self.launch(prod, installed, Mac(installed), popen=lambda *a, **k: child)
        assert code == 0 and child.waits == 2

    def test_runs_only_from_the_checkout(self, prod, installed):
        code, _, said = self.launch(prod, installed.replace(repo_dir=None), Mac(installed))
        assert code == 2 and "from the budgetapp folder" in said[0]


class TestEnsureDatabase:
    """./run live and install-agents start PostgreSQL through its agent when one is loaded."""

    def ensure(self, prod, layout, mac, cluster):
        return agents.ensure_database(prod, layout, agents.Launchctl(501, mac), cluster)

    def test_without_the_agent_pg_ctl_starts_it(self, prod, layout):
        events: list[str] = []
        cluster = FakeCluster(events, running=False)
        assert self.ensure(prod, layout, Mac(layout, events=events), cluster) == "already running"
        assert events == ["cluster start"]

    def test_a_plist_that_is_not_loaded_is_not_used(self, prod, layout):
        install_plists(layout, ["postgres"])
        events: list[str] = []
        self.ensure(prod, layout, Mac(layout, events=events), FakeCluster(events, running=False))
        assert events == ["cluster start"]

    def test_a_loaded_agent_is_kickstarted_never_pg_ctl(self, prod, layout):
        install_plists(layout, ["postgres"])
        events: list[str] = []
        mac = Mac(layout, loaded=("postgres",), events=events)
        said = self.ensure(prod, layout, mac, FakeCluster(events, running=False))
        assert said == "started by its launchd agent"
        assert events == ["kickstart postgres", "cluster wait"]

    def test_a_running_agent_is_left_alone(self, prod, layout):
        install_plists(layout, ["postgres"])
        events: list[str] = []
        mac = Mac(layout, loaded=("postgres",), events=events)
        said = self.ensure(prod, layout, mac, FakeCluster(events, running=True))
        assert said == "already running (launchd agent)" and events == []

    def test_an_agent_that_never_comes_up(self, prod, layout):
        install_plists(layout, ["postgres"])
        events: list[str] = []
        mac = Mac(layout, loaded=("postgres",), events=events)
        with pytest.raises(agents.AgentError, match="did not start under launchd"):
            self.ensure(prod, layout, mac, FakeCluster(events, running=False, ready=False))

    def test_live_uses_it(self, prod, layout):
        layout.venv_python.parent.mkdir(parents=True)
        layout.venv_python.write_text("")
        agents.write_stamp(layout, COMMIT, "3.14.2")
        install_plists(layout, ["postgres", "web"])
        mac = Mac(layout, loaded=("postgres", "web"))
        code = agents.launch_production(
            prod,
            layout=layout,
            runner=mac,
            cluster=FakeCluster(mac.events, running=False),
            revision=lambda s: code_head(),
            ready=lambda url: True,
            open_browser=lambda url: None,
            say=lambda line: None,
        )
        assert code == 0
        assert mac.events[:2] == ["kickstart postgres", "cluster wait"]
        assert "cluster start" not in mac.events
