"""The environment-variable catalogue, read out of ``config.py`` itself.

``Settings`` has 150-odd fields and every one of them is an environment
variable an operator can set. Only about half appear in ``.env.example``, and
none carry a pydantic ``description=`` — what they *do* carry, densely, is a
comment block above the declaration explaining why the number is the number.
That commentary is the real documentation, and it is maintained, because it
sits in the file people edit when they change the default.

So the catalogue is extracted rather than written. Writing it out by hand would
produce a second copy of 150 facts that drifts from the first the next time
somebody adds a setting — which is the failure mode this whole round is about.
Reading it from source means a new field is documented by the comment its
author already wrote, and :mod:`tests.test_api_catalog` fails when there is no
such comment.

**Why the AST and not ``model_fields``.** Pydantic hands over the name, the
type and the default, and drops the comments on the floor — they are not part
of the object. The section headers (``# ---- Database ----``) go the same way,
and they are how a reader finds anything in a 1000-line settings file. So this
walks the source: pydantic for what pydantic knows, the syntax tree for the
prose beside it. The two are cross-checked in
:func:`catalog` — a field visible to one and not the other is a bug in this
module, not a documentation gap, and it raises rather than being silently
omitted.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
from dataclasses import dataclass, field

from app.core.config import Settings

# ``# ---- Database ----``, the divider style used throughout config.py. The
# trailing dashes are optional because a few headers run long and drop them.
_SECTION_RE = re.compile(r"^#\s*-{2,}\s*(.+?)\s*-*\s*$")

# A field whose default is one of these ships unconfigured: it is blank, or it
# is the placeholder `Settings` refuses to start production on. Either way an
# operator has to supply a value, which is the distinction "required vs
# optional" is actually asking about — every field carries a default, so no
# field is required in pydantic's own sense and asking pydantic gives 151
# "optional".
#
# Compared against the *evaluated* literal, not its source text: `ast.unparse`
# normalises quoting, so matching on `'""'` silently missed every blank default
# in the file and reported the whole catalogue as configured.
_UNSET_DEFAULTS = {"", "change-me-to-a-long-random-string"}


def _ships_unset(node: ast.expr | None) -> bool:
    if node is None:
        return True
    try:
        return ast.literal_eval(node) in _UNSET_DEFAULTS
    except (ValueError, TypeError, SyntaxError):
        # A computed default — a call, a concatenation, an f-string. Not blank.
        return False

# Substrings that mark a value as credential-shaped. Used only to decide
# whether the catalogue prints the default; the point is that a generated doc
# committed to the repository must never carry a real secret, even though the
# defaults here are all placeholders today.
_SECRET_HINTS = ("secret", "password", "api_key", "_key", "token", "credentials")


@dataclass(frozen=True)
class SettingDoc:
    """One environment variable, as an operator needs to see it."""

    name: str
    """The field name, lower-case, as ``Settings`` declares it."""

    env_var: str
    """The environment variable that sets it — the name, upper-cased."""

    section: str
    """The ``# ---- ... ----`` heading the declaration sits under."""

    type_name: str
    """The annotation, verbatim from the source."""

    default: str
    """The default, rendered as source. ``"(secret)"`` for credential fields."""

    description: str
    """The comment block above the declaration, joined into prose."""

    required: bool
    """True when the shipped default is blank or a refused placeholder — the
    setting arrives unconfigured and an operator has to supply a value for the
    feature behind it to do anything."""

    secret: bool
    """True when the value is credential-shaped and must not be logged."""

    validated_by: tuple[str, ...] = ()
    """Names of the ``@field_validator`` guards that check this field at
    startup. A guarded field fails the process rather than misbehaving, which
    is worth knowing before changing it."""

    shared_description: bool = False
    """True when the prose came from the field above rather than its own
    comment — the declarations are consecutive and the block covers the run."""

    in_env_example: bool = field(default=False, compare=False)
    """Whether ``.env.example`` names it. Filled in by :func:`catalog`."""


def _config_source() -> str:
    return pathlib.Path(inspect.getfile(Settings)).read_text(encoding="utf-8")


def _repo_root() -> pathlib.Path:
    # app/core/settings_docs.py -> app/core -> app -> backend -> repo root
    return pathlib.Path(__file__).resolve().parents[3]


def env_example_names(path: pathlib.Path | None = None) -> set[str]:
    """Every variable ``.env.example`` mentions, commented-out ones included.

    A line that is commented out is still documentation — it is how the file
    shows an optional block — so ``# SERPAPI_API_KEY=`` counts as documented.
    """
    target = path or _repo_root() / ".env.example"
    if not target.exists():
        return set()
    text = target.read_text(encoding="utf-8")
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=", text, re.MULTILINE))


def _leading_comment(lines: list[str], decl_lineno: int) -> tuple[str, str | None]:
    """The comment block directly above line *decl_lineno* (1-based).

    Returns ``(description, section_header_seen)``. Walking upward stops at the
    first line that is neither a comment nor blank-adjacent, so a comment
    separated from the field by a blank line belongs to whatever came before
    and is not claimed here. A section divider terminates the block and is
    reported separately rather than becoming part of the prose.
    """
    collected: list[str] = []
    section: str | None = None
    i = decl_lineno - 2  # 0-based index of the line above the declaration
    while i >= 0:
        raw = lines[i].strip()
        if not raw.startswith("#"):
            break
        match = _SECTION_RE.match(raw)
        if match:
            section = match.group(1)
            break
        collected.append(raw.lstrip("#").strip())
        i -= 1
    collected.reverse()
    # Blank comment lines are paragraph breaks in the source; collapse them so
    # the rendered description is one flowing paragraph per break.
    parts: list[str] = []
    buffer: list[str] = []
    for line in collected:
        if line:
            buffer.append(line)
        elif buffer:
            parts.append(" ".join(buffer))
            buffer = []
    if buffer:
        parts.append(" ".join(buffer))
    return "\n\n".join(parts), section


def _sections(lines: list[str]) -> list[tuple[int, str]]:
    """Every section divider in the file, as ``(line number, heading)``."""
    out: list[tuple[int, str]] = []
    for idx, raw in enumerate(lines, start=1):
        match = _SECTION_RE.match(raw.strip())
        if match:
            out.append((idx, match.group(1)))
    return out


def _section_for(sections: list[tuple[int, str]], lineno: int) -> str:
    current = "General"
    for at, heading in sections:
        if at < lineno:
            current = heading
        else:
            break
    return current


def _validator_map(class_def: ast.ClassDef) -> dict[str, list[str]]:
    """Which fields each ``@field_validator`` in ``Settings`` guards.

    Read off the decorator's string arguments. A guarded field is one that can
    refuse to boot — ``JWT_SECRET`` on its placeholder, ``DEBUG`` outside
    development — and a catalogue that did not say so would present those as
    ordinary knobs with an unusually strong opinion in the prose.
    """
    out: dict[str, list[str]] = {}
    for node in class_def.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            named = getattr(func, "id", None) or getattr(func, "attr", None)
            if named != "field_validator":
                continue
            for arg in decorator.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.setdefault(arg.value, []).append(node.name)
    return out


def catalog() -> list[SettingDoc]:
    """Every configurable setting, in declaration order.

    Declaration order rather than alphabetical on purpose: the sections group
    related knobs, and an operator reading about ``DB_POOL_SIZE`` wants
    ``DB_MAX_OVERFLOW`` next to it, not forty entries away.
    """
    source = _config_source()
    lines = source.splitlines()
    sections = _sections(lines)
    tree = ast.parse(source)

    class_def = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )

    guards = _validator_map(class_def)
    known = set(Settings.model_fields)
    docs: list[SettingDoc] = []
    seen: set[str] = set()
    previous: str | None = None
    previous_end = -2

    for node in class_def.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if name not in known:
            # A ClassVar or a private annotation — not an environment variable.
            continue
        seen.add(name)
        description, header = _leading_comment(lines, node.lineno)
        section = header or _section_for(sections, node.lineno)
        # A run of declarations with nothing between them is one paragraph's
        # worth of subject. `db_pool_size`, `db_max_overflow` and
        # `db_pool_timeout` are declared on three consecutive lines under a
        # comment that reasons about all three at once ("a ceiling of 40" is
        # size plus overflow); the same holds for each provider's key/url/model
        # triple. Reading those as two undocumented fields beside one
        # documented one is a mis-parse, not a gap — so the block carries
        # through the run and stops at the first blank line or comment.
        if not description and previous is not None and node.lineno == previous_end + 1:
            description = previous
            inherited = True
        else:
            inherited = False
        if description:
            previous = description
        previous_end = node.end_lineno or node.lineno
        default = ast.unparse(node.value) if node.value is not None else ""
        secret = any(hint in name for hint in _SECRET_HINTS)
        unset = _ships_unset(node.value)
        docs.append(
            SettingDoc(
                name=name,
                env_var=name.upper(),
                section=section,
                type_name=ast.unparse(node.annotation),
                default="(secret)" if secret and not unset else default,
                description=description,
                required=unset,
                secret=secret,
                validated_by=tuple(guards.get(name, ())),
                shared_description=inherited,
            )
        )

    missing = known - seen
    if missing:
        # Not a documentation gap — a parser gap. Something declares a field in
        # a shape this walker does not recognise, and silently omitting it
        # would make the catalogue quietly incomplete, which is worse than the
        # no-catalogue state it replaced.
        raise RuntimeError(
            "settings_docs could not read these fields out of config.py: "
            + ", ".join(sorted(missing))
        )

    documented = env_example_names()
    return [
        SettingDoc(**{**doc.__dict__, "in_env_example": doc.env_var in documented})
        for doc in docs
    ]
