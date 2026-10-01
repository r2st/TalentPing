"""AI email composition — personalized recruiter outreach and reply drafting.

Prompts follow research §3.3: use the candidate profile + recruiter context,
keep it under ~130 words, one low-friction CTA, peer-to-peer (not supplicant)
tone. A deterministic template fallback is used when no LLM is configured so the
product still works (and tests run) without network access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import settings
from app.services import untrusted
from app.services.openrouter_client import (
    OpenRouterError,
    chat_completion,
    llm_is_configured,
    looks_like_reasoning,
    strip_code_fence,
)


@dataclass
class CandidateContext:
    name: str
    headline: str | None = None
    years_experience: int | None = None
    seniority: str | None = None
    location: str | None = None
    skills: list[str] | None = None
    target_roles: list[str] | None = None
    recent_roles: list[str] | None = None
    resume_excerpt: str | None = None

    @classmethod
    def from_resume(cls, resume, fallback_name: str) -> CandidateContext:
        """Build the composer's view of a candidate from a parsed resume."""
        if resume is None:
            return cls(name=fallback_name)
        recent = [
            f"{e.get('title')} at {e.get('company')}" if e.get("company") else e.get("title")
            for e in (resume.experience or [])[:3]
            if e.get("title")
        ]
        return cls(
            name=resume.full_name or fallback_name,
            headline=resume.headline,
            years_experience=resume.years_experience,
            seniority=resume.seniority,
            location=resume.location,
            skills=resume.skills,
            target_roles=resume.target_roles,
            recent_roles=recent,
            resume_excerpt=(resume.raw_text or "")[:900] or None,
        )


@dataclass
class RecruiterContext:
    name: str | None = None
    company: str | None = None
    title: str | None = None
    specialization: str | None = None
    industry: str | None = None

    @classmethod
    def from_recruiter(cls, recruiter) -> RecruiterContext:
        return cls(
            name=recruiter.name,
            company=recruiter.company,
            title=recruiter.title,
            specialization=recruiter.specialization,
            industry=recruiter.industry,
        )


@dataclass
class ComposedEmail:
    subject: str
    body: str


# How each tone is described *to the model*, and how the template fallback
# opens. Both paths read the same table so the two can't drift into meaning
# different things by the same name.
_TONES: dict[str, str] = {
    "peer": (
        "confident and peer-to-peer — an experienced person writing to another, "
        "never deferential"
    ),
    "warm": (
        "warm and human — friendly and personable, but still specific and "
        "professional; no gushing"
    ),
    "direct": (
        "extremely direct — no throat-clearing, no pleasantries, straight to the "
        "point in the first sentence"
    ),
    "formal": (
        "formal and precise — full sentences, no contractions, no exclamation "
        "marks"
    ),
}
DEFAULT_TONE = "peer"

# Word ceilings. "brief" is a genuinely different email, not a trimmed one.
_LENGTHS: dict[str, int] = {"brief": 80, "standard": 130}
DEFAULT_LENGTH = "standard"

# The single closing ask. One CTA is the rule; this picks which one.
_CTAS: dict[str, str] = {
    "call": "ask for a short call (e.g. 'Would 15 minutes this week make sense?')",
    "reply": (
        "ask only for a reply (e.g. 'Worth a conversation?') — do not propose a "
        "meeting or a time"
    ),
    "referral": (
        "ask to be pointed to the right person (e.g. 'Are you the right person "
        "for this, or is there someone better?')"
    ),
}
DEFAULT_CTA = "call"

# What the template fallback closes with, mirroring _CTAS above.
_CTA_SENTENCES: dict[str, str] = {
    "call": "Would a 15-minute call this week make sense?",
    "reply": "Worth a conversation?",
    "referral": "Are you the right person for this, or is there someone better?",
}


@dataclass
class Personalization:
    """The user's own settings for how their outreach should read.

    Every field has a working default, so callers that don't care can pass
    nothing and get exactly the email the composer produced before this existed.

    The important property is that these reach the *template fallback* as well
    as the prompt. A tone setting that quietly stops applying the moment the LLM
    is unreachable would be a preference the product only pretends to honour —
    and the fallback runs on every install with no OpenRouter key at all.
    """

    tone: str = DEFAULT_TONE
    length: str = DEFAULT_LENGTH
    cta: str = DEFAULT_CTA
    sign_off: str | None = None
    highlights: list[str] | None = None
    custom_instructions: str | None = None

    @classmethod
    def from_preference(cls, pref) -> Personalization:
        """Read the options off an AutopilotPreference row (or its absence)."""
        if pref is None:
            return cls()
        return cls(
            tone=getattr(pref, "outreach_tone", None) or DEFAULT_TONE,
            length=getattr(pref, "outreach_length", None) or DEFAULT_LENGTH,
            cta=getattr(pref, "outreach_cta", None) or DEFAULT_CTA,
            sign_off=getattr(pref, "outreach_sign_off", None),
            highlights=getattr(pref, "outreach_highlights", None) or [],
            custom_instructions=getattr(pref, "outreach_custom_instructions", None),
        )

    # Unknown values fall back rather than raising: these are stored strings,
    # and a preference row written by an older build must never be able to break
    # sending.
    @property
    def tone_text(self) -> str:
        return _TONES.get(self.tone, _TONES[DEFAULT_TONE])

    @property
    def word_cap(self) -> int:
        return _LENGTHS.get(self.length, _LENGTHS[DEFAULT_LENGTH])

    @property
    def cta_text(self) -> str:
        return _CTAS.get(self.cta, _CTAS[DEFAULT_CTA])

    @property
    def cta_sentence(self) -> str:
        return _CTA_SENTENCES.get(self.cta, _CTA_SENTENCES[DEFAULT_CTA])

    @property
    def closing(self) -> str:
        return (self.sign_off or "Best").strip().rstrip(",")


def _system_prompt(opts: Personalization) -> str:
    """The composer's instructions, with the user's settings folded in."""
    prompt = (
        "You are an expert career copywriter helping a job seeker write a short, "
        "highly personalized cold email to a company's recruiting team. Write in "
        f"a tone that is {opts.tone_text}. Keep the body under "
        f"{opts.word_cap} words. Lead with the candidate's most relevant proof "
        "point for this company, not with a request. End with exactly one "
        f"low-friction call to action: {opts.cta_text}. "
        "When the recruiter's name is unknown, greet the team ('Hi <Company> "
        "team,') rather than inventing a person. Never invent facts about the "
        "candidate or claim knowledge of specific open roles. Return the email "
        "as: first line 'Subject: ...', then a blank line, then the body."
    )
    if opts.sign_off:
        prompt += f" Sign off with '{opts.sign_off}' followed by the candidate's name."
    if opts.custom_instructions:
        # Appended last so it can steer, and labelled so the model treats it as
        # the writer's brief rather than as facts about the candidate.
        prompt += (
            " The candidate has also asked for the following, which you should "
            "follow unless it conflicts with the rules above: "
            f"{opts.custom_instructions.strip()}"
        )
    return prompt


def _build_user_prompt(
    cand: CandidateContext,
    rec: RecruiterContext,
    opts: Personalization | None = None,
) -> str:
    years = cand.years_experience if cand.years_experience is not None else "n/a"
    parts = [
        f"Candidate name: {cand.name}",
        f"Candidate headline: {cand.headline or 'n/a'}",
        f"Seniority: {cand.seniority or 'n/a'}",
        f"Years of experience: {years}",
        f"Location: {cand.location or 'n/a'}",
        f"Key skills: {', '.join((cand.skills or [])[:12]) or 'n/a'}",
        f"Target roles: {', '.join(cand.target_roles or []) or 'n/a'}",
        f"Recent roles: {'; '.join(cand.recent_roles or []) or 'n/a'}",
        # The candidate's own record ends here. Everything about the recruiter
        # was written by somebody else — the discovery crawl reads the name,
        # title and company off an employer's page, and an inbound thread takes
        # them off a From header a stranger controls, which
        # `recruiter_classifier`'s own tests call "a perfectly good place to put
        # a sentence aimed at the model". What comes out of this call is an
        # email sent from the candidate's own Gmail over their name, so the
        # fence goes round it — see :mod:`app.services.untrusted`.
        #
        # The "greet the team" rule stays outside it. It is an instruction to
        # the model about what to do with a missing name, and an instruction
        # inside a fence is the one thing the clause tells the model to ignore.
        "The recruiter, as this product recorded them. If the name below is "
        "n/a, greet the team rather than inventing a person:",
        untrusted.fence(
            "\n".join(
                [
                    f"Name: {rec.name or 'n/a'}",
                    f"Company: {rec.company or 'n/a'}",
                    f"Title: {rec.title or 'n/a'}",
                    f"Specialization/industry: "
                    f"{rec.specialization or rec.industry or 'n/a'}",
                ]
            ),
            label="recruiter record",
        ),
    ]
    if cand.resume_excerpt:
        parts.append(f"Resume excerpt: {cand.resume_excerpt[:800]}")
    # Offered as material the model *may* use, explicitly conditional on the
    # resume supporting it. The user typing "led the billing rewrite" into a
    # settings box is not evidence they did, and the one thing this composer
    # must never do is assert something the candidate then has to defend.
    if opts is not None and opts.highlights:
        joined = "; ".join(h.strip() for h in opts.highlights[:5] if h.strip())
        if joined:
            parts.append(
                "Candidate would like these worked in where they fit naturally "
                f"and are supported by the material above: {joined}"
            )
    return "\n".join(parts)


#: The subject line both composer prompts ask for, as the model actually writes
#: it back. The prompt says "first line 'Subject: ...'", and the label is the
#: only part of that instruction a model reliably keeps.
#:
#: Leading whitespace is here because the old test read ``text.splitlines()[0]``
#: while handing ``text.strip()`` to the body — so a completion that opened
#: with a newline, which is how a great many of them start, failed the test and
#: had its own label delivered as the first line of a cold email to a recruiter:
#: "Subject: Ada Lovelace — Senior Backend Engineer", and then the greeting.
#: That is the visible half of the failure. The other half is that the mail also
#: went out under the generic fallback subject, which is the one the model was
#: asked to improve on.
#:
#: The markdown emphasis is here because an instruct model told to lead with a
#: label leads with a bolded one — ``**Subject:**`` — often enough that it is
#: not an edge case. The closing marks are matched independently of the opening
#: ones rather than back-referenced: a model that opens with ``**`` and closes
#: with a single ``*`` has still labelled the line, and refusing it buys
#: nothing.
_SUBJECT_LINE_RE = re.compile(r"^[*_\s]*subject[*_\s]*:\s*", re.I)

def _parse_llm_output(text: str, fallback_subject: str) -> ComposedEmail:
    """Split "Subject: ... / blank / body" back into its two halves.

    Falls back to *fallback_subject* when the model wrote no label — but it
    must not fall back merely because the model wrote one untidily, because the
    label is then still sitting at the top of the body when the mail goes out.
    """
    # A fence around the whole email put ``` on the first line and pushed the
    # subject label to the second, where nothing looked for it.
    cleaned = strip_code_fence(text)
    lines = cleaned.splitlines()
    if not lines:
        return ComposedEmail(subject=fallback_subject, body="")

    label = _SUBJECT_LINE_RE.match(lines[0])
    if not label:
        return ComposedEmail(subject=fallback_subject, body=cleaned)

    # Trailing emphasis: "**Subject:** Backend role**" is rare, but a bolded
    # *value* — "Subject: **Backend role**" — is not.
    subject = lines[0][label.end() :].strip().strip("*_").strip()
    return ComposedEmail(
        subject=subject or fallback_subject,
        body="\n".join(lines[1:]).strip(),
    )


def _greeting(rec: RecruiterContext, opts: Personalization) -> str:
    """The opener, in the user's chosen register."""
    if opts.tone == "formal":
        if rec.name:
            return f"Dear {rec.name},"
        return f"Dear {rec.company} hiring team," if rec.company else "Dear hiring team,"
    if rec.name:
        return f"Hi {rec.name},"
    return f"Hi {rec.company} team," if rec.company else "Hi there,"


def _template_fallback(
    cand: CandidateContext,
    rec: RecruiterContext,
    opts: Personalization | None = None,
) -> ComposedEmail:
    """Deterministic, personalized-ish email used when no LLM is available.

    Honours tone, length, CTA and sign-off the same way the prompt does — see
    :class:`Personalization` for why the fallback has to.
    """
    opts = opts or Personalization()
    role = (cand.target_roles or ["a new role"])[0]
    greeting = _greeting(rec, opts)
    company_clause = f" at {rec.company}" if rec.company else ""
    skills = ", ".join((cand.skills or [])[:3]) or "my background"
    exp = (
        f"{cand.years_experience} years of experience"
        if cand.years_experience is not None
        else "solid experience"
    )
    subject = f"{cand.name} — {role}"

    opening = f"I'm {cand.name}, a {cand.headline or role.lower()} with {exp} in {skills}."
    if opts.tone == "direct":
        # "brief" is a shorter email; "direct" is a different one — it drops the
        # rapport sentence entirely rather than trimming it.
        middle = f"I'm exploring {role} opportunities{company_clause}."
    elif opts.length == "brief":
        middle = f"I'm exploring {role} opportunities and wanted to reach out directly."
    else:
        middle = (
            f"I've been following the work happening{company_clause} and wanted to "
            f"reach out directly rather than through a job board.\n\n"
            f"I'm exploring {role} opportunities and think I could add real value "
            f"to your team."
        )

    body = (
        f"{greeting}\n\n"
        f"{opening} {middle} {opts.cta_sentence}\n\n"
        f"{opts.closing},\n{cand.name}"
    )
    return ComposedEmail(subject=subject, body=body)


def compose_outreach(
    cand: CandidateContext,
    rec: RecruiterContext,
    opts: Personalization | None = None,
) -> ComposedEmail:
    """Generate a personalized outreach email, falling back to a template.

    ``opts`` carries the user's own tone/length/CTA settings. Omitted, the
    composer produces exactly what it did before they existed.
    """
    opts = opts or Personalization()
    fallback_subject = f"{cand.name} — {(cand.target_roles or ['introduction'])[0]}"
    if not llm_is_configured():
        return _template_fallback(cand, rec, opts)
    try:
        text = chat_completion(
            [
                {"role": "system", "content": untrusted.guarded(_system_prompt(opts))},
                {"role": "user", "content": _build_user_prompt(cand, rec, opts)},
            ],
            model=settings.openrouter_model,
            temperature=0.7,
        )
        if looks_like_reasoning(text):
            # The free reasoning models sometimes hand back their scratchpad
            # instead of the email. Sending that to a recruiter is worse than
            # sending the template.
            return _template_fallback(cand, rec, opts)
        return _parse_llm_output(text, fallback_subject)
    except OpenRouterError:
        # Never fail the pipeline on an AI hiccup — degrade to the template.
        return _template_fallback(cand, rec, opts)


@dataclass
class JobContext:
    """The specific posting an auto-applied outreach is about."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    remote: bool | None = None


def _job_system_prompt(opts: Personalization) -> str:
    """As :func:`_system_prompt`, for outreach about one named posting."""
    prompt = (
        "You are an expert career copywriter helping a job seeker write a short, "
        "highly personalized cold email to a recruiter about ONE specific open "
        f"role the seeker found. Write in a tone that is {opts.tone_text}. "
        "Name the role naturally in the first sentence. Keep the body under "
        f"{opts.word_cap} words. Lead with the candidate's single most relevant "
        "proof point for THIS role. End with exactly one low-friction call to "
        f"action: {opts.cta_text}. When the recruiter's name is unknown, greet "
        "the team ('Hi <Company> team,'). Never invent facts about the "
        "candidate, never claim a requirement they don't have. Return the email "
        "as: first line 'Subject: ...', then a blank line, then the body."
    )
    if opts.sign_off:
        prompt += f" Sign off with '{opts.sign_off}' followed by the candidate's name."
    if opts.custom_instructions:
        prompt += (
            " The candidate has also asked for the following, which you should "
            "follow unless it conflicts with the rules above: "
            f"{opts.custom_instructions.strip()}"
        )
    return prompt


def _build_job_prompt(
    cand: CandidateContext,
    rec: RecruiterContext,
    job: JobContext,
    opts: Personalization | None = None,
) -> str:
    base = _build_user_prompt(cand, rec, opts)
    # Read off a crawled posting, same as the recruiter block above.
    extra = untrusted.fence(
        "\n".join(
            [
                f"Role applying for: {job.title or 'the open role'}",
                f"At company: {job.company or rec.company or 'n/a'}",
                f"Role location: {job.location or ('remote' if job.remote else 'n/a')}",
            ]
        ),
        label="job posting",
    )
    return f"{base}\nThe role this email is about:\n{extra}"


def compose_job_outreach(
    cand: CandidateContext,
    rec: RecruiterContext,
    job: JobContext,
    opts: Personalization | None = None,
) -> ComposedEmail:
    """Outreach about one specific posting, falling back to a template.

    The auto-apply pipeline's composer: unlike :func:`compose_outreach` (which
    pitches the candidate to a company in general), this references the exact
    role the pipeline matched and is about to apply to.
    """
    opts = opts or Personalization()
    role = job.title or (cand.target_roles or ["a role on your team"])[0]
    fallback_subject = f"{cand.name} — {role}"
    if not llm_is_configured():
        return _job_template_fallback(cand, rec, job, opts)
    try:
        text = chat_completion(
            [
                {"role": "system", "content": untrusted.guarded(_job_system_prompt(opts))},
                {"role": "user", "content": _build_job_prompt(cand, rec, job, opts)},
            ],
            model=settings.openrouter_model,
            temperature=0.7,
        )
        if looks_like_reasoning(text):
            return _job_template_fallback(cand, rec, job, opts)
        return _parse_llm_output(text, fallback_subject)
    except OpenRouterError:
        return _job_template_fallback(cand, rec, job, opts)


def _job_template_fallback(
    cand: CandidateContext,
    rec: RecruiterContext,
    job: JobContext,
    opts: Personalization | None = None,
) -> ComposedEmail:
    """Deterministic, role-specific email used when no LLM is available."""
    opts = opts or Personalization()
    role = job.title or (cand.target_roles or ["the open role"])[0]
    company = job.company or rec.company
    # The job fallback greets off the *posting's* company, which is the one the
    # candidate is writing about; RecruiterContext is only the sender's employer.
    greeting = _greeting(RecruiterContext(name=rec.name, company=company), opts)
    company_clause = f" at {company}" if company else ""
    skills = ", ".join((cand.skills or [])[:3]) or "my background"
    exp = (
        f"{cand.years_experience} years"
        if cand.years_experience is not None
        else "solid experience"
    )
    subject = f"{cand.name} — {role}"

    lead = f"I came across the {role} role{company_clause} and wanted to reach out directly."
    pitch = (
        f"I'm {cand.name}, a {cand.headline or role.lower()} with {exp} in {skills}"
    )
    if opts.length == "brief" or opts.tone == "direct":
        pitch += "."
    else:
        pitch += " — closely aligned with what the role calls for."

    body = (
        f"{greeting}\n\n"
        f"{lead} {pitch}\n\n"
        f"{opts.cta_sentence}\n\n"
        f"{opts.closing},\n{cand.name}"
    )
    return ComposedEmail(subject=subject, body=body)


_REPLY_SYSTEM_PROMPT = (
    "You are a job seeker's assistant drafting a brief, professional reply to a "
    "recruiter's message. Match the recruiter's tone, be concise, and move the "
    "conversation forward. Follow the intent-specific brief exactly. Never invent "
    "specifics the candidate hasn't stated (exact availability, salary numbers, "
    "acceptance of an offer). Return only the reply body — no subject, no preamble."
)

# The angle each classified intent takes, given to both the LLM and the fallback.
# Rejections get a graceful thank-you; offers get warmth without committing.
_REPLY_BRIEF: dict[str, str] = {
    "SCHEDULING": (
        "They want to schedule. Confirm enthusiasm and readiness, and offer broad "
        "flexibility (e.g. mornings this week or next) without inventing exact "
        "slots. Ask them to pick a time that suits them."
    ),
    "INTERESTED": (
        "They're positive but haven't proposed a time. Thank them, express genuine "
        "interest, and propose a short intro call as the next step."
    ),
    "QUESTION": (
        "They asked something. Acknowledge the question, answer only from what the "
        "candidate profile supports, and keep it short. If it needs specifics the "
        "profile doesn't have, say the candidate will follow up with detail."
    ),
    "OFFER": (
        "They've extended (or are discussing) an offer. Express sincere enthusiasm "
        "and gratitude. Do NOT accept or state any numbers — ask to see the full "
        "details in writing and propose a quick call to discuss."
    ),
    "NOT_INTERESTED": (
        "They declined. Send a gracious, no-pressure thank-you: appreciate their "
        "time, leave the door open for future roles, and ask nothing of them."
    ),
}

_REPLY_FALLBACKS: dict[str, str] = {
    "SCHEDULING": (
        "Hi,\n\nThanks for reaching out — I'd be glad to talk. I'm fairly flexible "
        "over the next week or two, mornings especially. Let me know a time that "
        "works on your end and I'll make it happen.\n\nBest,\n{name}"
    ),
    "INTERESTED": (
        "Hi,\n\nThank you for getting back to me — I'm genuinely interested. Would "
        "a short intro call in the next week make sense? I'm happy to work around "
        "your schedule.\n\nBest,\n{name}"
    ),
    "QUESTION": (
        "Hi,\n\nThanks for the note and the question. Happy to give you what you "
        "need — I'll follow up shortly with the specifics.\n\nBest,\n{name}"
    ),
    "OFFER": (
        "Hi,\n\nThank you so much — I'm thrilled to hear this. I'd love to see the "
        "full details in writing, and would welcome a quick call to talk it "
        "through. Really appreciate the opportunity.\n\nBest,\n{name}"
    ),
    "NOT_INTERESTED": (
        "Hi,\n\nThank you for taking the time to get back to me — I appreciate it. "
        "Should something aligned come up down the line, I'd be glad to reconnect. "
        "Wishing you and the team the best.\n\nBest,\n{name}"
    ),
}


def compose_reply(
    cand: CandidateContext, recruiter_message: str, intent: str
) -> str:
    """Draft a reply to a recruiter message given the classified intent.

    Intent-aware: a rejection gets a graceful thank-you, an offer gets warmth
    without committing to anything, a scheduling note gets flexible availability.
    Every draft is reviewed by the user before it sends (see the review queue) —
    this only produces the first draft.
    """
    fallback = _REPLY_FALLBACKS.get(intent, _REPLY_FALLBACKS["INTERESTED"]).format(
        name=cand.name
    )
    brief = _REPLY_BRIEF.get(intent, _REPLY_BRIEF["INTERESTED"])
    if not llm_is_configured():
        return fallback

    profile = ", ".join((cand.skills or [])[:8]) or "n/a"
    try:
        draft = chat_completion(
            [
                {
                    "role": "system",
                    "content": untrusted.guarded(_REPLY_SYSTEM_PROMPT),
                },
                {
                    "role": "user",
                    "content": (
                        f"Intent brief: {brief}\n"
                        "Recruiter message:\n"
                        + untrusted.fence(
                            recruiter_message[:2000], label="recruiter email"
                        )
                        + f"\n\nCandidate name: {cand.name}\n"
                        f"Candidate headline: {cand.headline or 'n/a'}\n"
                        f"Candidate skills: {profile}"
                    ),
                },
            ],
            model=settings.openrouter_model,
            temperature=0.6,
            max_tokens=1500,
        )
    except OpenRouterError:
        return fallback
    # Same guard as the outreach path: never hand a model's scratchpad to a user.
    return fallback if looks_like_reasoning(draft) else draft.strip()
