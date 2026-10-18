"""Score candidate postings against the CV with an LLM (see llm.py).

Two jobs, and the second matters more than the first:

  1. How well does the candidate fit the role technically.
  2. Would this employer actually consider a candidate based in the home region.

The regex filter cannot answer (2). It leaves most postings as 'unknown'
because almost none of them state work authorization in a parseable way.
A wrong answer here is expensive: it either wastes an application on a role
that auto-rejects, or discards a role that would have hired.

Postings are scored in batches. The free tier grants roughly 20 requests per
day per model, so one request per job would cap the system at 20 jobs a day.
Batching turns that into 20 * BATCH_SIZE.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import llm
from normalize import criteria

ROOT = Path(__file__).resolve().parent.parent
BATCH_SIZE = 8
DESC_CHARS = 3500

RESULT = {
    "type": "object",
    "properties": {
        "job_ref": {"type": "integer"},
        "score": {"type": "integer"},
        "eligibility": {
            "type": "string",
            "enum": ["global_remote", "eor", "sponsor", "home_region",
                     "us_only", "uk_only", "eu_only", "citizen_only", "unknown"],
        },
        "eligibility_evidence": {"type": "string"},
        "seniority_fit": {"type": "string", "enum": ["below", "match", "above"]},
        "stack_match": {"type": "string", "enum": ["strong", "partial", "weak"]},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
        "recommend": {"type": "string", "enum": ["apply", "review", "skip"]},
    },
    "required": ["job_ref", "score", "eligibility", "eligibility_evidence",
                 "seniority_fit", "stack_match", "strengths", "gaps",
                 "rationale", "recommend"],
}

SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": RESULT}},
    "required": ["results"],
}

SYSTEM = """You screen job postings for one specific engineer. You are strict.

Your most important judgment is eligibility: the candidate's location and work
authorization are given in the CANDIDATE block. A role that requires
in-country authorization, or that is tied to a specific office with no remote
provision, is unusable no matter how well the stack matches. Say so plainly
rather than being encouraging.

Classify eligibility strictly from evidence in the posting:
  global_remote - explicitly hires remotely from anywhere, or from the candidate's region
  eor           - hires contractors or via an employer of record
  home_region   - the role is in the candidate's own country or region
  sponsor       - offers visa sponsorship or relocation
  us_only / uk_only / eu_only - requires authorization in that region
  citizen_only  - citizenship or security clearance required
  unknown       - the posting genuinely does not say

Quote the phrase you based it on in eligibility_evidence. If the posting says
nothing about location eligibility, answer unknown and say so. Do not guess
generously.

Score 0-100 on overall fit, weighing eligibility heavily. A technically
perfect role the candidate cannot legally take is not a high score.

You will be given several postings at once. Return exactly one result per
posting, echoing its job_ref number. Judge each one independently."""


def work_auth_summary(p: dict) -> str:
    wa = p["work_authorization"]
    ok = [k for k, v in wa.items() if v == "authorized"]
    need = [k for k, v in wa.items() if v == "requires_sponsorship" and k != "default"]
    return (f"authorized in {', '.join(ok) or 'no listed country'}; "
            f"requires sponsorship for {', '.join(need) or 'all others'}.")


def build_profile() -> str:
    p = yaml.safe_load((ROOT / "config" / "profile.yaml").read_text())
    prof, evidence = p["professional"], p["evidence"]
    return (
        f"CANDIDATE\n"
        f"{p['identity']['full_name']}, based in {p['identity']['city']}, "
        f"{p['identity']['country']}.\n"
        f"{prof['headline']}\n"
        f"{prof['years_experience']}+ years. Currently {prof['current_title']} "
        f"at {prof['current_company']}.\n"
        f"Primary stack: {prof['primary_stack']}. Notice period: "
        f"{prof['notice_period']}.\n"
        f"Open to: {', '.join(prof['open_to'])}.\n"
        f"Work authorization: {work_auth_summary(p)}\n\n"
        f"EVIDENCE (the only achievements you may treat as true):\n"
        + "\n".join(f"- {e}" for e in evidence)
    )


def build_batch_prompt(profile: str, batch: list[dict]) -> str:
    blocks = []
    for i, job in enumerate(batch, start=1):
        blocks.append(
            f"--- JOB {i} ---\n"
            f"job_ref: {i}\n"
            f"Company: {job['company']}\n"
            f"Title: {job['title']}\n"
            f"Location: {job.get('location') or 'not stated'}\n"
            f"Remote flag: {'yes' if job.get('is_remote') else 'no/unknown'}\n"
            f"Salary parsed: {job.get('salary_tier') or 'unlisted'}\n"
            f"Description:\n{(job.get('description') or '')[:DESC_CHARS]}"
        )
    return (
        f"{profile}\n\n"
        f"Score each of the following {len(batch)} postings. "
        f"Return exactly {len(batch)} results, one per job_ref.\n\n"
        + "\n\n".join(blocks)
    )


# Sources whose terms prohibit automated applying. A high score must never
# promote one of these to auto-apply.
SCRAPED_PREFIX = "jobspy"


def apply_verdict(conn, job: dict, out: dict, thresholds: tuple[int, int],
                  reject_elig: set[str]) -> str:
    apply_at, review_at = thresholds
    score = int(out["score"])
    elig = out["eligibility"]
    scraped = str(job.get("source") or "").startswith(SCRAPED_PREFIX)

    if elig in reject_elig:
        status = "rejected"
        reason = f"LLM: not eligible ({elig}) - {out['eligibility_evidence'][:90]}"
    elif score >= apply_at and out["recommend"] == "apply":
        # The fetcher forces scraped sources to review. Promoting one here on
        # score alone silently undid that containment and made six Indeed
        # listings auto-apply eligible.
        status = "review" if scraped else "passed"
        reason = (f"LLM: score {score}, apply | scraped source, review only"
                  if scraped else f"LLM: score {score}, apply")
    elif score >= review_at:
        status = "review"
        reason = f"LLM: score {score}, {out['recommend']}"
    else:
        status = "rejected"
        reason = f"LLM: score {score}, skip"

    conn.execute(
        "INSERT INTO scores (job_id, score, rationale, strengths, gaps, model, "
        "scored_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET score=excluded.score, "
        "rationale=excluded.rationale, strengths=excluded.strengths, "
        "gaps=excluded.gaps, model=excluded.model, scored_at=excluded.scored_at",
        (job["id"], score, out["rationale"], " | ".join(out["strengths"]),
         " | ".join(out["gaps"]), out.get("_model", ""), db.now()),
    )
    conn.execute(
        "UPDATE jobs SET eligibility=?, filter_status=?, filter_reason=? WHERE id=?",
        (elig, status, reason, job["id"]),
    )
    return status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="score at most N postings")
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    db.init()
    conn = db.connect()
    where = "j.filter_status IN ('passed','review')"
    if not args.rescore:
        where += " AND s.job_id IS NULL"
    jobs = [dict(r) for r in conn.execute(
        f"SELECT j.* FROM jobs j LEFT JOIN scores s ON s.job_id = j.id "
        f"WHERE {where} ORDER BY COALESCE(j.posted_at, j.fetched_at) DESC"
    ).fetchall()]
    if args.limit:
        jobs = jobs[: args.limit]

    if not jobs:
        print("nothing to score")
        return 0

    profile = build_profile()
    crit = criteria()["scoring"]
    thresholds = (crit["apply_threshold"], crit["review_threshold"])
    reject_elig = set(criteria()["eligibility"]["reject"])

    batches = [jobs[i:i + args.batch] for i in range(0, len(jobs), args.batch)]
    print(f"scoring {len(jobs)} postings in {len(batches)} batches of "
          f"{args.batch}\navailable models: {llm.available_models()}\n")

    counts = {"passed": 0, "review": 0, "rejected": 0}
    scored = failed = 0

    for n, batch in enumerate(batches, start=1):
        try:
            out = llm.generate(
                build_batch_prompt(profile, batch),
                schema=SCHEMA, system=SYSTEM,
                max_tokens=32000, temperature=0.2,
            )
        except Exception as exc:
            failed += len(batch)
            print(f"  batch {n}/{len(batches)}: FAILED {type(exc).__name__}: "
                  f"{str(exc)[:120]}")
            continue

        results = {int(r["job_ref"]): r for r in out.get("results", [])
                   if r.get("job_ref") is not None}
        missing = 0
        for i, job in enumerate(batch, start=1):
            r = results.get(i)
            if not r:
                missing += 1
                continue
            status = apply_verdict(conn, job, r, thresholds, reject_elig)
            counts[status] += 1
            scored += 1
        conn.commit()
        note = f"  ({missing} missing from response)" if missing else ""
        failed += missing
        print(f"  batch {n}/{len(batches)}: {len(batch) - missing} scored{note}"
              f"   running: {counts}")

    print(f"\nscored    {scored}")
    print(f"failed    {failed}")
    print(f"passed    {counts['passed']}")
    print(f"review    {counts['review']}")
    print(f"rejected  {counts['rejected']}")
    print(f"models left today: {llm.available_models()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
