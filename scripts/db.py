"""SQLite schema and access helpers. Every script goes through here."""

import hashlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "applications.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    fingerprint     TEXT UNIQUE NOT NULL,
    source          TEXT NOT NULL,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT,
    is_remote       INTEGER,
    description     TEXT,
    url             TEXT,
    apply_url       TEXT,
    apply_channel   TEXT,
    apply_email     TEXT,
    ats             TEXT,
    ats_job_id      TEXT,
    salary_raw      TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    salary_period   TEXT,
    salary_usd_mo   REAL,
    salary_tier     TEXT,
    employment_type TEXT,
    posted_at       TEXT,
    fetched_at      TEXT NOT NULL,
    region          TEXT,
    eligibility     TEXT,
    filter_status   TEXT NOT NULL DEFAULT 'pending',
    filter_reason   TEXT,
    raw             TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(filter_status);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched ON jobs(fetched_at);

CREATE TABLE IF NOT EXISTS scores (
    job_id    INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    score     INTEGER NOT NULL,
    rationale TEXT,
    strengths TEXT,
    gaps      TEXT,
    model     TEXT,
    scored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,
    channel       TEXT,
    cover_letter  TEXT,
    cv_variant    TEXT,
    queued_at     TEXT,
    submitted_at  TEXT,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

-- Single-row table holding runtime state such as shadow mode.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "shadow_mode": "1",
    "shadow_mode_note": "Generates everything, submits nothing. Set to 0 to go live.",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # journal_mode is deliberately NOT set here. Switching to WAL needs a
    # brief exclusive lock, so running it on every connect fails with
    # "database is locked" whenever another connection is already open.
    # It is persistent per database file, so init() sets it once.
    return conn


@contextmanager
def txn():
    """Transaction that actually closes the connection afterwards.

    `with sqlite3.connect(...) as conn` commits but does NOT close. Using it
    directly leaks one open connection per call, which eventually deadlocks
    writers against each other.
    """
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    conn = connect()
    try:
        # Persistent per database file, so this runs once rather than on
        # every connect. Harmless if already WAL.
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()
    with txn() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


def fingerprint(company: str, title: str, apply_url: str | None) -> str:
    """Stable identity for a posting. Dedupe depends entirely on this.

    Normalized so the same role arriving from two sources collapses to one row.
    """
    parts = [
        (company or "").strip().lower(),
        (title or "").strip().lower(),
        (apply_url or "").strip().lower().split("?")[0].rstrip("/"),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def get_setting(key: str, default: str | None = None) -> str | None:
    with txn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with txn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def shadow_mode() -> bool:
    return get_setting("shadow_mode", "1") == "1"


def log_event(job_id: int | None, kind: str, detail: str = "") -> None:
    with txn() as conn:
        conn.execute(
            "INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
            (job_id, kind, detail, now()),
        )


def upsert_job(job: dict) -> tuple[int | None, bool]:
    """Insert a normalized posting. Returns (job_id, was_new).

    Existing fingerprints are left untouched so a re-fetch never resets
    filter status or triggers a second application.
    """
    fp = fingerprint(job.get("company"), job.get("title"), job.get("apply_url"))
    columns = [
        "fingerprint", "source", "company", "title", "location", "is_remote",
        "description", "url", "apply_url", "apply_channel", "apply_email",
        "ats", "ats_job_id", "salary_raw", "salary_min", "salary_max",
        "salary_currency", "salary_period", "salary_usd_mo", "salary_tier",
        "employment_type", "posted_at", "fetched_at", "region", "eligibility",
        "filter_status", "filter_reason", "raw",
    ]
    values = {c: job.get(c) for c in columns}
    values["fingerprint"] = fp
    values["fetched_at"] = job.get("fetched_at") or now()
    values["filter_status"] = job.get("filter_status") or "pending"
    if isinstance(values.get("raw"), (dict, list)):
        values["raw"] = json.dumps(values["raw"])

    with txn() as conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE fingerprint = ?", (fp,)
        ).fetchone()
        if existing:
            return existing["id"], False
        cur = conn.execute(
            f"INSERT INTO jobs ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})",
            [values[c] for c in columns],
        )
        return cur.lastrowid, True


def already_applied(company: str, title: str) -> bool:
    """Has an application actually been SENT for this company and title?

    Only 'submitted' counts. 'queued' means a cover letter exists and nothing
    more, and 'approved' means the user tapped approve but it has not gone out
    yet. Counting those made a job block its own submission: the letter writer
    set status to 'queued', and the duplicate check then read that back as
    proof it had already applied.

    Matching on company and title rather than on the job row is deliberate, so
    the same role reached through a second URL is still caught.
    """
    with txn() as conn:
        row = conn.execute(
            """SELECT 1 FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE lower(j.company) = lower(?) AND lower(j.title) = lower(?)
                 AND a.status = 'submitted'
               LIMIT 1""",
            (company, title),
        ).fetchone()
    return row is not None


def submissions_today() -> int:
    with txn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM applications "
            "WHERE status = 'submitted' AND date(submitted_at) = date('now')"
        ).fetchone()
    return row["n"]


def stats() -> dict:
    with txn() as conn:
        out = {
            "jobs": conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"],
            "passed": conn.execute(
                "SELECT COUNT(*) n FROM jobs WHERE filter_status='passed'"
            ).fetchone()["n"],
            "rejected": conn.execute(
                "SELECT COUNT(*) n FROM jobs WHERE filter_status='rejected'"
            ).fetchone()["n"],
            "pending": conn.execute(
                "SELECT COUNT(*) n FROM jobs WHERE filter_status='pending'"
            ).fetchone()["n"],
            "scored": conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"],
            "submitted_today": 0,
        }
    out["submitted_today"] = submissions_today()
    return out


if __name__ == "__main__":
    init()
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        for k, v in stats().items():
            print(f"{k:18} {v}")
    else:
        print(f"initialized {DB_PATH}")
        print(f"shadow_mode = {shadow_mode()}")
