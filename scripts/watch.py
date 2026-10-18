"""Fast lane: detect newly posted roles and act on them within minutes.

Applying early matters more than almost anything in the letter. Recruiters
screen in arrival order and often stop reading once they have a shortlist, so
a good application on day 8 loses to an average one on day 0.

The backlog this system first ingested was mostly stale: 50 of 73 dated
candidates were over 30 days old. Those are effectively lost. The value is in
the next posting, not the last thousand.

Design:
  - Tiered polling. High-value boards are checked often, the long tail rarely,
    because a full sweep of 97 boards takes minutes and most of them rarely
    change.
  - Only genuinely new postings are processed. Everything else is already in
    the database and was handled on a previous pass.
  - Fresh postings preempt. A role posted an hour ago jumps ahead of a
    backlog item scoring higher, because the backlog item is already lost.
  - Daily submission headroom is reserved for fresh roles rather than being
    spent on the backlog.

Run:
    .venv/bin/python scripts/watch.py --once      # one cycle
    .venv/bin/python scripts/watch.py             # loop forever
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
LAST_FULL_KEY = "watch_last_full_sweep"

# A posting younger than this is worth preempting the queue for.
FRESH_HOURS = 12
# Never spend the whole daily cap on backlog; keep room for tomorrow's fresh
# postings, which are worth more than anything already sitting in the queue.
RESERVE_FOR_FRESH = 8


def log(msg: str) -> None:
    """Timestamped and flushed. launchd redirects stdout to a file, which makes
    Python block-buffer it, so an unflushed watcher looks completely dead: the
    log stayed empty for two hours while the process was alive and working."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run(script: str, *args: str, timeout: int = 420) -> str:
    """Run a stage with a bounded timeout.

    Timeouts are deliberately short. A stage starved of LLM quota can block for
    an unbounded time, and when it does the fetch loop stops with it. Ingestion
    is the part that must keep running: a missed score can be caught up later,
    a missed posting cannot."""
    cmd = [PY, str(ROOT / "scripts" / script), *args]
    started = time.monotonic()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        log(f"  {script} finished in {time.monotonic() - started:.0f}s")
        return (out.stdout or "") + (out.stderr or "")
    except subprocess.TimeoutExpired:
        log(f"  {script} TIMED OUT after {timeout}s, continuing")
        return f"{script}: timed out after {timeout}s"


def auto_approve_enabled() -> bool:
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from normalize import criteria
        return bool(criteria().get("approval", {}).get("auto_approve"))
    except Exception:
        return False


def new_since(marker: str) -> list[dict]:
    """Postings ingested after the marker that are worth acting on."""
    conn = db.connect()
    rows = conn.execute(
        """SELECT id, company, title, posted_at, fetched_at, filter_status
           FROM jobs
           WHERE fetched_at > ? AND filter_status IN ('passed','review')
           ORDER BY COALESCE(posted_at, fetched_at) DESC""",
        (marker,),
    ).fetchall()
    return [dict(r) for r in rows]


def age_hours(row: dict) -> float:
    stamp = row.get("posted_at") or row.get("fetched_at")
    if not stamp:
        return 9e9
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except ValueError:
        return 9e9


def cycle(full: bool, verbose: bool = True) -> dict:
    started = db.now()
    stats = {"new": 0, "fresh": 0, "scored": 0, "letters": 0}

    log(f"cycle start ({'full sweep' if full else 'fast poll'})")

    # Read button presses FIRST. The watcher previously only ever sent to
    # Telegram and never polled, so twelve approvals sat unread on Telegram's
    # servers and nothing the user tapped had any effect. Polling before
    # submission also means an approval acts in the cycle it arrives in,
    # rather than waiting for the next one.
    run("notify.py", "poll", "--seconds", "20", timeout=90)

    # Ingest. Everything downstream of this is catch-up work.
    run("fetch_ats.py", timeout=600)
    run("fetch_feeds.py", timeout=240)
    if full:
        db.set_setting(LAST_FULL_KEY, started)

    found = new_since(started)
    stats["new"] = len(found)
    fresh = [j for j in found if age_hours(j) <= FRESH_HOURS]
    stats["fresh"] = len(fresh)

    log(f"  new candidates: {len(found)} (fresh <{FRESH_HOURS}h: {len(fresh)})")
    for j in fresh[:8]:
        log(f"    {age_hours(j):5.1f}h  {j['company'][:22]:24} {j['title'][:40]}")

    if not found:
        # Even with nothing new, approvals may be waiting and approved work
        # may be ready to submit. Returning early here meant a quiet cycle
        # silently skipped both.
        if auto_approve_enabled():
            run("notify.py", "approve-all", "--auto", timeout=120)
        if not db.shadow_mode():
            run("submit_email.py", "--limit", "3", timeout=300)
            run("submit_form.py", "--limit", "3", "--submit", timeout=600)
        log(f"cycle done (nothing new). submitted today: {db.submissions_today()}")
        return stats

    # Score only what is new. Scoring the whole table every cycle would burn
    # the daily LLM quota within a couple of hours.
    out = run("score.py", "--batch", "5", timeout=420)
    stats["scored"] = out.count("scored")

    # Letters for anything that cleared the bar, freshest first.
    out = run("cover_letter.py", "--limit", "4", timeout=420)
    stats["letters"] = out.count("[")

    # Queue for approval, then let the submitters pick up what is allowed.
    run("notify.py", "send", "--limit", "8", timeout=120)
    run("notify.py", "poll", "--seconds", "20", timeout=90)

    # Auto-approval, if enabled. The queue still receives every message so
    # there is a record and Skip still works, but nothing waits on a tap.
    if auto_approve_enabled():
        run("notify.py", "approve-all", "--auto", timeout=120)

    remaining = db.submissions_today()
    cap_note = ""
    if remaining >= RESERVE_FOR_FRESH and not fresh:
        cap_note = " (holding remaining cap for fresh postings)"
    elif not db.shadow_mode():
        run("submit_email.py", "--limit", "3", timeout=300)
        run("submit_form.py", "--limit", "3", "--submit", timeout=600)

    log(f"cycle done. submitted today: {db.submissions_today()}{cap_note}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single cycle")
    ap.add_argument("--interval", type=int, default=900,
                    help="seconds between fast polls (default 900 = 15 min)")
    ap.add_argument("--full-every", type=int, default=6,
                    help="do a full sweep every N cycles")
    args = ap.parse_args()

    db.init()
    if args.once:
        cycle(full=True)
        return 0

    n = 0
    log(f"watching. poll every {args.interval}s, full sweep every "
        f"{args.full_every} cycles.")
    while True:
        try:
            cycle(full=(n % args.full_every == 0))
        except KeyboardInterrupt:
            print("\nstopped")
            return 0
        except Exception as exc:
            log(f"  cycle error: {type(exc).__name__}: {exc}")
        n += 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
