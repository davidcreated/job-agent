"""Pull postings from public remote-job feeds.

Sources: RemoteOK, Remotive, WeWorkRemotely, Hacker News "Who is hiring".
All are public JSON or RSS. No scraping, no terms problem, no cost.

Field mappings were captured from live responses.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
from normalize import clean_text, evaluate

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon) job-agent/1.0"}
TIMEOUT = 30


def _iso(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError):
        return None


def fetch_remoteok() -> list[dict]:
    data = requests.get(
        "https://remoteok.com/api", timeout=TIMEOUT, headers=HEADERS
    ).json()
    out = []
    for j in data:
        # The first element is a legal notice, not a posting.
        if not isinstance(j, dict) or not j.get("position"):
            continue
        salary_note = ""
        if j.get("salary_min"):
            salary_note = f"\nSalary: ${j.get('salary_min')} - ${j.get('salary_max')} per year"
        out.append({
            "source": "remoteok",
            "ats": "remoteok",
            "ats_job_id": str(j.get("id")),
            "company": j.get("company") or "",
            "title": j.get("position") or "",
            "location": j.get("location") or "Remote",
            "is_remote": 1,
            "description": clean_text(j.get("description")) + salary_note
            + "\nTags: " + ", ".join(j.get("tags") or []),
            "url": j.get("url"),
            "apply_url": j.get("apply_url") or j.get("url"),
            "apply_channel": "form",
            "posted_at": j.get("date") or _iso(j.get("epoch")),
        })
    return out


def fetch_remotive() -> list[dict]:
    out = []
    for category in ("software-dev", "all-others"):
        url = f"https://remotive.com/api/remote-jobs?category={category}"
        try:
            data = requests.get(url, timeout=TIMEOUT, headers=HEADERS).json()
        except Exception:
            continue
        for j in data.get("jobs", []):
            out.append({
                "source": "remotive",
                "ats": "remotive",
                "ats_job_id": str(j.get("id")),
                "company": j.get("company_name") or "",
                "title": j.get("title") or "",
                "location": j.get("candidate_required_location") or "Remote",
                "is_remote": 1,
                "description": clean_text(j.get("description"))
                + f"\nSalary: {j.get('salary') or 'not listed'}"
                + "\nTags: " + ", ".join(j.get("tags") or []),
                "url": j.get("url"),
                "apply_url": j.get("url"),
                "apply_channel": "form",
                "employment_type": j.get("job_type"),
                "posted_at": j.get("publication_date"),
            })
    return out


WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
]


def fetch_wwr() -> list[dict]:
    out = []
    for feed_url in WWR_FEEDS:
        feed = feedparser.parse(feed_url)
        for e in feed.entries:
            # Titles arrive as "Company: Role".
            raw = e.get("title", "")
            company, _, title = raw.partition(": ")
            if not title:
                company, title = "", raw
            out.append({
                "source": "weworkremotely",
                "ats": "wwr",
                "ats_job_id": e.get("id") or e.get("link"),
                "company": company.strip(),
                "title": title.strip(),
                "location": e.get("region") or "Remote",
                "is_remote": 1,
                "description": clean_text(e.get("summary")),
                "url": e.get("link"),
                "apply_url": e.get("link"),
                "apply_channel": "form",
                "posted_at": e.get("published"),
            })
    return out


# HN "Who is hiring" comments follow a loose convention:
#   Company | Role | Location | Remote | salary | url
HN_SEARCH = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?tags=story,author_whoishiring&hitsPerPage=5"
)


def fetch_hn() -> list[dict]:
    try:
        hits = requests.get(HN_SEARCH, timeout=TIMEOUT, headers=HEADERS).json()["hits"]
    except Exception:
        return []
    story = next(
        (h for h in hits if "who is hiring" in (h.get("title") or "").lower()), None
    )
    if not story:
        return []

    item = requests.get(
        f"https://hn.algolia.com/api/v1/items/{story['objectID']}",
        timeout=TIMEOUT, headers=HEADERS,
    ).json()

    out = []
    for child in item.get("children") or []:
        text = clean_text(child.get("text"))
        if not text or len(text) < 40:
            continue
        head = text.split("\n")[0]
        parts = [p.strip() for p in head.split("|")]
        company = parts[0][:80] if parts else ""
        title = parts[1][:120] if len(parts) > 1 else head[:120]
        url_match = re.search(r"https?://\S+", text)
        out.append({
            "source": "hackernews",
            "ats": "hn",
            "ats_job_id": str(child.get("id")),
            "company": company,
            "title": title,
            "location": " ".join(parts[2:4])[:120] if len(parts) > 2 else "",
            "is_remote": int("remote" in text.lower()),
            "description": text,
            "url": f"https://news.ycombinator.com/item?id={child.get('id')}",
            "apply_url": url_match.group(0) if url_match else None,
            "apply_channel": "manual",
            "posted_at": child.get("created_at"),
        })
    return out


SOURCES = {
    "remoteok": fetch_remoteok,
    "remotive": fetch_remotive,
    "weworkremotely": fetch_wwr,
    "hackernews": fetch_hn,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES), help="run only one source")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init()
    names = [args.source] if args.source else list(SOURCES)
    grand = {"total": 0, "new": 0, "passed": 0, "review": 0, "rejected": 0}

    for name in names:
        try:
            jobs = SOURCES[name]()
        except Exception as exc:
            print(f"{name:16} ERROR {type(exc).__name__}: {exc}")
            continue
        counts = {"total": 0, "new": 0, "passed": 0, "review": 0, "rejected": 0}
        for job in jobs:
            counts["total"] += 1
            verdict = evaluate(job)
            counts[verdict["filter_status"]] += 1
            if not args.dry_run:
                _, was_new = db.upsert_job(verdict)
                counts["new"] += int(was_new)
        print(
            f"{name:16} {counts['total']:>5} postings  "
            f"{counts['new']:>4} new  {counts['passed']:>3} passed  "
            f"{counts['review']:>3} review  {counts['rejected']:>5} rejected"
        )
        for k in grand:
            grand[k] += counts[k]

    print(
        f"\n{'TOTAL':16} {grand['total']:>5} postings  {grand['new']:>4} new  "
        f"{grand['passed']:>3} passed  {grand['review']:>3} review  "
        f"{grand['rejected']:>5} rejected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
