# How the system works

A map of what runs, in what order, and where the safety gates sit.

## The one-paragraph version

Every 15 minutes a watcher polls 97 ATS boards and 4 job feeds for new software engineering
roles. Cheap deterministic filters throw out roughly 99% of them. What
survives is scored by an LLM, which also answers the question regex cannot:
would this employer actually consider a candidate in your home region. High scorers
get a cover letter grounded in a closed set of CV facts and a CV restructured
for that specific posting. Then it either applies, or asks you first. Which of
those two happens is decided by rules you can read.

## The pipeline

```
                    ┌──────────────────────────────────────┐
  EVERY 15 MIN      │  watch.py  (launchd + caffeinate)    │
                    └──────────────────┬───────────────────┘
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                              ▼
  fetch_ats.py                  fetch_feeds.py                fetch_jobspy.py
  Greenhouse / Lever /          RemoteOK / Remotive /         Indeed / LinkedIn
  Ashby public APIs             WeWorkRemotely / HN           (scraped)
  97 boards                     public JSON + RSS             ToS-hostile
        │                              │                              │
        └──────────────────────────────┼──────────────────────────────┘
                                       ▼
                         ┌─────────────────────────────┐
                         │  normalize.py               │   no LLM, no cost
                         │  dedupe by fingerprint      │
                         │  title / region / salary    │
                         │  eligibility / dealbreakers │
                         └──────────────┬──────────────┘
                            ~99% rejected here
                                        ▼
                         ┌─────────────────────────────┐
                         │  score.py  →  LLM           │   8 jobs per request
                         │  fit 0-100 + eligibility    │
                         └──────────────┬──────────────┘
                                        ▼
                    ┌───────────────────┴───────────────────┐
            score >= 72                              score 60-71,
            clean source                             or scraped source,
                    │                                or visa sponsorship
                    ▼                                       ▼
         ┌────────────────────┐                  ┌────────────────────┐
         │  filter_status =   │                  │  filter_status =   │
         │     'passed'       │                  │     'review'       │
         │  auto-apply        │                  │  needs your tap    │
         └─────────┬──────────┘                  └─────────┬──────────┘
                   │                                       │
                   │                              notify.py → Telegram
                   │                                  Approve / Skip
                   │                                       │
                   └───────────────┬───────────────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │  cover_letter.py  → LLM      │  closed evidence set
                    │  render_cv.py                │  no LLM, deterministic
                    └──────────────┬───────────────┘
                                   ▼
                    ╔══════════════════════════════╗
                    ║      THE SUBMIT GATE         ║
                    ║  shadow mode                 ║
                    ║  kill switch (data/STOP)     ║
                    ║  daily cap (20)              ║
                    ║  per-company cap (2/month)   ║
                    ║  duplicate check             ║
                    ║  source containment          ║
                    ╚══════════════┬═══════════════╝
                                   ▼
                 ┌─────────────────┴─────────────────┐
                 ▼                                   ▼
        submit_email.py                      submit_form.py
        Gmail SMTP + CV                      Playwright
                                             screenshot before submit
                                             refuses on captcha
                                             refuses on unfilled required
```

## Why it is shaped this way

**Cheap filters before expensive ones.** 10,904 postings ingested, 58 reached
the LLM. Scoring everything would exhaust the free tier in minutes and cost
real money on a paid one.

**The LLM's real job is eligibility, not scoring.** Regex can tell a backend
role from a frontend role. It cannot tell whether a company will hire someone
in your country. Most postings never state it, so the model reads the whole posting
and quotes the phrase it based its answer on.

**Two gates, not one.** `filter_status` decides whether a human is needed.
The submit gate decides whether anything actually leaves the machine. They are
separate because upstream stages have twice silently changed their minds about
containment, and the second gate caught it both times.

**Freshness beats score.** Everything downstream orders by posting age band
first, score second. A 78 posted this morning goes out before a 91 from last
month, because the 91 is already applicant #400.

## Where each decision lives

| Question | File | Kind |
|---|---|---|
| Is this a role I want? | `config/criteria.yaml` → `titles` | config |
| Is the region allowed? | `config/criteria.yaml` → `regions` | config |
| Is the pay high enough? | `config/criteria.yaml` → `salary` | config |
| What score auto-applies? | `config/criteria.yaml` → `scoring` | config |
| How many per day? | `config/criteria.yaml` → `limits` | config |
| How are filters applied? | `scripts/normalize.py` | code |
| Would they hire from your country? | `scripts/score.py` prompt | LLM |
| What may the letter claim? | `config/profile.yaml` → `evidence` | config |
| What goes on the CV? | `config/cv.yaml` | config |
| What can never be sent? | `scripts/submit_email.py` guards | code |
| Form answers | `config/profile.yaml` + `submit_form.py` | both |

Anything in the first column marked config you can edit without touching code.
**After editing `criteria.yaml`, run `scripts/reevaluate.py`**, or stored
postings keep their old verdict.

## The safety model

Five independent things must all be true for an application to be sent.

1. `shadow_mode = 0` in the settings table
2. `data/STOP` does not exist
3. Under the daily cap, and under the per-company monthly cap
4. Not already applied to this company and title, checked again at send time
5. Either the source is clean and `filter_status = 'passed'`, or you approved
   it explicitly in Telegram

Scraped sources (Indeed, LinkedIn) can never satisfy 5 without your tap, no
matter how high they score. This was silently broken twice during the build,
in the scorer and then in the submitter, which is why the guard now lives at
the submit boundary and is covered by tests in both directions.

## What it will not do

- Answer visa, criminal history, disability, veteran, gender or race questions
  by guessing. They come from `profile.yaml` or they block the submission.
- Tick marketing, newsletter or talent-pool opt-ins.
- Solve captchas. It detects them and hands the job to you.
- Claim experience that is not in `profile.yaml` → `evidence`.
- Submit through Indeed or LinkedIn without your explicit approval.

## Failure modes worth knowing

| Symptom | Cause | Fix |
|---|---|---|
| Nothing being scored | Daily LLM quota (20/model/day) | Wait, quotas reset daily |
| Nothing submitted | Backlog is scraped or captcha'd | Normal, watch new postings |
| `watcher not loaded` | launchd agent unloaded | `launchctl load ~/Library/LaunchAgents/com.example.jobagent.plist` |
| Fetches stopped overnight | Laptop slept or lost wifi | A VPS removes this entirely |
| Form filled 0 fields | Apply button leads offsite, or Cloudflare | Check the screenshot in `data/screenshots/` |

## Data

Everything lives in `data/applications.db` (SQLite, WAL).

- `jobs` one row per posting, with `filter_status` and the reason
- `scores` LLM verdict, rationale, strengths, gaps
- `applications` cover letter, channel, status, submission time, error
- `events` audit trail of what happened and when
- `settings` shadow mode and watcher state

Nothing is deleted. A rejected posting keeps its rejection reason so you can
audit what the filter threw away, which is how five real bugs were found.

## Design decisions learned from real bugs

**The filter is tuned against false rejections, not false passes.** A posting
wrongly passed costs one LLM call. A posting wrongly rejected is an
opportunity that is never seen again. Five real bugs found this way are pinned
in `tests/test_filters.py`, each recovered from a posting the filter had
silently discarded.

**Location is authoritative for region.** Scanning a description for country
names rejected a Lithuania-based role because the company blurb mentioned an
India office.

**An implausible salary parse is treated as no salary.** "$250 billion in
transaction value" once became a $250/year salary and rejected three good
roles. Anything under $300/month is a parse failure, not a cheap job.

**Cover letters are grounded in a closed evidence set.** The model may select
and rephrase from `profile.yaml`, never add to it. A model handed a CV and a
job description will otherwise invent experience.

**Scraped sources can never auto-submit.** Anything from Indeed or LinkedIn is
forced to the review queue. Those terms prohibit automated access.

**The LLM provider has a 20 request/day free quota per model.** Handled with a
model rotation pool plus batched scoring, giving roughly 1,100 postings a day
on a free key.
