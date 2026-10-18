"""Shared normalization and filtering. Every fetcher routes through this.

The filter runs before any LLM call, so it has to be cheap and it has to be
right. A false reject costs an opportunity silently; a false pass costs a
Gemini call and, worse, a wasted application.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def criteria() -> dict:
    return yaml.safe_load((ROOT / "config" / "criteria.yaml").read_text())


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\r\f\v]+")
NL_RE = re.compile(r"\n{3,}")


def clean_text(raw: str | None) -> str:
    """Greenhouse double-escapes its HTML, so unescape twice before stripping."""
    if not raw:
        return ""
    s = html.unescape(html.unescape(raw))
    s = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", s, flags=re.I)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = WS_RE.sub(" ", s)
    return NL_RE.sub("\n\n", s).strip()


# --------------------------------------------------------------------------
# Salary
# --------------------------------------------------------------------------

CURRENCY_SIGNS = {
    "$": "USD", "£": "GBP", "€": "EUR", "₦": "NGN",
    "usd": "USD", "gbp": "GBP", "eur": "EUR", "ngn": "NGN",
    "cad": "CAD", "naira": "NGN", "us$": "USD", "c$": "CAD",
}

PERIOD_HINTS = [
    (r"per\s*hour|/\s*h(ou)?r|hourly|an hour", "hour"),
    (r"per\s*day|/\s*day|daily|a day", "day"),
    (r"per\s*week|/\s*week|weekly|a week", "week"),
    (r"per\s*month|/\s*mo(nth)?|monthly|a month|pm\b", "month"),
    (r"per\s*(year|annum)|/\s*(yr|year)|annually|yearly|p\.?a\.?\b", "year"),
]

TO_MONTH = {"hour": 160.0, "day": 21.0, "week": 4.33, "month": 1.0, "year": 1 / 12}

# Context that means the number is a business metric, not pay.
NON_SALARY_CTX = re.compile(
    r"\b(transaction|payment volume|revenue|funding|raised|valuation|"
    r"market cap|users|customers|merchants|processed|arr\b|gmv|aum|"
    r"in assets|portfolio|loans|deposits|invested|series [a-f])\b",
    re.I,
)

# A parse yielding less than this per month is a parse failure, not a cheap
# job. Treated as "no salary listed" so it is never used to reject a posting.
MIN_PLAUSIBLE_USD_MONTH = 300.0

AMOUNT_RE = re.compile(
    r"(?P<sign>US\$|C\$|[$£€₦])?\s*"
    r"(?P<num>\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)"
    r"\s*(?P<k>k\b)?",
    re.I,
)


def _to_float(num: str, k: bool) -> float:
    v = float(num.replace(",", "").replace(" ", ""))
    return v * 1000 if k else v


def parse_salary(text: str) -> dict:
    """Best-effort salary extraction. Returns empty dict when nothing is found.

    Deliberately conservative: a wrong number is worse than no number, because
    the floor filter acts on it.
    """
    if not text:
        return {}
    window = text[:6000]
    low = window.lower()

    currency = None
    for token, code in CURRENCY_SIGNS.items():
        if token in low:
            currency = code
            break
    if not currency:
        return {}

    period = None
    for pattern, name in PERIOD_HINTS:
        if re.search(pattern, low):
            period = name
            break

    # Only look at amounts near a currency marker, otherwise headcounts and
    # dates get parsed as pay.
    candidates: list[float] = []
    for m in AMOUNT_RE.finditer(window):
        ctx = window[max(0, m.start() - 12) : m.end() + 12].lower()
        if not any(t in ctx for t in CURRENCY_SIGNS):
            continue
        # "$250 billion in payment volume" is not a salary. Scale words and
        # business-metric context both disqualify the number.
        after = window[m.end() : m.end() + 28].lower()
        if re.match(r"\s*(billion|million|trillion|bn|bln|tn)\b", after):
            continue
        if NON_SALARY_CTX.search(ctx + " " + after):
            continue
        val = _to_float(m.group("num"), bool(m.group("k")))
        # Low bound is deliberately permissive so hourly contract rates
        # ("$65 per hour") survive. MIN_PLAUSIBLE_USD_MONTH does the vetting,
        # after the period is known.
        if 10 <= val <= 100_000_000:
            candidates.append(val)

    if not candidates:
        return {}

    lo, hi = min(candidates), max(candidates)

    # Infer the period when it is not stated, using magnitude.
    if period is None:
        ref = hi if currency != "NGN" else hi / 1500
        period = "year" if ref >= 20_000 else "month"

    result = {
        "salary_min": lo,
        "salary_max": hi,
        "salary_currency": currency,
        "salary_period": period,
        "salary_raw": window[max(0, low.find(next(t for t in CURRENCY_SIGNS if t in low))) :][:120],
    }

    # Distrust an implausible parse rather than acting on it. Rejecting a role
    # on a misread number is a silent, unrecoverable loss.
    usd_month = to_usd_month(result)
    if usd_month is not None and usd_month < MIN_PLAUSIBLE_USD_MONTH:
        return {}
    return result


def to_usd_month(sal: dict) -> float | None:
    if not sal.get("salary_max"):
        return None
    rates = criteria()["salary"]["usd_rates"]
    rate = rates.get(sal.get("salary_currency", "USD"))
    if rate is None:
        return None
    factor = TO_MONTH.get(sal.get("salary_period") or "year", 1 / 12)
    return sal["salary_max"] * rate * factor


def salary_tier(usd_month: float | None) -> str:
    if usd_month is None:
        return "unlisted"
    tiers = criteria()["salary"]["tiers"]
    if usd_month < tiers["below_market"]:
        return "below_market"
    if usd_month < tiers["market"]:
        return "market"
    if usd_month < tiers["strong"]:
        return "strong"
    return "top"


def meets_floor(sal: dict) -> tuple[bool, str]:
    cfg = criteria()["salary"]
    if not sal.get("salary_max"):
        return (cfg["keep_unlisted"], "salary not listed")
    floors, rates = cfg["floors"], cfg["usd_rates"]
    cur = sal.get("salary_currency", "USD")
    factor = TO_MONTH.get(sal.get("salary_period") or "year", 1 / 12)
    monthly = sal["salary_max"] * factor
    floor = floors.get(cur)
    if floor is None:
        usd = monthly * rates.get(cur, 1.0)
        floor_usd = floors["USD"]
        return (usd >= floor_usd, f"{usd:.0f} USD/mo vs floor {floor_usd}")
    return (monthly >= floor, f"{monthly:.0f} {cur}/mo vs floor {floor}")


# --------------------------------------------------------------------------
# Region and eligibility
# --------------------------------------------------------------------------

GLOBAL_REMOTE_MARKERS = [
    "work from anywhere", "anywhere in the world", "fully remote, worldwide",
    "remote - worldwide", "remote (worldwide)", "globally distributed",
    "work from any", "location independent", "remote anywhere",
    "hire anywhere", "any time zone", "any timezone", "worldwide remote",
    "remote, global", "global remote", "remote-first, worldwide",
]

EOR_MARKERS = [
    "employer of record", "via deel", "through deel", "remote.com",
    "b2b contract", "independent contractor", "contractor agreement",
    "we hire globally", "hire through an eor", "oyster hr", "global payroll",
]

SPONSOR_MARKERS = [
    "visa sponsorship", "we sponsor", "sponsorship available",
    "relocation package", "relocation assistance", "will sponsor",
    "happy to sponsor", "relocation support",
]

def _profile() -> dict:
    path = ROOT / "config" / "profile.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _home_markers() -> list[str]:
    """Words meaning "this role is in my region": the city and country from the
    profile, plus any extra markers listed in criteria.yaml."""
    ident = _profile().get("identity", {})
    own = [ident.get("city"), ident.get("country")]
    extra = criteria()["eligibility"].get("home_markers") or []
    return [m.lower() for m in [*own, *extra] if m]


# Which profile work_authorization key unlocks which blocking-phrase group.
AUTH_KEY = {"us_only": "US", "uk_only": "GB", "eu_only": "EU"}


def _authorized(group: str) -> bool:
    key = AUTH_KEY.get(group)
    return bool(key) and _profile().get("work_authorization", {}).get(key) == "authorized"


def _scan_region(text: str) -> str | None:
    crit = criteria()["regions"]
    t = text.lower()
    for bad in crit["deny"]:
        # Word-boundary match so "indiana" does not trigger "india".
        if re.search(rf"\b{re.escape(bad)}\b", t):
            return bad
    for good in crit["allow"]:
        if re.search(rf"\b{re.escape(good)}\b", t):
            return good
    return None


def detect_region(location: str, description: str) -> str:
    """Where the job is, not every country the description happens to name.

    The location field is authoritative. Scanning the description for the
    denylist rejected a Lithuania-based Flutter role at Tide because the
    company blurb mentioned its India engineering hub. Only fall through to
    the description when the location field says nothing useful, and then
    only the opening lines, where a real location is stated.
    """
    if location and (hit := _scan_region(location)):
        return hit
    # With no allowlist there is nothing to recognise a location against, but
    # a stated location still outranks the description. Without this, a
    # Lithuania role fell through to the blurb and was denied for India.
    if location.strip() and not criteria()["regions"]["allow"]:
        return location.strip().lower()
    if (hit := _scan_region(description[:1200])):
        return hit
    return "unknown"


def region_allowed(region: str) -> bool:
    crit = criteria()["regions"]
    if region in crit["deny"]:
        return False
    return not crit["allow"] or region in crit["allow"] or region == "unknown"


def detect_eligibility(location: str, description: str, is_remote: bool) -> str:
    hay = f"{location} {description[:8000]}".lower()

    for group, phrases in criteria()["eligibility"]["blocking_phrases"].items():
        if _authorized(group):
            continue
        if any(phrase in hay for phrase in phrases):
            return group

    if any(m in hay for m in _home_markers()):
        return "home_region"
    if any(m in hay for m in GLOBAL_REMOTE_MARKERS):
        return "global_remote"
    if any(m in hay for m in EOR_MARKERS):
        return "eor"
    if any(m in hay for m in SPONSOR_MARKERS):
        return "sponsor"
    if is_remote:
        return "unknown"
    return "unknown"


# --------------------------------------------------------------------------
# Title and content
# --------------------------------------------------------------------------

def title_verdict(title: str) -> tuple[bool, str]:
    t = (title or "").lower()
    crit = criteria()["titles"]
    for bad in crit["exclude"]:
        if bad in t:
            return False, f"title excluded: {bad}"
    for good in crit["include"]:
        if good in t:
            return True, f"title matched: {good}"
    return False, "title not a target role"


def _has_term(term: str, text: str) -> bool:
    """Word-boundary match. Plain substring matching found 'unity' inside
    'opportunity' and 'go' inside 'going', which disqualified real roles."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def stack_hits(text: str) -> dict:
    t = text.lower()
    crit = criteria()["stack"]
    return {
        "core": [s for s in crit["core"] if _has_term(s, t)],
        "secondary": [s for s in crit["secondary"] if _has_term(s, t)],
        "disqualifying": [s for s in crit["disqualifying"] if _has_term(s, t)],
    }


def years_required(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*\+?\s*years?(?:\s+of)?\s+(?:.{0,24})?experience", text.lower())
    return int(m.group(1)) if m else None


def dealbreaker(text: str) -> str | None:
    t = text.lower()
    for d in criteria()["dealbreakers"]:
        if d in t:
            return d
    return None


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

def evaluate(job: dict) -> dict:
    """Apply every cheap filter. Mutates and returns the job dict.

    Sets filter_status to 'passed', 'rejected' or 'review', plus a reason.
    Only 'passed' and 'review' rows ever reach the LLM.
    """
    title = job.get("title") or ""
    desc = job.get("description") or ""
    location = job.get("location") or ""
    blob = f"{title}\n{desc}"

    ok, reason = title_verdict(title)
    if not ok:
        return {**job, "filter_status": "rejected", "filter_reason": reason}

    if (db := dealbreaker(blob)):
        return {**job, "filter_status": "rejected", "filter_reason": f"dealbreaker: {db}"}

    hits = stack_hits(blob)
    if hits["disqualifying"] and not hits["core"]:
        return {
            **job, "filter_status": "rejected",
            "filter_reason": f"wrong stack: {hits['disqualifying'][0]}",
        }

    yrs = years_required(blob)
    if yrs and yrs > criteria()["seniority"]["max_years_required"]:
        return {**job, "filter_status": "rejected", "filter_reason": f"requires {yrs}y experience"}

    region = detect_region(location, desc)
    if not region_allowed(region):
        return {**job, "region": region, "filter_status": "rejected",
                "filter_reason": f"region denied: {region}"}

    elig = detect_eligibility(location, desc, bool(job.get("is_remote")))
    elig_cfg = criteria()["eligibility"]
    if elig in elig_cfg["reject"]:
        return {**job, "region": region, "eligibility": elig,
                "filter_status": "rejected", "filter_reason": f"not eligible: {elig}"}

    sal = parse_salary(blob)
    passes_floor, floor_note = meets_floor(sal)
    if not passes_floor:
        return {**job, **sal, "region": region, "eligibility": elig,
                "filter_status": "rejected", "filter_reason": f"below floor: {floor_note}"}

    usd_mo = to_usd_month(sal)
    status = "review" if elig in elig_cfg["queue_for_review"] else "passed"

    return {
        **job, **sal,
        "region": region,
        "eligibility": elig,
        "salary_usd_mo": usd_mo,
        "salary_tier": salary_tier(usd_mo),
        "filter_status": status,
        "filter_reason": (
            f"{reason}; core={','.join(hits['core']) or 'none'}; "
            f"elig={elig}; {floor_note}"
        ),
    }
