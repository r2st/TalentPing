# Proactive Job Search — finding roles and going after them unprompted

**Status:** Proposed
**Author:** engineering
**Last updated:** 2026-07-28
**Depends on:** autopilot (shipped), job search (shipped), fit scorer (shipped),
recruiter discovery (shipped), smart apply (shipped), reputation gate (shipped)

---

## 1. The finding that shapes this document

The ask was "search for relevant jobs, send emails or apply". Before designing
that, it is worth stating plainly: **it is already built, it is already running,
and it has applied to nothing.**

`auto_apply_tasks.run_all_autopilots` fires hourly in production. It calls
`auto_apply_service.run_user_autopilot`, which does the entire loop the request
describes — refresh the feed, score every posting against every active profile,
gate on relevance and location, find a recruiter, tailor a resume, write a cover
letter, send or fill the form, schedule follow-ups.

Here is what it actually returns, every hour, unchanged:

```
{'user_id': 1, 'status': 'ok', 'scanned': 42, 'applied': 0,
 'skipped_low_fit': 0, 'skipped_irrelevant': 42, 'skipped_location': 0,
 'skipped_no_contact': 0, 'skipped_duplicate': 0, 'budget': 4,
 'notes': ["Ritual Ads®: 'Executive Assistant' is not one of your target roles (ai engineer)",
           "Discovery Parks: 'Caretaker' is not one of your target roles (ai engineer)",
           "Lsn: 'Hand Assembly' is not one of your target roles (ai engineer)",
           "Trainvac: 'Take the initiative' is not one of your target roles (ai engineer)",
           "Vet Vision AI: 'We don't currently have any open roles' is not one of your target roles (ai engineer)", ...]}
```

Every number in that line is doing its job. The budget is there. The gates work —
an AI engineer should not be applying to a Caretaker vacancy, and the relevance
gate correctly refuses all forty-two. `applied: 0` is the *right answer to the
wrong feed*.

So this is not a feature to build. It is a feature to un-jam. Writing a second
discovery pipeline alongside the one already running would leave two systems
finding nothing instead of one.

### 1.1 Why the feed looks like that

Three separate causes, in the order they bite.

**The search can't say what it's looking for.** `job_search_service.matches_query`
is the pre-filter between the public boards and the scorer. Its fuzzy fallback
builds match words like this:

```python
words = [w for w in re.split(r"\W+", term) if len(w) > 3]
```

For a candidate targeting **"AI Engineer"**, `ai` is two characters and is
discarded. The search term degenerates to `engineer` — which is simultaneously
too broad (every engineering post on every board now matches on one word) and
unable to express the specialism that mattered. The same hole swallows `ML`,
`QA`, `SRE`, `BI`, `UX`, `iOS` and `dev`: short tokens are not noise words, they
are precisely the tokens that name a field.

**Nothing is ever screened out for good.** `_candidate_postings` selects every
posting at `status == NEW` with a passing fit score. A posting the relevance gate
rejected stays `NEW` forever, so it is re-fetched, re-judged and re-rejected on
every single run, for as long as it exists. That is why the numbers never move:
those 42 rows are the same 42 rows each hour. It also means the `notes` field —
the one thing the UI could show a user to explain the silence — is 42 lines of
the same rejection rather than anything actionable.

**Discovery is thin.** `PROVIDERS` is SerpApi, RemoteOK, Arbeitnow, Jobicy and a
LinkedIn scrape. `fetch_serpapi` returns `[]` immediately without
`SERPAPI_API_KEY`, and Google Jobs is the only one of the five that indexes
Indeed, LinkedIn and Workday. Without it, discovery is three small remote-first
boards, which for a niche specialism is close to no coverage at all.

---

## 2. Scope

**In scope**

* Make the pre-filter able to express short-token specialisms (`AI`, `ML`, `SRE`).
* Expand a candidate's stated roles into the synonyms boards actually use, so a
  search for "AI Engineer" also asks for "Machine Learning Engineer" and
  "LLM Engineer".
* Park screened-out postings so the autopilot stops re-judging them hourly, and
  so the user can see *why* a posting was passed over.
* Aggregate the run report into something a UI can show.

**Out of scope**

* A new discovery pipeline, new Celery tasks, or a new outreach path. The
  existing autopilot is the pipeline; this makes it able to find things.
* Changing what the relevance or location gates decide. They are correct.
* Paid job sources as a requirement. SerpApi stays optional; the feature must
  improve measurably without it.
* Scraping LinkedIn beyond what already ships (see the V2 invariant — no
  LinkedIn scraping is added here).

---

## 3. Design

### 3.1 Short tokens are content, not noise

Replace the length test with a stopword test. The reason the original filtered by
length was to drop `the`, `and`, `for`, `a` — words that carry no signal. The fix
is to say that directly rather than to approximate it with a ruler:

```python
_STOPWORDS = {"the", "and", "for", "with", "our", "your", ...}
words = [w for w in re.split(r"\W+", term) if w and w not in _STOPWORDS]
```

The two-thirds majority rule stays as it is. What changes is that "AI Engineer"
now needs *both* `ai` and `engineer` present rather than `engineer` alone, which
makes the filter both more specific and more expressive at the same time — it
rejects the generic engineering post it used to admit, and admits the AI role it
used to have no way to name.

### 3.2 Role expansion

A candidate writes down "AI Engineer". Boards advertise the same job as "Machine
Learning Engineer", "ML Engineer", "Applied Scientist", "LLM Engineer". A literal
search finds a fraction of the market.

`ensure_search` already re-synthesises the autopilot's search from the profiles
on every run, so this is one function inserted at that point:

```
roles = ["AI Engineer"]  ->  expand_roles(roles)  ->  ["AI Engineer",
                                                       "Machine Learning Engineer",
                                                       "ML Engineer",
                                                       "LLM Engineer", ...]
```

**Deterministic first, model second.** A static synonym table covers the common
specialisms and costs nothing; the LLM is asked only for roles the table doesn't
know, and the answer is cached on the profile so it is one call per profile ever
rather than one per scan. That ordering matters here more than usual — with every
provider rate-limited in production for days (see the recruiter-reply
investigation), any design whose *discovery* depends on a live model call
inherits an outage as a silent no-op.

The expansion widens the **feed** only. It does not widen the relevance gate,
which keeps judging against the roles the candidate actually wrote. A wide feed
scored selectively is the shape the existing code already documents in
`_search_criteria`; this just makes the feed as wide as it was meant to be.

### 3.3 Screening out is a decision, and decisions get recorded

Add to `JobPosting`:

| column | type | meaning |
| --- | --- | --- |
| `screened_out_at` | timestamptz, null | when the autopilot passed it over |
| `screened_out_reason` | text, null | the gate's own sentence, verbatim |

`_candidate_postings` gains `JobPosting.screened_out_at.is_(None)`, and
`run_user_autopilot` stamps the pair whenever a gate refuses a posting.

Three things fall out of it. The autopilot stops re-judging the same rejects
every hour, so its run report describes *this run*. The user gets an answer to
"why didn't you apply to that one?" that is the gate's own words. And a posting
that was screened out under old criteria can be un-screened when the criteria
change — clearing the column is how a profile edit puts a role back in play,
which is the behaviour `ensure_search` already promises for the search itself.

This is deliberately not a new `JobStatus`. Status is the *user's* pipeline
(new → interested → applied); being passed over by an automated gate is a fact
about our judgement, not a stage the candidate moved the job to, and conflating
them would make the two un-disentangleable later.

### 3.4 A run report worth showing

`AutoApplyResult.notes` currently accumulates one line per skipped posting and is
truncated in the log. Aggregate it: counts by reason, plus at most a handful of
examples per reason. "38 roles weren't in your target list (Executive Assistant,
Caretaker, Hand Assembly, …); 4 had no contact" is a sentence a user can act on.
Forty-two near-identical lines is not.

### 3.5 Sources

`SERPAPI_API_KEY` stays optional and unset by default. §3.1 and §3.2 are what
must carry the improvement, because they are what a deployment gets for free.
Document the key in the deployment notes as the single highest-leverage optional
source, and leave the decision to whoever pays for it.

---

## 4. Safety

Nothing in this document loosens a gate. It is worth being explicit, because a
change that makes an automated system apply to *more* jobs deserves the
paragraph:

* The **relevance gate** and **location gate** are untouched. They judge against
  the candidate's stated roles and places, not against the expanded feed.
* The **reputation gate**, the **warm-up ramp** and `daily_application_limit`
  still bound every send. `budget = min(send_headroom, apply_headroom)` is
  unchanged, so a wider feed changes *which* jobs get the day's four
  applications, never how many go out.
* `auto_send` remains per-user and off by default; with it off the applications
  land in review exactly as now.
* Role expansion cannot introduce a role the candidate didn't ask for, because
  the gate that admits an application still reads `targeting.roles`.

The failure mode this opens is a wider feed containing more things the gates then
reject — wasted scoring, not misdirected email.

---

## 5. Testing

* `matches_query` admits "AI Engineer" for an AI role and rejects a generic
  engineering post — the case the length rule got backwards.
* `expand_roles` returns the literal role first, is deterministic for known
  specialisms, and returns the input unchanged when no model is available.
* A screened-out posting is not returned by `_candidate_postings` on the next
  run, and clearing the column puts it back.
* An end-to-end autopilot run over a feed of one relevant and one irrelevant
  posting applies to exactly one, and reports the other with a reason.
* The daily budget still caps applications when the feed is large.

---

## 6. Rollout

1. §3.1 and §3.4 — pure logic, no migration, immediately visible in the run report.
2. §3.3 — one migration, backfilled as null (every existing posting stays in play).
3. §3.2 — the static table first; the model-backed expansion behind it.

The measure of success is the line this document opens with. `scanned: 42,
applied: 0, skipped_irrelevant: 42`, hour after hour with the same forty-two
rows, should become a run that considers new postings and either applies to some
or explains, in one sentence, why it didn't.
