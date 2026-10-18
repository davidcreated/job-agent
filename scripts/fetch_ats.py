"""Pull postings from the public ATS board APIs listed in config/companies.yaml.

Field mappings below were captured from live responses, not guessed. If an ATS
changes its shape this is the file that breaks, and it breaks loudly.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
from normalize import clean_text, evaluate

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon) job-agent/1.0"}
TIMEOUT = 25


def _ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError):
        return None


def fetch_greenhouse(slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = requests.get(url, timeout=TIMEOUT, headers=HEADERS).json()
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        offices = ", ".join(o.get("name", "") for o in j.get("offices") or [])
        out.append({
            "source": "greenhouse",
            "ats": "greenhouse",
            "ats_job_id": str(j.get("id")),
            "company": j.get("company_name") or slug,
            "title": j.get("title") or "",
            "location": loc or offices,
            "is_remote": int("remote" in f"{loc} {offices}".lower()),
            "description": clean_text(j.get("content")),
            "url": j.get("absolute_url"),
            "apply_url": j.get("absolute_url"),
            "apply_channel": "greenhouse",
            "posted_at": j.get("first_published") or j.get("updated_at"),
        })
    return out


def fetch_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = requests.get(url, timeout=TIMEOUT, headers=HEADERS).json()
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        loc = ", ".join(x for x in locs if x)
        body = "\n\n".join(filter(None, [
            j.get("descriptionPlain"),
            "\n".join(
                f"{s.get('text', '')}\n{clean_text(s.get('content'))}"
                for s in j.get("lists") or []
            ),
            j.get("additionalPlain"),
        ]))
        out.append({
            "source": "lever",
            "ats": "lever",
            "ats_job_id": j.get("id"),
            "company": slug.replace("-", " ").title(),
            "title": j.get("text") or "",
            "location": loc,
            "is_remote": int(
                (j.get("workplaceType") or "").lower() == "remote"
                or "remote" in loc.lower()
            ),
            "description": clean_text(body),
            "url": j.get("hostedUrl"),
            "apply_url": j.get("applyUrl") or j.get("hostedUrl"),
            "apply_channel": "lever",
            "employment_type": cats.get("commitment"),
            "posted_at": _ms_to_iso(j.get("createdAt")),
        })
    return out


def fetch_ashby(slug: str) -> list[dict]:
    url = (
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        "?includeCompensation=true"
    )
    data = requests.get(url, timeout=TIMEOUT, headers=HEADERS).json()
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        secondary = ", ".join(
            s.get("location", "") if isinstance(s, dict) else str(s)
            for s in j.get("secondaryLocations") or []
        )
        loc = ", ".join(x for x in [j.get("location"), secondary] if x)
        comp = j.get("compensation") or {}
        salary_raw = (
            comp.get("scrapeableCompensationSalarySummary")
            or comp.get("compensationTierSummary")
            or ""
        )
        out.append({
            "source": "ashby",
            "ats": "ashby",
            "ats_job_id": j.get("id"),
            "company": slug.replace("-", " ").title(),
            "title": j.get("title") or "",
            "location": loc,
            "is_remote": int(bool(j.get("isRemote"))),
            "description": clean_text(
                (j.get("descriptionPlain") or "") + "\n" + str(salary_raw)
            ),
            "url": j.get("jobUrl"),
            "apply_url": j.get("applyUrl") or j.get("jobUrl"),
            "apply_channel": "ashby",
            "employment_type": j.get("employmentType"),
            "posted_at": j.get("publishedAt"),
        })
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def load_boards() -> list[dict]:
    path = ROOT / "config" / "companies.yaml"
    if not path.exists():
        sys.exit("config/companies.yaml missing. Run scripts/discover_boards.py first.")
    return yaml.safe_load(path.read_text())["boards"]


def fetch_board(board: dict) -> tuple[str, list[dict], str | None]:
    try:
        jobs = FETCHERS[board["ats"]](board["slug"])
        return board["slug"], jobs, None
    except Exception as exc:
        return board["slug"], [], f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only probe N boards, for testing")
    ap.add_argument("--dry-run", action="store_true", help="do not write to the db")
    args = ap.parse_args()

    db.init()
    boards = load_boards()
    if args.limit:
        boards = boards[: args.limit]

    total = new = passed = review = rejected = 0
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=12) as pool:
        for slug, jobs, err in pool.map(fetch_board, boards):
            if err:
                errors.append(f"{slug}: {err}")
                continue
            for job in jobs:
                total += 1
                verdict = evaluate(job)
                status = verdict["filter_status"]
                if status == "rejected":
                    rejected += 1
                elif status == "review":
                    review += 1
                else:
                    passed += 1
                if not args.dry_run:
                    _, was_new = db.upsert_job(verdict)
                    new += int(was_new)

    print(f"boards      {len(boards)}")
    print(f"postings    {total}")
    print(f"new rows    {new}")
    print(f"passed      {passed}")
    print(f"review      {review}")
    print(f"rejected    {rejected}")
    if errors:
        print(f"\nerrors ({len(errors)}):")
        for e in errors[:10]:
            print(f"  {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
