#!/usr/bin/env python3
"""Decide whether a deploy actually landed — without believing the easy signals.

Every check here exists because the obvious version of it once passed while
production was broken.

``systemctl is-active`` is the worst offender. A unit with ``Restart=always``
that crashes on startup spends most of its life *between* restarts, and in that
window ``is-active`` prints ``active``. On 2026-07-27 that reported a healthy
``talentping-beat`` for seventeen hours while every scheduled job — follow-ups,
inbox polling, autopilot — silently did not run. The honest question is not "is
it up right now?" but "has it stayed up?", and the answer is in ``NRestarts``
and a ``MainPID`` that does not move between two samples.

The frontend has the mirror-image trap. Checking that the *old* asset URL 404s
looks like the natural test and is wrong: Cloudflare serves the deleted file
from its edge for up to four hours, so a correct deploy fails the check. The
positive signal — the hash the live ``index.html`` references matches the hash
in the bundle just built — has no such failure mode.

The env-file check is the third. ``/opt/TalentPing/.env`` is read by systemd,
which takes ``KEY=value`` to end of line, but the documented recipe for one-off
scripts sources it with ``.``, and bash word-splits. An unquoted value with a
space in it silently becomes its first word.

Everything above is a pure function of text so it can be tested without a
server; ``main`` is the thin part that goes and gets the text.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Units whose death is invisible from the outside. The API failing is obvious
# within a minute; beat failing looks exactly like "nothing happened today".
SERVICES = ("talentping-api", "talentping-worker", "talentping-beat")

# How long a crash-looping unit needs to betray itself. Restart=always with the
# default RestartSec=100ms means a unit that cannot start will have restarted
# many times over well inside this window.
SETTLE_SECONDS = 30


@dataclass
class Verdict:
    """Whether one thing is healthy, and what to say if it is not."""

    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'}  {self.name}: {self.detail}"


@dataclass
class Report:
    verdicts: list[Verdict] = field(default_factory=list)

    def add(self, verdict: Verdict) -> Verdict:
        self.verdicts.append(verdict)
        return verdict

    @property
    def ok(self) -> bool:
        return all(v.ok for v in self.verdicts)

    def text(self) -> str:
        return "\n".join(v.line() for v in self.verdicts)


# --------------------------------------------------------------------------
# systemd
# --------------------------------------------------------------------------


def parse_properties(text: str) -> dict[str, str]:
    """Parse ``systemctl show`` output into a dict.

    Values may contain ``=``; only the first one separates. Blank and malformed
    lines are dropped rather than raising, because this parses the output of a
    command that may have failed, and a confusing verdict beats a traceback.
    """
    properties: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        properties[key.strip()] = value.strip()
    return properties


def _int(properties: dict[str, str], key: str) -> int:
    try:
        return int(properties.get(key, "0"))
    except ValueError:
        return 0


def service_verdict(
    name: str,
    before: dict[str, str],
    after: dict[str, str],
    later: dict[str, str] | None = None,
) -> Verdict:
    """Is *name* actually up, judged across the restart rather than at a moment?

    *before* is read before the deploy restarts anything, *after* once the
    restart returns, and *later* after the settle window. ``NRestarts`` counts
    only automatic restarts — an operator's ``systemctl restart`` does not
    increment it — so any growth across the deploy is the unit restarting
    itself, which is the crash loop.
    """
    active = after.get("ActiveState", "unknown")
    sub = after.get("SubState", "unknown")
    main_pid = _int(after, "MainPID")

    if main_pid == 0:
        return Verdict(name, False, f"no main process (ActiveState={active})")
    if active != "active":
        return Verdict(name, False, f"ActiveState={active} SubState={sub}")

    baseline = _int(before, "NRestarts")
    settled = later if later is not None else after
    restarts = _int(settled, "NRestarts")
    if restarts > baseline:
        return Verdict(
            name,
            False,
            f"crash-looping: NRestarts {baseline} -> {restarts} across the deploy. "
            f"`systemctl is-active` will still say active between restarts; read "
            f"`journalctl -u {name} -n 50` for the reason.",
        )

    settled_pid = _int(settled, "MainPID")
    if settled_pid == 0:
        return Verdict(name, False, f"died within {SETTLE_SECONDS}s of starting")
    if settled_pid != main_pid:
        return Verdict(
            name,
            False,
            f"main process moved {main_pid} -> {settled_pid} within "
            f"{SETTLE_SECONDS}s: the unit is restarting itself",
        )

    return Verdict(name, True, f"active, pid {settled_pid} stable, NRestarts={restarts}")


# --------------------------------------------------------------------------
# systemd unit configuration
# --------------------------------------------------------------------------

# systemd prints durations either as microseconds or as a formatted string
# ("1min 30s", "100ms"), depending on the property and the version. Both forms
# reach this parser, so it accepts both rather than assuming the one this box
# happens to print today.
_DURATION_UNITS = {
    "us": 1,
    "usec": 1,
    "ms": 1_000,
    "msec": 1_000,
    "s": 1_000_000,
    "sec": 1_000_000,
    "second": 1_000_000,
    "seconds": 1_000_000,
    "min": 60_000_000,
    "minute": 60_000_000,
    "minutes": 60_000_000,
    "h": 3_600_000_000,
    "hour": 3_600_000_000,
    "hours": 3_600_000_000,
}

_DURATION_PART = re.compile(r"(\d+)\s*([a-z]+)")


def parse_duration_usec(value: str) -> int | None:
    """A systemd duration in microseconds, or ``None`` for infinity/unparseable.

    ``infinity`` is not a large number here, it is a different thing — a stop
    timeout of infinity means a unit that will not die never gets killed — so it
    is returned as ``None`` rather than as ``sys.maxsize``, which would quietly
    pass a "long enough?" comparison.
    """
    value = value.strip().lower()
    if not value or value in {"infinity", "0"}:
        return None
    if value.isdigit():
        return int(value)

    total = 0
    matched = False
    for amount, unit in _DURATION_PART.findall(value):
        if unit not in _DURATION_UNITS:
            return None
        total += int(amount) * _DURATION_UNITS[unit]
        matched = True
    return total if matched else None


# Below this, systemd SIGKILLs a unit that is still shutting down. The API's
# graceful stop has to outlive the longest in-flight request, and a route that
# calls a model can hold for up to 60 seconds per provider in the chain.
MIN_STOP_TIMEOUT_USEC = 30 * 1_000_000


def unit_verdicts(name: str, properties: dict[str, str]) -> list[Verdict]:
    """Audit one unit's configuration — restart policy, limits, logging.

    Advisory, and separated from the pass/fail of a deploy on purpose: these
    read the running configuration on the server, which this repository does not
    own and cannot see except through ``systemctl show``. A finding here is
    something to go and change deliberately, not a reason to fail a deploy that
    is otherwise healthy.
    """
    verdicts: list[Verdict] = []

    def add(label: str, ok: bool, detail: str) -> None:
        verdicts.append(Verdict(f"{name} {label}", ok, detail))

    restart = properties.get("Restart", "no")
    add(
        "Restart",
        restart in {"always", "on-failure", "on-abnormal"},
        f"Restart={restart}"
        + ("" if restart != "no" else " — a crash ends the unit until someone notices"),
    )

    # A crash loop that trips the start limit stops being restarted, and the
    # unit then sits in `failed` looking like a one-off rather than a loop.
    burst = properties.get("StartLimitBurst")
    if burst is not None:
        add("StartLimitBurst", True, f"gives up after {burst} restarts in the interval")

    stop_timeout = properties.get("TimeoutStopUSec", "")
    parsed = parse_duration_usec(stop_timeout)
    if parsed is None:
        add(
            "TimeoutStopSec",
            False,
            f"TimeoutStopUSec={stop_timeout or 'unset'} — with no deadline a unit "
            f"that will not stop hangs the deploy instead of being killed",
        )
    elif parsed < MIN_STOP_TIMEOUT_USEC:
        add(
            "TimeoutStopSec",
            False,
            f"{parsed // 1_000_000}s is shorter than a slow request; in-flight work "
            f"is SIGKILLed mid-response on every deploy",
        )
    else:
        add("TimeoutStopSec", True, f"{parsed // 1_000_000}s before SIGKILL")

    kill_signal = properties.get("KillSignal", "SIGTERM")
    add(
        "KillSignal",
        kill_signal == "SIGTERM",
        f"KillSignal={kill_signal}"
        + (
            ""
            if kill_signal == "SIGTERM"
            else " — uvicorn and celery only shut down gracefully on SIGTERM"
        ),
    )

    # The box is shared with two other products. An unbounded unit does not fail
    # alone; it takes the neighbours down with it.
    memory_max = properties.get("MemoryMax", "infinity")
    add(
        "MemoryMax",
        memory_max not in {"infinity", ""},
        f"MemoryMax={memory_max}"
        + (
            ""
            if memory_max not in {"infinity", ""}
            else " — unbounded, and this box also runs N409 and USTradingBot"
        ),
    )

    for stream in ("StandardOutput", "StandardError"):
        value = properties.get(stream, "")
        add(
            stream,
            value in {"journal", "inherit", "journal+console"},
            f"{stream}={value or 'unset'}"
            + ("" if value else " — logs are going somewhere journalctl will not find"),
        )

    return verdicts


# --------------------------------------------------------------------------
# frontend
# --------------------------------------------------------------------------

_ASSET = re.compile(r"""["'(]/?(assets/[A-Za-z0-9._-]+\.(?:js|css))""")


def asset_paths(index_html: str) -> set[str]:
    """Every hashed bundle an ``index.html`` references.

    Vite names are content-addressed, so this set *is* the identity of a build.
    """
    return {match.group(1) for match in _ASSET.finditer(index_html)}


def frontend_verdict(built_index: str, served_index: str) -> Verdict:
    """Does the live site reference the bundle that was just built?

    Compares what ``index.html`` points at, not whether old files are gone.
    Cloudflare keeps serving deleted, content-addressed assets from its edge for
    hours; that is cache behaviour, not a failed deploy, and a check built on it
    fails when everything is fine.
    """
    built = asset_paths(built_index)
    served = asset_paths(served_index)

    if not built:
        return Verdict(
            "frontend", False, "the local build references no hashed assets — did "
            "`npm run build` run?"
        )
    if not served:
        return Verdict(
            "frontend", False, "the served index.html references no hashed assets"
        )
    if served == built:
        return Verdict("frontend", True, f"serving the new bundle ({len(built)} assets)")

    stale = served - built
    return Verdict(
        "frontend",
        False,
        f"still serving the old bundle: {sorted(stale)[:3]} are not in this build. "
        f"rsync frontend/dist/ separately — a repo sync that excludes dist/ leaves "
        f"the UI untouched.",
    )


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------


def migration_verdict(local_revisions: set[str], deployed_revisions: set[str]) -> Verdict:
    """Did the migration *files* reach the server before ``alembic upgrade``?

    ``alembic upgrade head`` reports success having applied nothing when the new
    revision files were never shipped — it upgrades to the head it can see. A
    narrow ``rsync backend/app/`` does exactly that, because the versions live in
    ``backend/alembic/``, outside ``app/``. Confirmed 2026-07-30, when prod ran
    new code against two tables that did not exist.
    """
    missing = local_revisions - deployed_revisions
    if missing:
        return Verdict(
            "migrations",
            False,
            f"{len(missing)} revision file(s) never reached the server: "
            f"{sorted(missing)[:3]}. `alembic upgrade head` would report (head) "
            f"having applied nothing. rsync backend/alembic/ too.",
        )
    return Verdict(
        "migrations", True, f"all {len(local_revisions)} revision files present"
    )


# --------------------------------------------------------------------------
# deploy manifest
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployRecord:
    """One line of ``/opt/TalentPing/.deploy-history``."""

    at: str
    git: str
    alembic: str


def parse_manifest(text: str) -> list[DeployRecord]:
    """Read the deploy history, oldest first, skipping anything unreadable.

    A line that will not parse is dropped rather than raising. This file is
    appended to over ssh, so a half-written line is a thing that can exist, and
    a rollback that tracebacks on it during an incident is strictly worse than
    one that works from the records it can read.
    """
    records: list[DeployRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        git = payload.get("git")
        if not isinstance(git, str) or not git:
            continue
        records.append(
            DeployRecord(
                at=str(payload.get("at", "")),
                git=git,
                alembic=str(payload.get("alembic", "")),
            )
        )
    return records


def rollback_target(
    records: list[DeployRecord], to: str = ""
) -> tuple[DeployRecord | None, str]:
    """Which deploy to go back to, and why not if there isn't one.

    With *to*, the revision is looked up **by commit** rather than taken from
    the line before the last. Those are only the same thing when rolling back
    exactly one deploy, and using the wrong one would downgrade the schema to a
    revision that commit never ran against.

    The most recent record for a commit wins: a commit deployed twice ran
    against whatever schema was current the second time.
    """
    if not records:
        return None, "the deploy history is empty"

    if to:
        matches = [record for record in records if record.git == to]
        if not matches:
            return None, f"{to} is not in the deploy history"
        return matches[-1], ""

    current = records[-1].git
    earlier = [record for record in records if record.git != current]
    if not earlier:
        return None, (
            "only one commit is on record, so there is no previous deploy to "
            "return to. Choose one with --to <sha>."
        )
    return earlier[-1], ""


# --------------------------------------------------------------------------
# destructive DDL
# --------------------------------------------------------------------------

# Operations that lose data or rewrite a table, and what an operator needs to
# know that the op name alone does not say.
_DATA_LOSS = {
    "drop_column": "drops a column; its data is gone and no downgrade restores it",
    "drop_table": "drops a table; its rows are gone and no downgrade restores them",
}

# Operations that do not lose data but can fail, or can break the code still
# running while they apply.
_RISKY = {
    "drop_constraint": "drops a constraint; rows that violate it can appear before it is restored",
    "rename_table": "renames a table; the running old code queries the old name until it restarts",
    "drop_index": "drops an index; queries relying on it degrade rather than fail, "
                  "so it shows up as latency",
}

# Raw SQL worth reading twice. Matched on the statement text, so a column named
# `dropped_at` does not trip it.
_RAW_SQL = re.compile(
    r"\b(?:DROP\s+(?:TABLE|COLUMN|CONSTRAINT|INDEX|TYPE)|TRUNCATE|DELETE\s+FROM)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    """One thing in one migration that deserves a second look before it runs."""

    revision: str
    line: int
    operation: str
    detail: str
    data_loss: bool

    def line_text(self) -> str:
        mark = "DATA LOSS" if self.data_loss else "risky"
        return f"      {self.revision}:{self.line} {self.operation} [{mark}] — {self.detail}"


def _function_body(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _called_name(call: ast.Call) -> str:
    """The bare function name of a call, whether ``op.drop_column`` or ``drop_column``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def scan_migration(revision: str, source: str) -> list[Finding]:
    """Everything destructive that *applying* this migration would do.

    Only ``upgrade()`` is scanned, and that distinction is the whole point. A
    ``drop_column`` inside ``downgrade()`` is the ordinary shape of a migration
    that adds one — a grep for the op name flags every migration in the tree and
    a check that is always red is a check nobody reads.

    Parsed rather than pattern-matched because the arguments decide the verdict:
    ``alter_column`` is routine until it carries ``type_``, and ``add_column`` is
    routine until it is ``nullable=False`` with nothing to backfill from.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            Finding(revision, exc.lineno or 0, "parse", f"cannot be parsed: {exc.msg}", False)
        ]

    upgrade = _function_body(tree, "upgrade")
    if upgrade is None:
        return [Finding(revision, 0, "upgrade", "has no upgrade() at all", False)]

    findings: list[Finding] = []
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)

        if name in _DATA_LOSS:
            findings.append(Finding(revision, node.lineno, name, _DATA_LOSS[name], True))
        elif name in _RISKY:
            findings.append(Finding(revision, node.lineno, name, _RISKY[name], False))
        elif name == "alter_column":
            findings.extend(_alter_column_findings(revision, node))
        elif name == "add_column":
            findings.extend(_add_column_findings(revision, node))
        elif name == "execute":
            findings.extend(_execute_findings(revision, node))

    return findings


def _alter_column_findings(revision: str, call: ast.Call) -> list[Finding]:
    findings = []
    if _keyword(call, "type_") is not None:
        findings.append(
            Finding(
                revision,
                call.lineno,
                "alter_column(type_=...)",
                "changes a column's type; Postgres rewrites the table under an "
                "ACCESS EXCLUSIVE lock, and rows that will not cast abort the deploy",
                False,
            )
        )
    if _keyword(call, "new_column_name") is not None:
        findings.append(
            Finding(
                revision,
                call.lineno,
                "alter_column(new_column_name=...)",
                "renames a column; the running old code selects the old name until it restarts",
                False,
            )
        )
    nullable = _keyword(call, "nullable")
    if isinstance(nullable, ast.Constant) and nullable.value is False:
        findings.append(
            Finding(
                revision,
                call.lineno,
                "alter_column(nullable=False)",
                "tightens a column to NOT NULL; existing NULL rows abort the migration",
                False,
            )
        )
    return findings


def _add_column_findings(revision: str, call: ast.Call) -> list[Finding]:
    """``add_column`` is only interesting when the new column forbids NULL.

    Alembic passes the column as a nested ``sa.Column(...)`` call, so the
    keywords that matter are on that inner call, not on ``add_column`` itself.
    """
    for arg in call.args:
        if not isinstance(arg, ast.Call) or _called_name(arg) != "Column":
            continue
        nullable = _keyword(arg, "nullable")
        if not (isinstance(nullable, ast.Constant) and nullable.value is False):
            continue
        if _keyword(arg, "server_default") is not None:
            continue
        return [
            Finding(
                revision,
                call.lineno,
                "add_column(nullable=False)",
                "adds a NOT NULL column with no server_default; this aborts on any "
                "table that already has rows",
                False,
            )
        ]
    return []


def _execute_findings(revision: str, call: ast.Call) -> list[Finding]:
    for arg in call.args:
        if (
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and _RAW_SQL.search(arg.value)
        ):
                statement = " ".join(arg.value.split())[:90]
                return [
                    Finding(
                        revision,
                        call.lineno,
                        "execute",
                        f"raw SQL that removes something: {statement!r}",
                        True,
                    )
                ]
    return []


def downgrade_is_stub(source: str) -> bool:
    """Is ``downgrade()`` present but empty — a rollback that silently does nothing?

    A body of ``pass`` (with or without a docstring) is the shape autogenerate
    leaves behind when the author did not finish. It is worse than a missing
    ``downgrade``, which at least fails loudly when someone tries to roll back.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    downgrade = _function_body(tree, "downgrade")
    if downgrade is None:
        return False

    def is_filler(node: ast.stmt) -> bool:
        """``pass``, ``...``, or a docstring — the three ways of writing nothing.

        ``...`` is matched as ``Expr(Constant(Ellipsis))`` rather than via
        ``ast.Ellipsis``, which was deprecated in 3.8 and removed in 3.12. The
        server runs Python 3.14, so the deprecated spelling would not have
        raised here — it would have quietly matched nothing and reported every
        stub downgrade as fine.
        """
        if isinstance(node, ast.Pass):
            return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            return isinstance(node.value.value, str) or node.value.value is Ellipsis
        return False

    return all(is_filler(node) for node in downgrade.body)


def ddl_verdict(pending: dict[str, str]) -> Verdict:
    """Judge the migrations this deploy is about to apply.

    *pending* maps revision id to source. It is deliberately only the migrations
    that have not run yet: scanning the whole tree would report the same dozen
    historical findings on every deploy, and an operator who has scrolled past
    the same warning twenty times does not read the twenty-first.
    """
    if not pending:
        return Verdict("destructive ddl", True, "no new migrations to apply")

    findings: list[Finding] = []
    stubs: list[str] = []
    for revision, source in sorted(pending.items()):
        findings.extend(scan_migration(revision, source))
        if downgrade_is_stub(source):
            stubs.append(revision)

    if not findings and not stubs:
        return Verdict(
            "destructive ddl", True, f"{len(pending)} new migration(s), nothing destructive"
        )

    lines = [f"{len(pending)} new migration(s) need reading before they run:"]
    lines += [f.line_text() for f in findings]
    for revision in stubs:
        lines.append(
            f"      {revision} downgrade() is empty — rolling back past this "
            f"revision would report success and change nothing"
        )
    if any(f.data_loss for f in findings):
        lines.append(
            "      At least one of these destroys data, so `alembic downgrade` "
            "will NOT undo this deploy. Take a dump first: see docs/RUNBOOK.md §6b."
        )
    lines.append("      Re-run with --allow-destructive once you have read the above.")
    return Verdict("destructive ddl", False, "\n".join(lines))


# --------------------------------------------------------------------------
# env file
# --------------------------------------------------------------------------


def envfile_issues(text: str) -> list[str]:
    """Values that systemd reads whole but ``.``-sourcing would truncate.

    Not a style complaint. systemd's ``EnvironmentFile`` takes the rest of the
    line, so the services always see the full value; the documented recipe for
    running a one-off script against prod sources the same file with ``.``, and
    bash stops the value at the first space. The script then sees a truncated
    value and reports a confident wrong answer.
    """
    issues: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not value:
            continue
        # `shlex` reads the value the way bash would, and every value goes
        # through it — including ones that start with a quote, since `KEY="a b" c`
        # and an unterminated `KEY="a b` are both broken and neither is caught by
        # looking at the first character. `#` is left as an ordinary character on
        # purpose: systemd keeps it, bash would start a comment, and that
        # disagreement is exactly what this is for.
        try:
            words = shlex.split(value)
        except ValueError:
            issues.append(
                f"line {number}: {key} has an unbalanced quote — sourcing this "
                f"file would fail outright"
            )
            continue
        if len(words) > 1:
            issues.append(
                f"line {number}: {key} is unquoted and contains spaces — "
                f"sourcing this file would set it to {words[0]!r}"
            )
    return issues


def envfile_verdict(text: str) -> Verdict:
    issues = envfile_issues(text)
    if issues:
        return Verdict("env file", False, "; ".join(issues))
    return Verdict("env file", True, "every multi-word value is quoted")


# --------------------------------------------------------------------------
# health endpoint
# --------------------------------------------------------------------------


def health_verdict(body: str) -> Verdict:
    """The API answers, and says it is well.

    ``llm_breakers_open`` is deliberately not treated as a failure: a restart
    resets those counters, so it reads empty right after every deploy whether or
    not anything was wrong. Reporting that as "deploy fixed the breakers" would
    be the deploy taking credit for its own amnesia.
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return Verdict("health", False, f"not JSON: {body[:120]!r}")
    status = payload.get("status")
    if status != "ok":
        return Verdict("health", False, f"status={status!r}{_failing(payload)}")
    return Verdict("health", True, f"status=ok{_latencies(payload)}")


# A restarted API answers within a second or two normally, but the first
# request after a deploy can be slower: `_hydrate_credentials` opens a database
# connection during startup, and Postgres itself may still be accepting the
# previous process's disconnects. Six tries five seconds apart covers that
# without turning a genuinely dead API into a half-minute of false hope.
HEALTH_ATTEMPTS = 6
HEALTH_INTERVAL_SECONDS = 5

# A hung API must fail the check rather than hang it. Without a deadline on the
# request itself, a deploy against an API that accepts the connection and never
# answers blocks forever, which reads as the deploy script being stuck.
HEALTH_TIMEOUT_SECONDS = 10


def poll_health(
    fetch,
    attempts: int = HEALTH_ATTEMPTS,
    interval: float = HEALTH_INTERVAL_SECONDS,
    sleep=time.sleep,
) -> Verdict:
    """Ask ``/health`` until it says ok, or until the attempts run out.

    A single request is the wrong shape here for a reason that only shows up
    under load: the check runs seconds after ``systemctl restart``, and a first
    request that arrives while the pool is still opening is answered honestly
    and unhelpfully with a failure the next request would not repeat. Retrying
    turns that into the pass it should be.

    The attempt number rides along in the passing verdict on purpose. "ok" and
    "ok, but only on the fifth try" mean very different things about the box,
    and a check that flattens them teaches the reader that slow starts are
    normal right up until one becomes a failure.
    """
    verdict = Verdict("health", False, "never attempted")
    for attempt in range(1, max(1, attempts) + 1):
        try:
            verdict = health_verdict(fetch())
        except Exception as exc:  # noqa: BLE001 - a failed fetch is a failed check
            verdict = Verdict("health", False, f"could not be reached: {exc}")

        if verdict.ok:
            if attempt > 1:
                return Verdict("health", True, f"{verdict.detail} (after {attempt} tries)")
            return verdict
        if attempt < max(1, attempts):
            sleep(interval)

    return Verdict(
        "health",
        False,
        f"{verdict.detail} — still failing after {max(1, attempts)} tries over "
        f"{int(max(1, attempts - 1) * interval)}s",
    )


def _failing(payload: dict) -> str:
    """Name the dependencies that are down, rather than quoting the body at it.

    The endpoint reports per-dependency now (`app/services/health.py`), so the
    one thing an operator reading a failed deploy needs — *which* of Postgres
    and Redis is refusing — can be stated instead of left in a truncated blob
    for them to pick it out of.
    """
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        return ""
    down = [
        name
        for name, check in checks.items()
        if isinstance(check, dict) and check.get("status") != "ok"
    ]
    return f" - down: {', '.join(sorted(down))}" if down else ""


def _latencies(payload: dict) -> str:
    """A passing check still worth reading: the numbers before they go bad."""
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        return ""
    parts = [
        f"{name} {check['latency_ms']}ms"
        for name, check in sorted(checks.items())
        if isinstance(check, dict) and isinstance(check.get("latency_ms"), (int, float))
    ]
    return f" ({', '.join(parts)})" if parts else ""


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def _ssh(host: str, key: str, command: str) -> str:
    return subprocess.run(
        [
            "ssh",
            "-i",
            key,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"root@{host}",
            command,
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def _show(host: str, key: str, unit: str) -> dict[str, str]:
    return parse_properties(
        _ssh(
            host,
            key,
            f"systemctl show {unit} -p ActiveState -p SubState -p MainPID -p NRestarts",
        )
    )




def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _revisions(text: str) -> set[str]:
    """Revision ids from a listing of version filenames, one per line.

    Alembic names files ``<revision>_<slug>.py``, and the id is the part before
    the first underscore. Reading filenames rather than executing anything means
    the same function works on `ls` output piped back from the server.
    """
    ids: set[str] = set()
    for line in text.splitlines():
        name = Path(line.strip()).name
        if not name.endswith(".py") or name == "__init__.py":
            continue
        ids.add(name[: -len(".py")].split("_", 1)[0])
    return ids


def _services_report(host: str, key: str, baseline: dict[str, int]) -> Report:
    report = Report()
    for unit in SERVICES:
        before = {"NRestarts": str(baseline.get(unit, 0))}
        after = _show(host, key, unit)
        time.sleep(SETTLE_SECONDS)
        later = _show(host, key, unit)
        report.add(service_verdict(unit, before, after, later))
    # `--max-time` belongs on the curl rather than only in `poll_health`: the
    # retry budget bounds how many times we ask, but only the request's own
    # deadline bounds how long one unanswered ask can take.
    report.add(
        poll_health(
            lambda: _ssh(
                host,
                key,
                f"curl -fsS --max-time {HEALTH_TIMEOUT_SECONDS} "
                f"http://127.0.0.1:8000/api/v1/health",
            )
        )
    )
    return report


def _units_report(host: str, key: str) -> Report:
    report = Report()
    for unit in SERVICES:
        properties = parse_properties(
            _ssh(
                host,
                key,
                f"systemctl show {unit} -p Restart -p StartLimitBurst -p TimeoutStopUSec "
                f"-p KillSignal -p MemoryMax -p StandardOutput -p StandardError",
            )
        )
        for verdict in unit_verdicts(unit, properties):
            report.add(verdict)
    return report


def _module_string(tree: ast.Module, name: str) -> str | None:
    """A module-level ``name = "value"`` assignment, or None.

    Alembic writes ``revision`` and ``down_revision`` as plain literals at the
    top of every version file, so reading them does not require importing the
    module — which matters, because importing them all would pull in the whole
    application to answer a question about text.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            # `down_revision: str | None = "abc"` is the annotated form recent
            # alembic templates emit; its `value` is optional, unlike Assign's.
            targets = [node.target]
        else:
            continue

        value = node.value
        if not isinstance(value, ast.Constant):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return value.value if isinstance(value.value, str) else None
    return None


def revision_chain(versions_dir: str) -> tuple[list[str], dict[str, str]]:
    """The revisions in application order, plus each one's source.

    Ordered by walking ``down_revision`` from the base rather than by filename,
    because filenames sort alphabetically and migrations do not run
    alphabetically. ``tests/test_migration_chain.py`` already guarantees the
    shape this relies on — one base, one head, no branching — so a walk is safe
    here without re-proving it.
    """
    sources: dict[str, str] = {}
    parents: dict[str, str | None] = {}
    for path in sorted(Path(versions_dir).glob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        revision = _module_string(tree, "revision")
        if not revision:
            continue
        sources[revision] = source
        parents[revision] = _module_string(tree, "down_revision")

    children = {parent: child for child, parent in parents.items() if parent is not None}
    order: list[str] = []
    node = next((rev for rev, parent in parents.items() if parent is None), None)
    seen: set[str] = set()
    while node is not None and node not in seen:
        seen.add(node)
        order.append(node)
        node = children.get(node)

    # Anything the walk did not reach is off the chain. Reporting it is better
    # than dropping it: an orphaned migration is exactly the sort of thing this
    # check should not be silent about.
    order += sorted(set(sources) - seen)
    return order, sources


def pending_sources(versions_dir: str, current: str) -> dict[str, str]:
    """Source of every migration that ``alembic upgrade head`` would still apply.

    *current* is what the server reports as its present revision. Everything
    after it in the chain is pending; an empty or unrecognised *current* means
    the whole chain is, which is the right answer for a fresh database and the
    safe answer when the server's revision could not be read.
    """
    order, sources = revision_chain(versions_dir)
    if current and current in order:
        order = order[order.index(current) + 1 :]
    return {revision: sources[revision] for revision in order}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post-deploy checks that do not lie.")
    sub = parser.add_subparsers(dest="command", required=True)

    services = sub.add_parser("services", help="units stayed up across the restart")
    services.add_argument("--host", required=True)
    services.add_argument("--key", required=True, help="ssh private key")
    services.add_argument(
        "--baseline",
        default="{}",
        help='JSON {unit: NRestarts} captured before restarting',
    )

    frontend = sub.add_parser("frontend", help="the live page names this build")
    frontend.add_argument("--built", required=True, help="local dist/index.html")
    frontend.add_argument("--served", required=True, help="index.html fetched from the site")

    migrations = sub.add_parser("migrations", help="revision files reached the server")
    migrations.add_argument("--local", required=True, help="local version filenames")
    migrations.add_argument("--deployed", required=True, help="remote version filenames")

    envfile = sub.add_parser("envfile", help="values systemd and bash read differently")
    envfile.add_argument("--file", required=True)

    ddl = sub.add_parser("ddl", help="destructive DDL among the migrations about to run")
    ddl.add_argument("--versions", required=True, help="backend/alembic/versions")
    ddl.add_argument(
        "--current",
        default="",
        help="revision the server is on now; empty means treat the whole chain as pending",
    )

    units = sub.add_parser("units", help="restart policy, limits and logging (advisory)")
    units.add_argument("--host", required=True)
    units.add_argument("--key", required=True, help="ssh private key")

    manifest = sub.add_parser("manifest", help="pick the commit to roll back to")
    manifest.add_argument("--history", required=True, help="the .deploy-history file")
    manifest.add_argument("--to", default="", help="a specific commit instead of the previous")
    manifest.add_argument(
        "--latest",
        action="store_true",
        help="report the running deploy rather than a rollback target",
    )
    manifest.add_argument(
        "--field",
        choices=("git", "alembic", "at"),
        help="print just this field, for a shell to read",
    )

    args = parser.parse_args(argv)

    # Not a Report: this answers a question rather than judging a thing, and its
    # stdout is read by the shell.
    if args.command == "manifest":
        records = parse_manifest(_read(args.history))
        if args.latest:
            record = records[-1] if records else None
            why = "the deploy history is empty"
        else:
            record, why = rollback_target(records, args.to)
        if record is None:
            print(why, file=sys.stderr)
            return 1
        if args.field:
            print(getattr(record, args.field))
        else:
            print(f"git={record.git}\nalembic={record.alembic}\nat={record.at}")
        return 0

    if args.command == "services":
        report = _services_report(args.host, args.key, json.loads(args.baseline))
    elif args.command == "units":
        report = _units_report(args.host, args.key)
    else:
        report = Report()
        if args.command == "frontend":
            report.add(frontend_verdict(_read(args.built), _read(args.served)))
        elif args.command == "migrations":
            report.add(
                migration_verdict(
                    _revisions(_read(args.local)), _revisions(_read(args.deployed))
                )
            )
        elif args.command == "envfile":
            report.add(envfile_verdict(_read(args.file)))
        elif args.command == "ddl":
            report.add(ddl_verdict(pending_sources(args.versions, args.current)))

    print(report.text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
