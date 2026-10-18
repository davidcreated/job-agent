"""Re-run the filter over every stored posting.

Needed whenever criteria.yaml or normalize.py changes, since upsert_job skips
fingerprints it has already seen and would otherwise leave stale verdicts in
place. Prints what changed so a filter edit is never applied blind.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
from normalize import evaluate

FIELDS = (
    "filter_status", "filter_reason", "region", "eligibility", "salary_min",
    "salary_max", "salary_currency", "salary_period", "salary_usd_mo",
    "salary_tier",
)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-scored", action="store_true",
                    help="also re-filter postings the LLM already judged. "
                         "This DISCARDS those verdicts. Rescore afterwards.")
    args = ap.parse_args()
    db.init()
    conn = db.connect()

    # Only postings the LLM has NOT judged. The cheap filter is a coarse
    # pre-screen; the LLM verdict is strictly better informed, and it is the
    # only thing that knows whether an employer will consider a home-region
    # candidate. Re-running the filter over scored rows overwrote 130 correct
    # rejections, including roles explicitly restricted to US and Finland
    # residents, and destroyed the eligibility column while doing it.
    rows = conn.execute(
        "SELECT j.* FROM jobs j LEFT JOIN scores s ON s.job_id = j.id "
        "WHERE s.job_id IS NULL" if not args.force_scored else
        "SELECT * FROM jobs"
    ).fetchall()
    scored = conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"]
    print(f"skipping {scored} postings already judged by the LLM "
          f"(use --force-scored to override, which discards those verdicts)")

    moves: dict[tuple[str, str], int] = {}
    changed = 0
    for row in rows:
        job = dict(row)
        verdict = evaluate({
            "title": job.get("title"),
            "description": job.get("description"),
            "location": job.get("location"),
            "is_remote": job.get("is_remote"),
        })
        if verdict["filter_status"] == job["filter_status"] and \
           verdict.get("filter_reason") == job["filter_reason"]:
            continue
        if verdict["filter_status"] != job["filter_status"]:
            key = (job["filter_status"], verdict["filter_status"])
            moves[key] = moves.get(key, 0) + 1
        conn.execute(
            f"UPDATE jobs SET {','.join(f'{f}=?' for f in FIELDS)} WHERE id=?",
            [verdict.get(f) for f in FIELDS] + [job["id"]],
        )
        changed += 1
    conn.commit()

    print(f"re-evaluated {len(rows)} postings, {changed} updated")
    if moves:
        print("\nstatus transitions:")
        for (old, new), n in sorted(moves.items(), key=lambda kv: -kv[1]):
            arrow = "RECOVERED" if old == "rejected" else ""
            print(f"  {old:9} -> {new:9} {n:>5}  {arrow}")

    print("\ncurrent totals:")
    for status in ("passed", "review", "rejected"):
        n = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE filter_status=?", (status,)
        ).fetchone()["n"]
        print(f"  {status:9} {n:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
