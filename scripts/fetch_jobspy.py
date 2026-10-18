"""Pull postings from Indeed, LinkedIn, Glassdoor and Google Jobs via JobSpy.

This is the only source that scrapes. Indeed and LinkedIn both prohibit
automated access in their terms, and LinkedIn actively detects it. Account
restriction is a real risk.

Because of that, everything from this module is forced to 'review' status and
can never be auto-submitted, regardless of match score. That containment is
the point, so do not "optimize" it away.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
from normalize import clean_text, evaluate

# Search terms and locations live in criteria.yaml (search:). JobSpy needs a
# real country for Indeed and Glassdoor: "worldwide" is not a valid value and
# silently returns zero rows.
from normalize import criteria

SEARCHES = criteria()["search"]["terms"]
LOCATIONS = [tuple(x) for x in criteria()["search"]["locations"]]


def scrape(sites: list[str], results: int, hours: int) -> list[dict]:
    from jobspy import scrape_jobs  # imported lazily, it is slow to load

    out: list[dict] = []
    for term in SEARCHES:
        for location, country in LOCATIONS:
            try:
                df = scrape_jobs(
                    site_name=sites,
                    search_term=term,
                    location=location,
                    results_wanted=results,
                    hours_old=hours,
                    country_indeed=country,
                    linkedin_fetch_description=True,
                    verbose=0,
                )
            except Exception as exc:
                print(f"  {term} @ {location}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                continue
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                def val(key, default=""):
                    v = r.get(key, default)
                    return default if v is None or str(v) == "nan" else v

                salary_note = ""
                if val("min_amount"):
                    salary_note = (
                        f"\nSalary: {val('min_amount')} - {val('max_amount')} "
                        f"{val('currency', 'USD')} per {val('interval', 'year')}"
                    )
                out.append({
                    "source": f"jobspy_{val('site')}",
                    "ats": val("site"),
                    "ats_job_id": str(val("id")),
                    "company": str(val("company")),
                    "title": str(val("title")),
                    "location": str(val("location")) or location,
                    "is_remote": int(bool(r.get("is_remote"))),
                    "description": clean_text(str(val("description"))) + salary_note,
                    "url": val("job_url"),
                    "apply_url": val("job_url_direct") or val("job_url"),
                    "apply_channel": "manual",
                    "employment_type": str(val("job_type")),
                    "posted_at": str(val("date_posted")),
                })
            print(f"  {term} @ {location}: {len(df)} rows", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", default="indeed,linkedin",
                    help="comma separated: indeed,linkedin,glassdoor,google,zip_recruiter")
    ap.add_argument("--results", type=int, default=20)
    ap.add_argument("--hours", type=int, default=168, help="max posting age")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init()
    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    print(f"scraping {sites} across {len(SEARCHES)} terms x {len(LOCATIONS)} locations",
          file=sys.stderr)

    jobs = scrape(sites, args.results, args.hours)

    total = new = queued = rejected = 0
    for job in jobs:
        total += 1
        verdict = evaluate(job)
        if verdict["filter_status"] == "rejected":
            rejected += 1
        else:
            # Containment: scraped sources never auto-submit.
            verdict["filter_status"] = "review"
            verdict["filter_reason"] = (
                (verdict.get("filter_reason") or "") + " | scraped source, review only"
            )
            queued += 1
        if not args.dry_run:
            _, was_new = db.upsert_job(verdict)
            new += int(was_new)

    print(f"\nscraped     {total}")
    print(f"new rows    {new}")
    print(f"queued      {queued}  (review only, never auto-submitted)")
    print(f"rejected    {rejected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
