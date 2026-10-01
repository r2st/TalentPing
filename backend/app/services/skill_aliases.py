"""The vocabulary two documents have to agree on before either can be matched.

Three things in this package are about the *spellings* of a tool rather than
about anyone's skill at it, and until now they lived in two files that did not
import each other:

* :data:`SKILL_ALIASES` — the groups of spellings that name one tool, read by
  :func:`app.services.fit_scorer.skill_mentioned` when it looks for a posting's
  skill in a resume;
* :func:`normalize_text` — the form both sides are compared in, accents folded
  and punctuation turned to spaces;
* :data:`app.services.resume_parser._SKILL_TAXONOMY` — the list of skills that
  are looked *for* in the first place. That one stays where it is, because it is
  what the parser scans with; what it needed was the two above.

The split mattered. ``extract_skills`` is the producer for both sides of the fit
comparison — it writes a posting's ``required_skills`` and a resume's ``skills``
— and it matched each taxonomy entry as one literal string. So a posting that
said "ReactJS", "NodeJS" or "NextJS" produced *no skills at all*, and one asking
for "Postgres" or "k8s" produced none of those either. The consumer downstream
knew every one of those spellings; the producer that fed it did not, and a skill
that is never extracted cannot be missed, listed, scored or tailored for.

This module is a leaf on purpose: it imports only :mod:`app.services.places`, so
both the parser and the scorer can depend on it without either depending on the
other.
"""
from __future__ import annotations

import re

from app.services.places import fold_diacritics


def normalize_text(value: str) -> str:
    """Lower-cased, unaccented, punctuation-folded — the form both sides are compared in.

    The unaccenting is not cosmetic and it has to happen *first*. The character
    class below keeps ``a-z0-9+#.`` and turns everything else into a space, and
    an accented letter is everything else — so "développeur" arrived here as the
    two fragments "d veloppeur", "ingénieur" as "ing nieur", "diseñador" as
    "dise ador". Not a word that failed to match: a word that stopped being a
    word, in every language this feed carries but English.

    Every comparison in :mod:`app.services.fit_scorer` runs on the output, so
    the damage was everywhere at once and all of it silent:

    * ``_title_tokens`` drops the one-letter fragment as noise and keeps the
      other, so "Développeur" tokenised to ``["veloppeur"]`` while the same
      posting spelt without its accent — which is how half the boards write it —
      tokenised to ``["developpeur"]``. Those share nothing, so
      :func:`title_relevance` scored one title against its own other spelling at
      **0.0**, "unrelated". "Ingénieur" and "Diseñador" did the same. The
      two-token forms fared no better: "Développeur Backend" against
      "Developpeur Backend" scored 0.65, "adjacent" — a posting marked as a
      different specialism from itself.
    * :func:`~app.services.fit_scorer.skill_mentioned` looks for the needle in
      a haystack shredded the
      same way, so a résumé saying "sécurité applicative" did not contain
      "sécurité", and the skill went onto ``missing_skills`` — the "Not on your
      resume" list the candidate reads and the tailoring prompt is told to
      close.
    * ``_industry_match`` and the experience history are keyed on the same
      output, so "Santé" and "Énergie" named no industry.

    Role is a quarter of the fit score and skills are the heaviest dimension at
    0.30, so this ran straight into the number that decides whether autopilot
    writes to a stranger.

    Folding is symmetric — both the needle and the haystack pass through here —
    so it can only make two spellings of one word agree. It is the same helper,
    and the same bug, that :func:`app.services.places.fold_diacritics` was
    extracted for; ``score_location`` already went through it, which is why the
    location dimension was the only one that could read a French posting.

    Public, and it is the haystack form :func:`skill_mentioned` expects: that
    function does *not* normalize what it is given, because it is called once
    per skill per document and normalizing inside would repeat the pass for
    every skill. Callers were left to work that contract out from the source,
    and one of them passed raw text, which quietly matches nothing once
    punctuation is involved.
    """
    return re.sub(r"[^a-z0-9+#.]+", " ", fold_diacritics(value or "").lower()).strip()


# Spellings of one tool that a posting and a resume will not agree on.
#
# :func:`app.services.fit_scorer.score_skills` matched a skill by looking for
# the posting's exact
# spelling in the candidate's resume, and the two documents are written by
# different people who have never agreed on how to spell anything. A posting
# asking for "Postgres" found nothing in a resume that says "PostgreSQL"; "k8s"
# found nothing in one that says "Kubernetes"; "Golang" found nothing in one
# that says "Go".
#
# It is the heaviest dimension in the score at 0.30, so each of those is a real
# dent in a real posting's fit. But the worse half is that the same comparison
# writes ``missing_skills``, which is not an internal number — it is the "Not on
# your resume" list the candidate reads, the gap the tailoring prompt is told to
# close, and the row `analytics` sums across every posting to tell someone what
# to go and learn. A candidate with eight years of PostgreSQL was being told to
# learn Postgres, on every posting that used the short name, and then told again
# in aggregate that it was their biggest gap.
#
# The product already knows most of these: `company_research._TECH_KEYWORDS`
# groups "postgres" with "postgresql" and "k8s" with "Kubernetes" so a company
# page shows one chip instead of two. That table is built for scanning prose —
# its entries carry padding spaces (``"java "``, ``" go "``) to fake the word
# boundaries the scorer gets from a regex — so it is the same knowledge in a
# shape this cannot use, not a table to import.
#
# Groups, not a canonical mapping: every spelling in a group is tried whenever
# any one of them is asked for, so it works whichever side used the short name.
# Kept to tools whose spellings are genuinely the same thing — "opensearch" is
# not in with "elasticsearch", and ".net" is not in with "c#", because those are
# claims about products rather than about spelling.
SKILL_ALIASES: tuple[tuple[str, ...], ...] = (
    ("postgresql", "postgres", "psql"),
    ("mongodb", "mongo"),
    ("elasticsearch", "elastic search"),
    ("microsoft sql server", "sql server", "mssql"),
    ("kubernetes", "k8s"),
    ("golang", "go"),
    ("javascript", "js"),
    ("typescript", "ts"),
    ("node.js", "nodejs", "node js"),
    ("react.js", "reactjs", "react"),
    ("vue.js", "vuejs", "vue"),
    ("next.js", "nextjs"),
    ("c#", "csharp", "c sharp"),
    ("c++", "cpp"),
    ("objective-c", "objectivec", "objc"),
    ("ruby on rails", "rails"),
    ("amazon web services", "aws"),
    ("google cloud platform", "google cloud", "gcp"),
    ("microsoft azure", "azure"),
    ("ci cd", "cicd", "continuous integration"),
    ("machine learning", "ml"),
    ("artificial intelligence", "ai"),
    ("natural language processing", "nlp"),
    ("large language models", "large language model", "llms", "llm"),
    ("scikit-learn", "scikit learn", "sklearn"),
    ("rest api", "restful api", "rest apis", "restful apis"),
    # The "Apache" prefix is a posting convention, not part of what anyone
    # calls the tool. A resume says "Kafka"; the job ad that wants it says
    # "Apache Kafka", finds nothing, and puts the candidate's own daily tool on
    # the "Not on your resume" list.
    ("apache kafka", "kafka"),
    ("apache spark", "spark"),
    ("apache airflow", "airflow"),
    ("apache cassandra", "cassandra"),
    (".net", "dotnet", "dot net"),
    ("spring boot", "springboot"),
    ("react native", "reactnative"),
)

#: Every spelling above, pointing at the whole group it belongs to. Normalised
#: on the way in so a lookup can use the same form `normalize_text` produces.
SKILL_ALIAS_INDEX: dict[str, tuple[str, ...]] = {
    normalize_text(spelling): group for group in SKILL_ALIASES for spelling in group
}


def canonical_skill(skill: str) -> str:
    """The one spelling an alias group is counted under.

    Public because the matcher that reads the table above is not the only
    place two spellings of one tool have to become one thing:
    :mod:`app.services.skills_gap` folds
    ``missing_skills`` across every posting a candidate has been scored against,
    and it keys that fold on the string the posting used. Returned unchanged
    when the skill is in no group, so it is safe to run over anything.
    """
    group = SKILL_ALIAS_INDEX.get(normalize_text(skill))
    return group[0] if group else skill


def alias_spellings(skill: str, *, known: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """The other ways *skill* is written, for a caller that scans raw text.

    Returned as written rather than normalised, because the caller matching them
    is looking through a document rather than comparing two normalised strings.

    Two spellings are held back, and both exclusions keep an existing scan
    behaving exactly as it did:

    * anything in *known* — a spelling the caller already looks for on its own
      account. Handing "golang" back as an alias of "go" would have made one
      posting match under both entries and changed which name it was reported
      under, for no new match.
    * anything under three characters. "js", "ts" and "ml" are the whole of
      that set, and each is a substring of a real word away from a false
      positive with no context to catch it — "Next.js" is not a claim to
      JavaScript the language, and 500 ml is not machine learning. The
      lookaround a caller matches with cannot tell those apart; a longer alias
      carries enough of itself to be safe.
    """
    group = SKILL_ALIAS_INDEX.get(normalize_text(skill))
    if not group:
        return ()
    return tuple(
        spelling
        for spelling in group
        if spelling != skill and spelling not in known and len(spelling) >= 3
    )


__all__ = [
    "SKILL_ALIASES",
    "SKILL_ALIAS_INDEX",
    "alias_spellings",
    "canonical_skill",
    "normalize_text",
]
