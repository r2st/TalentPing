"""What the market keeps asking for that the candidate's resume does not say.

:attr:`~app.models.fit_score.FitScore.missing_skills` has always been computed —
:func:`app.services.fit_scorer.score_skills` writes it on every scored posting —
and then only ever read back one posting at a time, on the card the user is
looking at. One posting's missing list is noise: any single employer asks for
something nobody has. The same list summed over three hundred postings is a
different object entirely, and it answers a question the product could not
answer before: *which absences are actually costing you roles.*

**Two kinds of absence, and the second is the useful one.**

A skill the scorer marked missing is genuinely nowhere in the resume it scored —
``score_skills`` matches against the whole document text, the headline, the
summary, the experience entries and the profile's own skills, not just the
parsed ``skills`` list. So there is no "the parser missed it" bucket here, and a
report that claimed one would be inventing it.

What there is instead comes from the grain of the table.
:class:`~app.models.fit_score.FitScore` is keyed on ``(resume_id, jd_hash,
profile_id)``, so a candidate with three resumes has three verdicts on the same
posting — and a skill can be missing on the document that was scored while
sitting in plain text on one of the others. That is the split:

* :attr:`SkillsGapReport.blocking` — asked for repeatedly, on none of the
  candidate's resumes. Real homework, and the honest thing to call it.
* :attr:`SkillsGapReport.already_have` — asked for repeatedly, missing on the
  resume that got scored, present on another one they own. Not homework at all:
  the experience exists and is already written down, on the wrong document.

The second list is worth more than the first and is why this module exists. It
turns "learn Kubernetes" into "your platform resume says Kubernetes and your
generalist one does not, and forty of the roles you are being scored against ask
for it" — which is one edit, not a quarter of study.

**When to stay quiet.** The same problem :mod:`app.routers.analytics` solved for
its focus notes, and the same answer: a recommendation drawn from four postings
is worse than no recommendation, because the user will act on it. Below
:data:`MIN_SCORED_POSTINGS` this reports the count and nothing else, and a skill
asked for fewer than :data:`MIN_POSTINGS_PER_SKILL` times is one employer's
preference rather than a market signal.

Pure apart from two reads: a session and a user in, a report out. Nothing here
scores anything — every number is a fold over rows the scorer already wrote.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.fit_score import FitScore
from app.models.resume import Resume
from app.models.user import User
from app.services.fit_scorer import canonical_skill

#: Distinct scored postings below which no gap is reported at all. Matches the
#: spirit of ``analytics.MIN_FOCUS_TOTAL``: under this the only honest sentence
#: is how far off having an answer the user is.
MIN_SCORED_POSTINGS = 10

#: How many distinct postings must ask for a skill before its absence is a gap.
#: Two is a coincidence — one team's stack twice. Three is the market saying
#: something.
MIN_POSTINGS_PER_SKILL = 3

#: Rows per list. Long enough to be a plan, short enough to be one.
MAX_ROWS = 8


@dataclass(frozen=True)
class SkillGap:
    """One skill, and how much its absence is costing."""

    skill: str
    #: Distinct postings that asked for it and did not find it.
    postings: int
    #: That count as a share of every posting the user has scored, 0..1.
    share: float
    #: The resume of theirs that *does* have it, when one does. ``None`` puts
    #: this row in ``blocking``; set, it puts it in ``already_have``.
    on_resume_id: int | None = None
    on_resume_label: str | None = None


@dataclass(frozen=True)
class SkillsGapReport:
    """The whole answer, including the answer "not yet"."""

    #: Distinct postings scored, over every resume and profile.
    scored_postings: int
    #: Asked for repeatedly, on none of their resumes.
    blocking: list[SkillGap] = field(default_factory=list)
    #: Asked for repeatedly, missing on the scored resume, present on another.
    already_have: list[SkillGap] = field(default_factory=list)
    #: Why the lists are empty, when they are. Empty string when they are not.
    note: str = ""

    @property
    def has_findings(self) -> bool:
        return bool(self.blocking or self.already_have)


def _normalize(skill: str) -> str:
    """The key two spellings of one skill collapse onto.

    Case, surrounding space, and the alias groups ``fit_scorer`` already scores
    by. No stemming beyond that.

    Alias expansion used to be wrong here rather than merely absent, because the
    scorer matched these strings against the resume exactly as the posting wrote
    them: merging ``postgres`` into ``postgresql`` would have claimed a match it
    never made. It collapses both onto one tool before deciding now, so anything
    reaching this table is missing under *every* spelling of itself — and
    keeping them apart is what invents a number, in both of the ways this report
    can be wrong.

    It undercounts. A skill asked for by two postings spelling it "Postgres" and
    two spelling it "PostgreSQL" is four postings, and it was two rows of two —
    under :data:`MIN_POSTINGS_PER_SKILL` twice over, so a gap on four of the
    candidate's postings was reported as no gap at all. Where both halves did
    clear the floor it spent two of :data:`MAX_ROWS` saying one thing, each with
    half the count beside it.

    And it gives the wrong advice. ``matched_by`` is keyed the same way, so a
    posting that missed "Postgres" and a resume that matched "PostgreSQL"
    landed in different tallies — which puts the skill in ``blocking``, a
    quarter of study, when the experience is already written down on another of
    the candidate's own documents and the fix is one edit. That is precisely the
    merge this module's docstring says it must never get wrong, arrived at from
    the other direction.
    """
    return canonical_skill(" ".join(skill.split()).lower())


@dataclass
class _Tally:
    """Working state for one skill while the rows are folded."""

    #: Every spelling the postings used for this skill, and how often. Kept as
    #: a count rather than as the first one seen because the fold now collapses
    #: whole alias groups: "Postgres" and "PostgreSQL" arrive as one tally, so
    #: "the first one" is decided by the order rows come back in, and this query
    #: has no ``ORDER BY`` — the same report could say "k8s" on one read and
    #: "Kubernetes" on the next. Counting also picks a better label than any
    #: tie-break could: the spelling the candidate's own market actually writes.
    spellings: Counter[str] = field(default_factory=Counter)
    #: Postings that asked for it and did not find it on the scored resume.
    missing_on: set[str] = field(default_factory=set)
    #: Resumes of theirs that do have it.
    matched_by: set[int] = field(default_factory=set)
    #: Resumes of theirs that were scored and *did not* have it. Kept because
    #: the two sets can overlap, and where they do only this one is load-bearing
    #: — see :func:`report`.
    missing_by: set[int] = field(default_factory=set)

    @property
    def label(self) -> str:
        """The spelling to show, most-used first.

        ``Counter.most_common`` breaks ties by insertion order, which is row
        order again, so the tie is broken on the spelling itself instead.
        """
        return min(self.spellings.items(), key=lambda item: (-item[1], item[0]))[0]


def _tally(db: Session, user: User) -> tuple[dict[str, _Tally], int]:
    """Fold every fit score the user has into one record per skill.

    One query, four columns, no bodies and no joins. ``jd_hash`` is the posting
    identity rather than ``job_posting_id`` because the latter is ``SET NULL``
    on delete — pruning a posting would silently drop it out of the denominator
    and inflate every share on the page.
    """
    tallies: dict[str, _Tally] = {}
    postings: set[str] = set()

    rows = db.execute(
        select(
            FitScore.jd_hash,
            FitScore.resume_id,
            FitScore.missing_skills,
            FitScore.matched_skills,
        ).where(FitScore.user_id == user.id)
    )

    for jd_hash, resume_id, missing, matched in rows:
        if not jd_hash:  # pragma: no cover - the column is non-null
            continue
        postings.add(jd_hash)

        for skill in missing or []:
            if not isinstance(skill, str) or not skill.strip():
                continue
            tally = tallies.setdefault(_normalize(skill), _Tally())
            tally.spellings[skill.strip()] += 1
            tally.missing_on.add(jd_hash)
            tally.missing_by.add(resume_id)

        for skill in matched or []:
            if not isinstance(skill, str) or not skill.strip():
                continue
            tally = tallies.setdefault(_normalize(skill), _Tally())
            tally.spellings[skill.strip()] += 1
            tally.matched_by.add(resume_id)

    return tallies, len(postings)


def _resume_labels(db: Session, user: User) -> dict[int, str]:
    return {
        resume.id: resume.display_label
        for resume in db.scalars(select(Resume).where(Resume.user_id == user.id))
    }


def report(db: Session, user: User) -> SkillsGapReport:
    """The aggregate gap for *user*, or an explanation of why there isn't one."""
    tallies, scored_postings = _tally(db, user)

    if scored_postings < MIN_SCORED_POSTINGS:
        return SkillsGapReport(
            scored_postings=scored_postings,
            note=(
                f"{scored_postings} postings scored so far. At "
                f"{MIN_SCORED_POSTINGS} there is enough here to say which "
                "skills are actually costing you roles."
            ),
        )

    labels = _resume_labels(db, user)
    blocking: list[SkillGap] = []
    already_have: list[SkillGap] = []

    for tally in tallies.values():
        count = len(tally.missing_on)
        if count < MIN_POSTINGS_PER_SKILL:
            continue

        # Share is against every posting scored, not against the postings that
        # asked for this skill. "Missing on 40% of everything you look at" is
        # the number that decides where a week goes; "missing on 100% of the
        # postings that ask for it" is true of every row here and says nothing.
        gap = SkillGap(
            skill=tally.label,
            postings=count,
            share=round(count / scored_postings, 3),
        )

        # `already_have` is only worth its name if the resume it points at is a
        # *different* document from the ones the skill went missing on. A resume
        # can land in both sets at once, and then it is no evidence at all:
        #
        # * Two profiles, one document. The scorer matches against the profile's
        #   own skills as well as the resume's text, so a resume scored under a
        #   profile that lists Kubernetes matches it, and the same resume scored
        #   under a profile that does not, misses it. Nothing about the document
        #   changed between the two rows.
        # * A resume edited after it was scored. The old verdicts stay on the
        #   table saying missing while the new ones say matched.
        #
        # Either way the row used to read "your Senior Backend Engineer resume
        # has it" while every posting in its own count was scored against that
        # same Senior Backend Engineer resume and found it lacking — an edit
        # with nothing to copy from and no other document named. Subtracting
        # leaves only resumes that really are somewhere else to copy from; when
        # that leaves nothing, the row is homework, which is what the rest of it
        # already says.
        elsewhere = tally.matched_by - tally.missing_by
        if not elsewhere:
            blocking.append(gap)
            continue

        # Present on a resume they own. Name the one with the strongest claim —
        # in practice there is usually exactly one, and picking the lowest id
        # keeps the sentence stable between page loads rather than reshuffling
        # on whatever order the rows came back in.
        resume_id = min(elsewhere)
        already_have.append(
            SkillGap(
                skill=gap.skill,
                postings=gap.postings,
                share=gap.share,
                on_resume_id=resume_id,
                on_resume_label=labels.get(resume_id),
            )
        )

    # Most-demanded first, then alphabetically so a tie does not reorder itself
    # between two reads of the same data.
    def _rank(row: SkillGap) -> tuple[int, str]:
        return (-row.postings, row.skill.lower())

    blocking.sort(key=_rank)
    already_have.sort(key=_rank)

    note = ""
    if not blocking and not already_have:
        note = (
            f"Nothing is missing from {MIN_POSTINGS_PER_SKILL} or more of the "
            f"{scored_postings} postings you have scored."
        )

    return SkillsGapReport(
        scored_postings=scored_postings,
        blocking=blocking[:MAX_ROWS],
        already_have=already_have[:MAX_ROWS],
        note=note,
    )


__all__ = [
    "MAX_ROWS",
    "MIN_POSTINGS_PER_SKILL",
    "MIN_SCORED_POSTINGS",
    "SkillGap",
    "SkillsGapReport",
    "report",
]
