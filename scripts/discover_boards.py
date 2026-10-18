"""Probe candidate companies against the public ATS job board APIs.

We do not know which ATS a given company uses, and guessing produces a config
full of dead endpoints. This tries every candidate against every ATS, keeps
what actually responds with jobs, and writes config/companies.yaml.

Run again whenever you add candidates:
    .venv/bin/python scripts/discover_boards.py
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "config" / "companies.yaml"
TIMEOUT = 12

# Slugs to try. A name appearing here is a guess; the probe decides what is
# real. Add freely, dead entries cost one request each and are discarded.
CANDIDATES = [
    # Large product companies with large engineering orgs
    "stripe", "airbnb", "robinhood", "coinbase", "doordash", "instacart",
    "discord", "figma", "notion", "databricks", "reddit", "duolingo",
    "gitlab", "cloudflare", "twilio", "affirm", "brex", "chime", "plaid",
    "samsara", "flexport", "scaleai", "openai", "anthropic", "ramp",
    "shopify", "spotify", "pinterest", "snap", "lyft", "strava", "sofi",
    "nerdwallet", "betterment", "wealthfront", "dropbox", "asana", "airtable",
    "grammarly", "calendly", "zapier", "retool", "vercel", "supabase",
    "sentry", "linear", "replit", "posthog", "clerk", "resend", "modal",
    "elevenlabs", "runway", "perplexityai", "perplexity", "cursor",
    "anysphere", "mintlify", "browserbase", "together", "huggingface",
    # Remote-first and EOR companies, strongest fit for a remote hire outside the US/EU
    "deel", "remotecom", "remote", "oyster", "oysterhr", "andela", "turing",
    "toptal", "gun", "crossover", "automattic", "zapier", "doist",
    "buffer", "hotjar", "aha", "close", "hopin", "gitpod", "aiven",
    # Fintech and neobanks, heavy engineering investment
    "wise", "monzo", "starling", "revolut", "n26", "klarna", "curve",
    "zilch", "moneybox", "freetrade", "marshmallow", "zopa", "tide",
    "mercury", "current", "dave", "varo", "step", "greenlight",
    # African and Nigeria-based (edit this list for your own region)
    "flutterwave", "paystack", "moniepoint", "kuda", "piggyvest", "cowrywise",
    "interswitch", "chipper", "chippercash", "yellowcard", "mono", "okra",
    "reliancehmo", "54collective", "helium", "termii", "seamlesshr",
    "eden", "bamboo", "risevest", "graph", "thepeer", "nomba", "opay",
    # Health, mobility, commerce
    "whoop", "oura", "calm", "headspace", "noom", "ro", "carbonhealth",
    "babylonhealth", "kry", "livi", "getir", "bolt", "wolt", "deliveroo",
    "gousto", "depop", "vinted", "etsy", "faire", "gorillas", "glovo",
    "cabify", "freenow", "tier", "voi", "lime", "bird",
    # Dev tools and infra that ship SDKs
    "algolia", "contentful", "storyblok", "prismic", "builderio",
    "launchdarkly", "amplitude", "mixpanel", "segment", "braze", "iterable",
    "onesignal", "customerio", "intercom", "front", "pylon",
]

ATS = {
    "greenhouse": {
        "url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "count": lambda d: len(d.get("jobs", [])),
    },
    "lever": {
        "url": "https://api.lever.co/v0/postings/{slug}?mode=json",
        "count": lambda d: len(d) if isinstance(d, list) else 0,
    },
    "ashby": {
        "url": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "count": lambda d: len(d.get("jobs", [])),
    },
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon) job-agent/1.0"}


def probe(slug: str, ats: str) -> dict | None:
    spec = ATS[ats]
    try:
        r = requests.get(
            spec["url"].format(slug=slug), timeout=TIMEOUT, headers=HEADERS
        )
        if r.status_code != 200:
            return None
        n = spec["count"](r.json())
        if n <= 0:
            return None
        return {"slug": slug, "ats": ats, "open_roles": n}
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return None


def main() -> int:
    tasks = [(s, a) for s in dict.fromkeys(CANDIDATES) for a in ATS]
    print(f"probing {len(tasks)} slug/ATS combinations...", file=sys.stderr)

    hits: list[dict] = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        for result in pool.map(lambda t: probe(*t), tasks):
            if result:
                hits.append(result)
                print(
                    f"  {result['ats']:11} {result['slug']:20} "
                    f"{result['open_roles']:>4} roles",
                    file=sys.stderr,
                )

    # A slug can legitimately resolve on two ATS providers. Keep the one
    # advertising more roles, which is reliably the live board.
    best: dict[str, dict] = {}
    for h in hits:
        cur = best.get(h["slug"])
        if cur is None or h["open_roles"] > cur["open_roles"]:
            best[h["slug"]] = h

    boards = sorted(best.values(), key=lambda h: (h["ats"], h["slug"]))
    doc = {
        "_note": (
            "Generated by scripts/discover_boards.py. Every entry was verified "
            "live at generation time. Re-run to refresh."
        ),
        "boards": boards,
    }
    OUT.write_text(yaml.safe_dump(doc, sort_keys=False, width=100))

    by_ats: dict[str, int] = {}
    for b in boards:
        by_ats[b["ats"]] = by_ats.get(b["ats"], 0) + 1
    total = sum(b["open_roles"] for b in boards)
    print(
        f"\n{len(boards)} live boards ({by_ats}), {total} open roles total"
        f"\nwrote {OUT}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
