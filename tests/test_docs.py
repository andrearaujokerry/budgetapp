"""The README and the user guides match the code (MNT-1).

Every ``./run`` command line the docs show parses with the real CLI, every relative link and
anchor resolves, the Markdown is well formed (tables, code fences, comments), and the ports,
paths, schedules, limits, defaults and Keychain names the guides quote are the code's own.
Nothing here runs a command: command lines are only parsed.
"""

from __future__ import annotations

import argparse
import inspect
import re
import shlex
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from budgetapp import cli, secrets
from budgetapp.commands import serve as serve_command
from budgetapp.config import load_settings
from budgetapp.models import get_app_settings
from budgetapp.services import agents, app_settings, backup, csv_import, doctor, linking, status
from budgetapp.web import templating
from budgetapp.web.routes import ALL_ROUTERS

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
GUIDES = sorted(p for p in (REPO / "docs").glob("*.md") if p.name != "ARCHITECTURE.md")
#: The user docs: what this test holds to the code most strictly.
USER_DOCS = (README, *GUIDES)
#: Developer docs: their command lines and links must work too.
DEV_DOCS = (
    REPO / "docs" / "ARCHITECTURE.md",
    REPO / "budgetapp" / "web" / "README.md",
    REPO / "budgetapp" / "web" / "templates" / "components" / "README.md",
    REPO / "budgetapp" / "devtools" / "README.md",
)
ALL_DOCS = (*USER_DOCS, *DEV_DOCS)
EXPECTED_GUIDES = {
    "setup.md",
    "going-live.md",
    "using-the-app.md",
    "reconnecting.md",
    "alerts.md",
    "maintenance.md",
    "security.md",
}


def _id(path: Path) -> str:
    return str(path.relative_to(REPO))


# --------------------------------------------------------------------------- Markdown helpers

FENCE = re.compile(r"^\s*(```|~~~)\s*([\w-]*)")
#: Fenced blocks that hold commands to type (``text`` blocks show program output instead).
SHELL_LANGUAGES = {"", "sh", "bash", "zsh", "console", "shell"}
INLINE_CODE = re.compile(r"(`+)(.+?)\1")
LINK = re.compile(r"!?\[(?:[^\]]|\][^(])*?\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def split_code(text: str, *, shell_only: bool = False) -> tuple[list[str], list[str]]:
    """``(prose_lines, code_block_lines)``: lines outside and inside fenced code blocks.

    ``shell_only`` keeps only the lines of blocks in a shell language (or none), not ``text``,
    ``python`` or ``sql`` blocks.
    """
    prose: list[str] = []
    code: list[str] = []
    language: str | None = None
    for line in text.splitlines():
        fence = FENCE.match(line)
        if fence:
            language = None if language is not None else fence.group(2).lower()
            continue
        if language is None:
            prose.append(line)
        elif not shell_only or language in SHELL_LANGUAGES:
            code.append(line)
    return prose, code


def code_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Every fenced code block as ``(language, lines)``."""
    blocks: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        fence = FENCE.match(line)
        if fence and current is None:
            current = []
            blocks.append((fence.group(2).lower(), current))
        elif fence:
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def inline_spans(lines: list[str]) -> Iterator[str]:
    for line in lines:
        for match in INLINE_CODE.finditer(line):
            yield match.group(2).strip()


def slugify(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links keep their text
    text = text.replace("`", "").replace("*", "").strip().lower()
    kept = []
    for ch in text:
        if ch in " -_" or unicodedata.category(ch)[0] in "LN":
            kept.append(ch)
    return "".join(kept).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    prose, _code = split_code(path.read_text())
    seen: dict[str, int] = {}
    found = set()
    for line in prose:
        match = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        slug = slugify(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def text_of(path: Path) -> str:
    """The prose of a doc with Markdown emphasis removed and lines joined (for phrase checks)."""
    prose, code = split_code(path.read_text())
    joined = " ".join(prose + code).replace("**", "").replace("\\|", "|")
    return re.sub(r"\s+", " ", joined)


# --------------------------------------------------------------------------- command lines

RUN = re.compile(r"(?:^|[\s;&(=])\./run(?![\w/.-])(.*)$")
PLACEHOLDER = re.compile(r"^(<[^>]+>|[A-Z][A-Z0-9_]*(-[A-Z0-9]+)*|…|\.\.\.)$")


def run_lines(path: Path) -> Iterator[str]:
    """Every ``./run …`` command line shown in code (fenced blocks and inline code)."""
    prose, code = split_code(path.read_text(), shell_only=True)
    candidates = [line.split(" #", 1)[0] for line in code]
    candidates += list(inline_spans(prose))
    for candidate in candidates:
        match = RUN.search(candidate.replace("\\|", "|"))
        if match:
            yield match.group(1).strip()


def subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("the CLI has no sub-commands")


PARSER = cli.build_parser()
COMMANDS = subparsers(PARSER)


def sample_value(action: argparse.Action | None, token: str) -> str:
    """A concrete value for a placeholder such as ``FILE``, ``N`` or ``<port>``."""
    if action is None:
        return "placeholder"
    if action.choices:
        return str(next(iter(action.choices)))
    if action.type in (int, float):
        return "8800" if "port" in action.dest else "1"
    if action.type is not None and action.type is not str:
        return "2026-01-15"  # the date options (--today, --since)
    return token.strip("<>").lower() or "placeholder"


def expand(rest: str) -> list[list[str]]:
    """The argv lists one documented command line stands for (``a|b`` alternatives expanded)."""
    rest = rest.replace("[", " ").replace("]", " ")
    try:
        tokens = shlex.split(rest)
    except ValueError:  # an unbalanced quote in prose: parse what is there
        tokens = rest.split()
    argvs: list[list[str]] = [[]]
    for token in tokens:
        if token in ("|", "/", "…", "..."):
            continue
        options = token.split("|") if "|" in token and not token.startswith("-") else [token]
        argvs = [argv + [option] for argv in argvs for option in options if option]
    return argvs


def to_argv(rest: str) -> list[list[str]] | None:
    """The concrete argv lists for ``./run <rest>``; ``None`` for a generic ``<command>``."""
    results = []
    for tokens in expand(rest):
        if not tokens:
            tokens = ["start"]
        elif tokens[0] == "live":
            tokens = ["start", "--env", "production", *tokens[1:]]
        if tokens[0].startswith("<"):
            return None
        command = next((t for t in tokens if not t.startswith("-")), None)
        sub = COMMANDS.get(command or "")
        actions = (
            {opt: a for a in sub._actions for opt in a.option_strings} if sub is not None else {}
        )
        positionals = [a for a in sub._actions if not a.option_strings] if sub is not None else []
        argv: list[str] = []
        previous: argparse.Action | None = None
        seen_command = False
        for token in tokens:
            if PLACEHOLDER.match(token):
                if previous is None and seen_command and positionals:
                    index = sum(1 for t in argv[argv.index(command) + 1 :] if not t.startswith("-"))
                    positional = positionals[min(index, len(positionals) - 1)]
                    token = sample_value(positional, token)
                else:
                    token = sample_value(previous, token)
            argv.append(token)
            if token == command:
                seen_command = True
            action = actions.get(token)
            previous = action if action is not None and action.nargs != 0 else None
        results.append(argv)
    return results


def documented_commands(paths: tuple[Path, ...]) -> list[tuple[str, str]]:
    """``(doc, rest)`` for every concrete command line (not a generic ``./run <command>``)."""
    return [
        (_id(path), rest) for path in paths for rest in run_lines(path) if to_argv(rest) is not None
    ]


@pytest.mark.parametrize(("doc", "rest"), documented_commands(ALL_DOCS))
def test_every_documented_command_line_parses(doc: str, rest: str, capsys) -> None:
    """``./run <rest>`` as shown in a doc is a real command with real options and values."""
    for argv in to_argv(rest) or []:
        try:
            args, extra = PARSER.parse_known_args(argv)
        except SystemExit:
            message = capsys.readouterr().err.strip().splitlines()[-1:]
            if len(argv) == 1 and "arguments are required" in "".join(message):
                continue  # names the command (e.g. "the `./run db` commands"), not a command line
            pytest.fail(f"{doc}: `./run {rest}` does not parse ({argv}): {message}")
        if extra:
            assert getattr(args, "accepts_extra", False), (
                f"{doc}: `./run {rest}` has options {args.command!r} doesn't know: {extra}"
            )


def test_the_command_line_checker_catches_mistakes(capsys) -> None:
    """Edge cases of the checker itself: a wrong option, a wrong value, a missing command."""

    def parses(rest: str) -> bool:
        for argv in to_argv(rest) or []:
            try:
                args, extra = PARSER.parse_known_args(argv)
            except SystemExit:
                capsys.readouterr()
                return False
            if extra and not getattr(args, "accepts_extra", False):
                return False
        return True

    assert parses("setup --keys-only")
    assert parses("")  # ./run = start
    assert parses("live")
    assert parses("demo create try --seed 4 --today 2026-10-15")
    assert parses("serve --demo <id> --port <port>")
    assert parses("db start|stop|status|psql|url")
    assert parses('restore "$HOME/Library/Application Support/x.dump" --env production')
    assert parses("test -k budgets -x")  # extra arguments go to pytest
    assert not parses("setup --key-only")
    assert not parses("backup --env staging")
    assert not parses("sandbox-link --user nobody")
    assert not parses("synchronise")
    assert not parses("import-csv FILE --dryrun")
    assert to_argv("<command> --help") is None


def test_the_readme_lists_every_command() -> None:
    """A new CLI command can't ship without a line in the README's command list."""
    mentioned = set()
    for rest in run_lines(README):
        for argv in to_argv(rest) or []:
            mentioned.add(next(t for t in argv if not t.startswith("-")))
    assert set(COMMANDS) - mentioned == set()


# --------------------------------------------------------------------------- links and anchors


def doc_links(paths: tuple[Path, ...]) -> list[tuple[str, str]]:
    found = []
    for path in paths:
        prose, _code = split_code(path.read_text())
        for line in prose:
            line = INLINE_CODE.sub("", line)
            found += [(_id(path), m.group(1)) for m in LINK.finditer(line)]
    return found


@pytest.mark.parametrize(("doc", "target"), doc_links(ALL_DOCS))
def test_every_link_resolves(doc: str, target: str) -> None:
    if re.match(r"^[a-z]+:", target):
        assert target.startswith(("https://", "http://127.0.0.1:", "mailto:")), target
        return
    source = REPO / doc
    file_part, _, anchor = target.partition("#")
    destination = (source.parent / file_part).resolve() if file_part else source
    assert destination.exists(), f"{doc}: {target} points at a missing file"
    if anchor:
        assert destination.suffix == ".md", f"{doc}: {target}: anchors only work in Markdown"
        assert anchor in anchors(destination), (
            f"{doc}: #{anchor} is not a heading of {_id(destination)}"
        )


def test_the_slugs_follow_github() -> None:
    assert slugify("One of your 10 Plaid connections, permanently") == (
        "one-of-your-10-plaid-connections-permanently"
    )
    assert slugify("2. Install the background agents") == "2-install-the-background-agents"
    assert slugify("Keep a copy of the access token (recommended)") == (
        "keep-a-copy-of-the-access-token-recommended"
    )
    assert slugify("`./run doctor`") == "run-doctor"
    assert slugify("Sandbox and production") == "sandbox-and-production"


def test_the_readme_links_every_guide() -> None:
    linked = {Path(t.partition("#")[0]).name for _doc, t in doc_links((README,))}
    assert {p.name for p in GUIDES} == EXPECTED_GUIDES
    assert linked >= EXPECTED_GUIDES


def test_the_readme_covers_setup_keys_and_reconnecting() -> None:
    """MNT-1: one command starts everything; the README covers setup, keys and reconnecting."""
    headings = anchors(README)
    assert {"quick-start", "setup-and-keys", "starting-the-app", "reconnecting"} <= headings
    text = text_of(README)
    assert "./run setup" in text and "./run setup --keys-only" in text
    assert "`./run` is the one command that starts everything" in README.read_text()


# --------------------------------------------------------------------------- well-formed Markdown


@pytest.mark.parametrize("path", ALL_DOCS, ids=_id)
def test_code_fences_and_comments_are_closed(path: Path) -> None:
    text = path.read_text()
    fences = [line for line in text.splitlines() if FENCE.match(line)]
    assert len(fences) % 2 == 0, "an unclosed ``` fence hides the rest of the page"
    assert text.count("<!--") == text.count("-->"), "an unclosed comment hides the rest"
    for comment in re.findall(r"<!--(.*?)-->", text, flags=re.S):
        assert "<!--" not in comment


def table_cells(row: str) -> int:
    body = INLINE_CODE.sub(lambda m: m.group(0).replace("\\|", ""), row.strip())
    body = body.replace("\\|", "")
    return body.strip("|").count("|") + 1


@pytest.mark.parametrize("path", ALL_DOCS, ids=_id)
def test_tables_have_the_same_columns_on_every_row(path: Path) -> None:
    prose, _code = split_code(path.read_text())
    table: list[str] = []
    for line in [*prose, ""]:
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        if table:
            widths = {table_cells(row) for row in table}
            assert len(widths) == 1, f"table starting {table[0][:60]!r} has rows of {widths} cells"
            assert re.match(r"^\|(\s*:?-{3,}:?\s*\|)+$", table[1].strip()), table[1]
            table = []


@pytest.mark.parametrize("path", USER_DOCS, ids=_id)
def test_no_secrets_in_the_docs(path: Path) -> None:
    """Docs show key names, never values: no token, client ID or secret shapes."""
    text = path.read_text()
    assert not re.search(r"(access|public|link)-(sandbox|production)-[0-9a-f]{8}", text)
    assert not re.search(r"\b[0-9a-f]{24}\b|\b[0-9a-f]{30}\b", text)
    assert "PGPASSWORD" not in text


# --------------------------------------------------------------------------- facts from the code


def test_ports_and_addresses() -> None:
    sandbox = load_settings("sandbox").web_port
    production = load_settings("production").web_port
    allowed = {sandbox, production, serve_command.DEMO_PORT}
    for path in USER_DOCS:
        for port in re.findall(r"127\.0\.0\.1:(\d+)", path.read_text()):
            assert int(port) in allowed | {load_settings("sandbox").pg_port}, (_id(path), port)
    readme = README.read_text()
    assert f"http://127.0.0.1:{sandbox}" in readme
    assert f"http://127.0.0.1:{production}" in readme
    assert f"port {load_settings('sandbox').pg_port}" in readme
    setup = text_of(REPO / "docs" / "setup.md")
    assert f"http://127.0.0.1:{serve_command.DEMO_PORT}" in setup
    for port in sorted(serve_command.RESERVED_PORTS):
        assert str(port) in setup


def test_page_addresses_are_real_routes() -> None:
    paths = {r.path for router in ALL_ROUTERS for r in router.routes if isinstance(r, APIRoute)}
    for doc in USER_DOCS:
        for path in re.findall(r"127\.0\.0\.1:\d+(/[\w/-]*)", doc.read_text()):
            assert path in paths, f"{_id(doc)} links {path}, which no route serves"
        for path in re.findall(r"\(`(/[\w/-]*)`\)", doc.read_text()):
            assert path in paths, f"{_id(doc)} names {path}, which no route serves"


def test_the_page_guide_covers_every_page_in_menu_order() -> None:
    guide = REPO / "docs" / "using-the-app.md"
    labels = [item["label"] for item in templating.NAV_ITEMS]
    headings = [m.group(1) for m in re.finditer(r"^## (.+)$", guide.read_text(), flags=re.M)]
    assert [h for h in headings if h in labels] == labels
    groups = []
    for item in templating.NAV_ITEMS:
        if item["group"] not in groups:
            groups.append(item["group"])
    text = guide.read_text()
    assert all(f"**{group}:**" in text for group in groups)


def test_paths_are_the_ones_the_code_uses() -> None:
    settings = load_settings("production")
    home = Path.home()
    data = "~/" + str(settings.data_dir.relative_to(home))
    logs = "~/" + str(settings.logs_dir.relative_to(home))
    layout = agents.Layout.for_settings(settings)
    launch_agents = "~/" + str(layout.launch_agents_dir.relative_to(home))
    known_under_data = {
        settings.pg_data_dir.name,
        settings.pg_socket_dir.name,
        settings.install_dir.name,
        settings.state_dir.name,
        settings.backups_dir.parent.name,
        layout.bin_dir.name,
    }
    for path in USER_DOCS:
        text = path.read_text().replace("$HOME/", "~/").replace("\\ ", " ")
        for found in re.findall(r"~/Library/[^`\"\n|)]*", text):
            found = found.rstrip(" .,:;")
            assert found.startswith((data, logs, launch_agents)), (_id(path), found)
            if found.startswith(data + "/"):
                first = found[len(data) + 1 :].split("/")[0]
                assert first in known_under_data, (_id(path), found)
    where = text_of(REPO / "docs" / "maintenance.md")
    for name in known_under_data:
        assert f"{data}/{name}" in where, name
    assert f"{data}/backups/production" in where
    assert f"{launch_agents}/{agents.LABEL_PREFIX}*.plist" in where


def test_logs_are_the_files_the_code_writes() -> None:
    text = text_of(REPO / "docs" / "maintenance.md")
    for name in ("postgres", "web-sandbox", "web-production", "sync-sandbox", "sync-production"):
        assert f"{name}.log" in text
    for agent in ("sync", "backup", "web"):
        assert f"{agent}.log" in text
    assert f"{agents.LABEL_PREFIX}postgres" in text


def test_agent_schedules() -> None:
    sync_times = [f"{h:02d}:{m:02d}" for h, m in agents.SYNC_SCHEDULE]
    backup_time = "{:02d}:{:02d}".format(*agents.BACKUP_SCHEDULE)
    phrase = ", ".join(sync_times[:-1]) + f" and {sync_times[-1]}"
    for name in ("going-live.md", "maintenance.md"):
        text = text_of(REPO / "docs" / name)
        assert phrase.replace(" and ", ", ") in text.replace(" and ", ", "), name
        assert backup_time in text, name
    assert set(agents.AGENTS) == {"postgres", "sync", "backup", "web"}
    maintenance = text_of(REPO / "docs" / "maintenance.md")
    for name in agents.AGENTS[1:3]:
        command = " ".join(
            agents.program_arguments(name, agents.Layout.for_settings(load_settings("production")))[
                5:
            ]
        )
        assert command in maintenance, command


def test_backup_numbers() -> None:
    text = text_of(REPO / "docs" / "maintenance.md")
    assert f"newest {backup.DEFAULT_KEEP} are kept" in text
    assert f"newest {backup.PRE_RESTORE_KEEP} safety copies" in text
    assert f"under {int(backup.DUE_AFTER.total_seconds() // 3600)} hours" in text
    assert f"more than {int(backup.STALE_AFTER.total_seconds() // 3600)} hours old" in text
    assert backup.PRE_RESTORE in text
    stamp = re.search(r"budgetapp-(\d{8}T\d{6}Z)\.dump", text)
    assert stamp and backup.DUMP_NAME.match(f"budgetapp-{stamp.group(1)}.dump")


def test_stale_and_consent_numbers() -> None:
    hours = int(status.STALE_AFTER.total_seconds() // 3600)
    for name in ("using-the-app.md", "alerts.md"):
        assert f"{hours} hours" in text_of(REPO / "docs" / name), name
    assert status.CONSENT_FALLBACK.days == 365  # "a year after you last linked or renewed"


def test_settings_defaults_and_limits(db) -> None:
    """The defaults the guides quote are the seeded app settings; the limits are the form's."""
    row = get_app_settings(db)
    alerts = text_of(REPO / "docs" / "alerts.md")
    pages = text_of(REPO / "docs" / "using-the-app.md")
    thresholds = ", ".join(str(t) for t in row.alert_thresholds)
    assert f"| {thresholds} |" in alerts
    assert f"{row.consent_reminder_days} days" in alerts
    assert f"default {row.statement_settle_days})" in pages
    assert f"{app_settings.SETTLE_MIN}–{app_settings.SETTLE_MAX}" in pages
    assert f"from {app_settings.THRESHOLD_MIN} to {app_settings.THRESHOLD_MAX}" in alerts
    assert f"({app_settings.REMINDER_MIN}–{app_settings.REMINDER_MAX})" in alerts
    words = ("zero", "one", "two", "three", "four", "five", "six")
    assert f"up to {words[app_settings.THRESHOLDS_MAX_COUNT]}" in alerts
    assert row.large_charge_threshold is None and "blank (off)" in alerts
    assert row.budget_alerts_enabled and row.sync_alerts_enabled
    assert row.include_pending_in_budgets and not row.ai_enabled


def test_keychain_names() -> None:
    """The Keychain table names exactly the accounts the code uses."""
    text = (REPO / "docs" / "setup.md").read_text()
    section = text.split("### What Keychain holds", 1)[1].split("\n## ", 1)[0]
    named = set(re.findall(r"`((?:plaid|db)\.[\w.<>]+)`", section))
    expected = {
        secrets.PLAID_CLIENT_ID,
        secrets.plaid_secret_key("sandbox"),
        secrets.plaid_secret_key("production"),
        secrets.PRODUCTION_ACCESS_TOKEN,
        secrets.link_in_progress_key(),
        secrets.sandbox_access_token_key("ITEM").replace("ITEM", "<id>"),
        secrets.SANDBOX_ITEM_INDEX,
        secrets.db_password_key("budgetapp_dev"),
        secrets.db_password_key("budgetapp_live"),
    }
    assert named == expected
    assert f"under the name `{secrets.SERVICE}`" in text
    going_live = (REPO / "docs" / "going-live.md").read_text()
    assert f"`{secrets.PRODUCTION_ACCESS_TOKEN}`" in going_live
    assert secrets.KEYCHAIN_FIX_COMMAND in text_of(REPO / "docs" / "reconnecting.md")


def test_the_link_checklist_is_the_apps() -> None:
    text = text_of(REPO / "docs" / "going-live.md")
    for _key, label, _detail, _href in linking.CHECKLIST:
        assert label in text, label
    assert "https://dashboard.plaid.com/activity/status/oauth-institutions" in text
    assert "This uses 1 of your 10 Plaid connections, permanently." in text


def test_the_csv_download_steps_are_the_apps() -> None:
    text = text_of(REPO / "docs" / "using-the-app.md")
    for phrase in ("Statements & Activity", "Custom Date Range", "Include all additional"):
        assert any(phrase in step for step in csv_import.DOWNLOAD_STEPS)
        assert phrase in text
    assert f"up to {csv_import.MAX_FILE_BYTES // 2**20} MB" in text


def doctor_sections() -> list[str]:
    source = inspect.getsource(doctor)
    return list(dict.fromkeys(re.findall(r'section = "([^"]+)"', source)))


def test_the_doctor_section_table_lists_every_section() -> None:
    text = (REPO / "docs" / "maintenance.md").read_text()
    table = text.split("| Section | What it checks |", 1)[1].split("\n\n", 1)[0]
    listed = re.findall(r"^\| ([^|]+?) \|", table, flags=re.M)
    assert [s for s in listed if not s.startswith("-")] == doctor_sections()


def test_the_doctor_sample_reads_like_doctor() -> None:
    blocks = code_blocks((REPO / "docs" / "maintenance.md").read_text())
    sample = next(lines for _lang, lines in blocks if lines[0].startswith("Budget Tracker doctor"))
    summary = next(line for line in sample if re.match(r"^(All good|Problems found): ", line))
    counts = dict(
        (label, int(n))
        for n, label in re.findall(r"(\d+) (passed|warning\(s\)|failed|info)", summary)
    )
    assert summary.startswith("All good: " if counts["failed"] == 0 else "Problems found: ")
    report = doctor.Report("production", "budgetapp", ())
    assert doctor.format_report(report).splitlines()[0] == sample[0]
    for line in sample:
        if line and not line.startswith(" ") and line != sample[0] and line != summary:
            assert line in doctor_sections(), line
        status_line = re.match(r"^  (\w+)  ", line)
        if status_line:
            assert status_line.group(1) in doctor.STATUSES


def test_recheck_markers_name_a_wave_two_package() -> None:
    """Sections written before DASH and ALR merged are marked for the integrator."""
    for path in USER_DOCS:
        for marker in re.findall(r"<!-- RECHECK\((\w+)\)", path.read_text()):
            assert marker in ("DASH", "ALR"), (_id(path), marker)
