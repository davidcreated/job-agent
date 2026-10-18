"""Generate a tailored cover letter per job, grounded in real CV evidence.

The prompt is the product here. Two failure modes to design against:

  1. Invention. A model given a CV and a job description will happily claim
     experience the candidate does not have. Every factual claim must trace to
     the evidence list in profile.yaml, which is why the evidence is passed as
     a closed set and the model is told it may not add to it.

  2. Slop. The default register for this task is corporate filler that reads
     as machine-written and gets skimmed past. The banned-phrase list is not
     decoration; it is most of what makes the output usable.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import llm

ROOT = Path(__file__).resolve().parent.parent

SCHEMA = {
    "type": "object",
    "properties": {
        "body": {"type": "string"},
        "evidence_used": {"type": "array", "items": {"type": "string"}},
        "word_count": {"type": "integer"},
    },
    "required": ["body", "evidence_used", "word_count"],
}

SYSTEM = """You write cover letters for one specific engineer. You write like a
competent person who respects the reader's time, not like a template.

HARD RULES

Every factual claim must come from the EVIDENCE list you are given. You may
rephrase and select from it. You may not add achievements, technologies,
employers, metrics or dates that are not in it. If the job asks for something
the candidate lacks, either omit it or address the gap honestly in one clause.
Never claim experience that is not in the evidence.

Never use these openings:
  "I am writing to express my interest"
  "I am excited to apply"
  "I was thrilled to see"
  "As a passionate..."
Open with a concrete, specific claim that connects the candidate's actual work
to this specific role.

Banned words and phrases: passionate, thrilled, excited, delve, leverage,
synergy, dynamic, fast-paced, seamless, robust ecosystem, game-changer,
cutting-edge, world-class, rockstar, ninja, "perfect fit", "I believe I would
be", "proven track record", "wealth of experience", "hit the ground running".

Do not use dashes as connectors in sentences. Use commas, semicolons or full
stops instead.

Do not restate the job description back at the reader. They wrote it.

Do not end with "I look forward to hearing from you" or any variant. End on
substance.

STRUCTURE
Three or four short paragraphs. Under the word limit given. Plain text, no
markdown, no headers, no bullet points. No greeting line and no sign-off; those
are added separately.

First paragraph: the single strongest reason this candidate fits this role,
with a specific number or shipped product from the evidence.
Middle: one or two concrete pieces of evidence that map onto what the posting
actually asks for. Name the technology only where the posting names it.
Last: why this company and role specifically, based on what the posting says.
One sentence on availability if relevant."""


def load_profile() -> dict:
    return yaml.safe_load((ROOT / "config" / "profile.yaml").read_text())


def build_prompt(profile: dict, job: dict, score_row: dict | None) -> str:
    prof = profile["professional"]
    ident = profile["identity"]
    max_words = profile["cv"]["max_words"]

    strengths = (score_row or {}).get("strengths") or ""
    gaps = (score_row or {}).get("gaps") or ""

    return (
        f"CANDIDATE\n"
        f"{ident['full_name']}, {prof['headline']}\n"
        f"{prof['years_experience']}+ years. Currently {prof['current_title']} "
        f"at {prof['current_company']}. Based in {ident['city']}, {ident['country']}.\n"
        f"Available: {prof['earliest_start']} (notice {prof['notice_period']}).\n\n"
        f"EVIDENCE (closed set, do not add to it)\n"
        + "\n".join(f"- {e}" for e in profile["evidence"])
        + f"\n\nTARGET ROLE\n"
        f"Company: {job['company']}\n"
        f"Title: {job['title']}\n"
        f"Location: {job.get('location') or 'not stated'}\n\n"
        f"POSTING\n{(job.get('description') or '')[:9000]}\n\n"
        + (f"SCREENING NOTES\nStrengths: {strengths}\nGaps: {gaps}\n\n"
           if strengths or gaps else "")
        + f"Write the cover letter body. Maximum {max_words} words. "
        f"Report which evidence items you used, verbatim, in evidence_used."
    )


BANNED = [
    "passionate", "thrilled", "excited", "delve", "leverage", "synergy",
    "fast-paced", "seamless", "game-changer", "cutting-edge", "world-class",
    "perfect fit", "proven track record", "wealth of experience",
    "hit the ground running", "i am writing to express",
    "look forward to hearing",
]


def lint(body: str, max_words: int) -> list[str]:
    """Cheap post-check. The model follows the rules most of the time, not all."""
    problems = []
    low = body.lower()
    for phrase in BANNED:
        if phrase in low:
            problems.append(f"banned phrase: {phrase}")
    words = len(body.split())
    if words > max_words + 40:
        problems.append(f"too long: {words} words (limit {max_words})")
    if " - " in body or " -- " in body or "—" in body:
        problems.append("dash used as a connector")
    if body.lstrip().lower().startswith(("dear ", "hello", "hi ")):
        problems.append("contains a greeting line")
    return problems


def write_one(job: dict, profile: dict, score_row: dict | None):
    max_words = profile["cv"]["max_words"]
    try:
        out = llm.generate(
            build_prompt(profile, job, score_row),
            schema=SCHEMA, system=SYSTEM, max_tokens=6000, temperature=0.6,
        )
        body = (out.get("body") or "").strip()
        return job["id"], body, lint(body, max_words), out.get("evidence_used") or [], None
    except Exception as exc:
        return job["id"], None, [], [], f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--regenerate", action="store_true")
    ap.add_argument("--min-score", type=int, help="override the apply threshold")
    ap.add_argument("--show", action="store_true", help="print each letter")
    args = ap.parse_args()

    db.init()
    conn = db.connect()
    profile = load_profile()

    threshold = args.min_score
    if threshold is None:
        from normalize import criteria
        threshold = criteria()["scoring"]["review_threshold"]

    where = "j.filter_status IN ('passed','review') AND s.score >= ?"
    params: list = [threshold]
    if not args.regenerate:
        where += " AND (a.cover_letter IS NULL OR a.cover_letter = '')"

    rows = conn.execute(
        f"SELECT j.*, s.score, s.strengths, s.gaps FROM jobs j "
        f"JOIN scores s ON s.job_id = j.id "
        f"LEFT JOIN applications a ON a.job_id = j.id "
        f"WHERE {where} ORDER BY CASE WHEN j.filter_status = 'passed' THEN 0 ELSE 1 END, CASE WHEN j.posted_at IS NOT NULL AND julianday('now') - julianday(j.posted_at) <= 2 THEN 0 WHEN j.posted_at IS NOT NULL AND julianday('now') - julianday(j.posted_at) <= 7 THEN 1 ELSE 2 END, s.score DESC",
        params,
    ).fetchall()
    jobs = [dict(r) for r in rows]
    if args.limit:
        jobs = jobs[: args.limit]

    if not jobs:
        print("nothing to write. Run scripts/score.py first.")
        return 0

    print(f"writing {len(jobs)} cover letters (score >= {threshold})\n")
    shadow = db.shadow_mode()
    written = flagged = errors = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = pool.map(
            lambda j: write_one(j, profile, {"strengths": j.get("strengths"),
                                             "gaps": j.get("gaps")}),
            jobs,
        )
        by_id = {j["id"]: j for j in jobs}
        for job_id, body, problems, evidence, err in results:
            job = by_id[job_id]
            if err:
                errors += 1
                print(f"  ERROR {job['company']}: {err[:90]}")
                continue
            written += 1
            if problems:
                flagged += 1

            conn.execute(
                "INSERT INTO applications (job_id, status, channel, cover_letter, "
                "cv_variant, queued_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                "cover_letter=excluded.cover_letter, "
                # Never downgrade a terminal status. Overwriting 'submitted'
                # with 'shadow' erased the record of a real application, which
                # reset the daily cap and made the duplicate check pass, so the
                # next run would have applied to the same company twice.
                "status=CASE WHEN applications.status IN "
                "  ('submitted','approved','rejected_by_user') "
                "  THEN applications.status ELSE excluded.status END, "
                "queued_at=excluded.queued_at",
                (job_id, "shadow" if shadow else "queued", job.get("apply_channel"),
                 body, profile["cv"]["base_pdf"], db.now()),
            )
            conn.commit()

            flag = f"  LINT: {'; '.join(problems)}" if problems else ""
            print(f"  [{job['score']:>3}] {job['company'][:22]:24} "
                  f"{job['title'][:38]:40} {len(body.split()):>3}w{flag}")
            if args.show:
                print("\n" + body + "\n" + "-" * 70)

    print(f"\nwritten   {written}")
    print(f"flagged   {flagged}  (lint problems, review these)")
    print(f"errors    {errors}")
    print(f"status    {'shadow (nothing will be sent)' if shadow else 'QUEUED FOR SENDING'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
