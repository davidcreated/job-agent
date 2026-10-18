"""One command that answers "what is this thing doing right now".

Designed to be the only thing you need to run to know whether the system is
healthy, what it has done, and what it is about to do.

    .venv/bin/python scripts/status.py           # dashboard
    .venv/bin/python scripts/status.py --watch   # refresh every 30s
    .venv/bin/python scripts/status.py --applied # everything ever submitted
    .venv/bin/python scripts/status.py --next    # what would go out next
    .venv/bin/python scripts/status.py --errors  # recent failures
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db

ROOT = Path(__file__).resolve().parent.parent
PLIST = "com.example.jobagent"
PLIST_TG = "com.example.jobagent-telegram"

G, R, Y, B, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"


def paint(text: str, colour: str) -> str:
    return f"{colour}{text}{OFF}" if sys.stdout.isatty() else text


def agent_state(label: str) -> tuple[str, str]:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if line.strip().endswith(label):
                pid = line.split()[0]
                if pid.isdigit():
                    return "running", f"pid {pid}"
                return "loaded but not running", line.split()[1]
        return "not loaded", f"launchctl load ~/Library/LaunchAgents/{label}.plist"
    except Exception as exc:
        return "unknown", str(exc)[:40]


def watcher_state() -> tuple[str, str]:
    return agent_state(PLIST)


def age(stamp: str | None) -> str:
    if not stamp:
        return "never"
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
    except ValueError:
        return "?"
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172800:
        return f"{secs / 3600:.1f}h ago"
    return f"{int(secs / 86400)}d ago"


def n(conn, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()["n"]


def dashboard(conn) -> None:
    stop = (ROOT / "data" / "STOP").exists()
    shadow = db.shadow_mode()
    state, detail = watcher_state()

    print(paint("\n  JOB AGENT", B))
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # Safety first: these decide whether anything can act.
    if stop:
        line = paint("HALTED", R) + "   kill switch data/STOP is present"
    elif shadow:
        line = paint("SHADOW", Y) + "   generating only, nothing is submitted"
    else:
        line = paint("LIVE", G) + "     applications are being submitted"
    print(f"  mode      {line}")

    wcol = G if state == "running" else (Y if "loaded" in state else R)
    print(f"  watcher   {paint(state, wcol)}   {DIM}{detail}{OFF}")

    # The approval poller is a separate service. Without it, Telegram taps are
    # accepted by Telegram and never read, which looks exactly like the
    # buttons being broken.
    tstate, tdetail = agent_state(PLIST_TG)
    tcol = G if tstate == "running" else R
    print(f"  approvals {paint(tstate, tcol)}   {DIM}{tdetail}{OFF}")

    cap = 20
    try:
        import yaml
        cap = yaml.safe_load((ROOT / "config" / "criteria.yaml").read_text())[
            "limits"]["max_submissions_per_day"]
    except Exception:
        pass
    today = db.submissions_today()
    ccol = R if today >= cap else (Y if today > cap * 0.7 else G)
    print(f"  today     {paint(f'{today}/{cap}', ccol)} submitted")

    # Auto-approval removes the human gate, so it must be impossible to miss.
    try:
        import yaml
        ap = yaml.safe_load((ROOT / "config" / "criteria.yaml").read_text()).get(
            "approval", {})
        if ap.get("auto_approve"):
            scraped = " including Indeed/LinkedIn" if ap.get(
                "auto_approve_scraped") else " (scraped sources still gated)"
            print(f"  approval  {paint('AUTOMATIC', Y)}{scraped}")
        else:
            print(f"  approval  manual, waits for your tap")
    except Exception:
        pass
    print()

    # Pipeline.
    total = n(conn, "SELECT COUNT(*) n FROM jobs")
    scored = n(conn, "SELECT COUNT(*) n FROM scores")
    passed = n(conn, "SELECT COUNT(*) n FROM jobs WHERE filter_status='passed'")
    review = n(conn, "SELECT COUNT(*) n FROM jobs WHERE filter_status='review'")
    letters = n(conn, "SELECT COUNT(*) n FROM applications "
                      "WHERE cover_letter IS NOT NULL AND cover_letter != ''")
    submitted = n(conn, "SELECT COUNT(*) n FROM applications WHERE status='submitted'")
    failed = n(conn, "SELECT COUNT(*) n FROM applications WHERE status='failed'")
    unscored = passed + review - scored if (passed + review) > scored else 0

    print(paint("  PIPELINE", B))
    print(f"    ingested       {total:>6}")
    print(f"    candidates     {passed + review:>6}   {DIM}after filtering{OFF}")
    print(f"    scored         {scored:>6}" +
          (f"   {paint(f'{unscored} waiting', Y)}" if unscored > 0 else ""))
    print(f"    auto-apply     {passed:>6}   {DIM}no approval needed{OFF}")
    print(f"    your queue     {review:>6}   {DIM}awaiting Telegram approval{OFF}")
    print(f"    letters ready  {letters:>6}")
    print(f"    submitted      {paint(f'{submitted:>6}', G if submitted else DIM)}")
    if failed:
        print(f"    failed         {paint(f'{failed:>6}', R)}")

    # Freshness. This is the metric that actually decides outcomes.
    print(paint("\n  FRESHNESS", B) + f"  {DIM}earlier applications win{OFF}")
    row = conn.execute("""
        SELECT SUM(julianday('now')-julianday(posted_at) <= 1) d1,
               SUM(julianday('now')-julianday(posted_at) BETWEEN 1 AND 7) d7,
               SUM(julianday('now')-julianday(posted_at) > 7) old
        FROM jobs WHERE filter_status IN ('passed','review')
          AND posted_at IS NOT NULL""").fetchone()
    print(f"    <24h {paint(str(row['d1'] or 0), G)}    1-7d {row['d7'] or 0}"
          f"    >7d {DIM}{row['old'] or 0}{OFF}")

    last_fetch = conn.execute(
        "SELECT MAX(fetched_at) t FROM jobs").fetchone()["t"]
    print(f"    last fetch: {age(last_fetch)}")

    # Recent activity.
    events = conn.execute(
        "SELECT kind, detail, created_at FROM events "
        "ORDER BY created_at DESC LIMIT 5").fetchall()
    if events:
        print(paint("\n  RECENT", B))
        for e in events:
            print(f"    {age(e['created_at']):>9}  {e['kind'][:22]:24}"
                  f" {DIM}{(e['detail'] or '')[:40]}{OFF}")

    # Top of the queue.
    top = conn.execute("""
        SELECT j.company, j.title, s.score, j.filter_status, j.posted_at
        FROM jobs j JOIN scores s ON s.job_id = j.id
        WHERE j.filter_status IN ('passed','review')
        ORDER BY s.score DESC LIMIT 5""").fetchall()
    if top:
        print(paint("\n  TOP MATCHES", B))
        for t in top:
            tag = paint("auto", G) if t["filter_status"] == "passed" else paint("queue", Y)
            print(f"    {t['score']:>3}  {t['company'][:20]:22}"
                  f" {t['title'][:32]:34} {tag}  {DIM}{age(t['posted_at'])}{OFF}")

    print(f"\n  {DIM}status.py --next | --applied | --errors | --watch{OFF}\n")


def show_next(conn) -> None:
    print(paint("\n  NEXT OUT THE DOOR", B) +
          f"  {DIM}has a letter and clears every guard{OFF}\n")
    rows = conn.execute("""
        SELECT j.company, j.title, j.source, j.apply_channel, j.filter_status,
               a.status app_status, s.score
        FROM applications a JOIN jobs j ON j.id = a.job_id
        LEFT JOIN scores s ON s.job_id = j.id
        WHERE a.cover_letter IS NOT NULL AND a.cover_letter != ''
          AND a.status IN ('shadow','queued','approved')
        ORDER BY s.score DESC LIMIT 15""").fetchall()
    if not rows:
        print("    nothing ready. Run scripts/cover_letter.py\n")
        return

    import submit_email
    for r in rows:
        job = dict(r)
        try:
            submit_email.check_job_guards(conn, job)
            verdict = paint("READY", G)
        except submit_email.Blocked as exc:
            verdict = paint("BLOCKED", Y) + f" {DIM}{str(exc)[:52]}{OFF}"
        print(f"    {r['score'] or '-':>3}  {r['company'][:20]:22}"
              f" {r['title'][:28]:30} {verdict}")
    print()


def show_applied(conn) -> None:
    rows = conn.execute("""
        SELECT j.company, j.title, a.channel, a.submitted_at, j.url
        FROM applications a JOIN jobs j ON j.id = a.job_id
        WHERE a.status='submitted' ORDER BY a.submitted_at DESC""").fetchall()
    print(paint(f"\n  APPLIED ({len(rows)})", B) + "\n")
    if not rows:
        print("    nothing submitted yet\n")
        return
    for r in rows:
        print(f"    {age(r['submitted_at']):>9}  {r['company'][:22]:24}"
              f" {r['title'][:34]:36} {DIM}{r['channel'] or ''}{OFF}")
    print()


def show_errors(conn) -> None:
    print(paint("\n  FAILURES", B) + "\n")
    rows = conn.execute("""
        SELECT j.company, j.title, a.error, a.submitted_at
        FROM applications a JOIN jobs j ON j.id = a.job_id
        WHERE a.status='failed' ORDER BY a.id DESC LIMIT 20""").fetchall()
    if not rows:
        print("    no submission failures\n")
    for r in rows:
        print(f"    {r['company'][:20]:22} {r['title'][:26]:28}"
              f" {paint((r['error'] or '')[:44], R)}")

    log = ROOT / "data" / "watch.err"
    if log.exists() and log.stat().st_size:
        print(paint("\n  WATCHER STDERR (last 12 lines)", B))
        for line in log.read_text().splitlines()[-12:]:
            print(f"    {DIM}{line[:100]}{OFF}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="refresh every 30s")
    ap.add_argument("--next", action="store_true", dest="nxt")
    ap.add_argument("--applied", action="store_true")
    ap.add_argument("--errors", action="store_true")
    args = ap.parse_args()

    db.init()
    conn = db.connect()

    if args.nxt:
        show_next(conn)
        return 0
    if args.applied:
        show_applied(conn)
        return 0
    if args.errors:
        show_errors(conn)
        return 0

    if args.watch:
        try:
            while True:
                os.system("clear")
                dashboard(db.connect())
                time.sleep(30)
        except KeyboardInterrupt:
            return 0
    dashboard(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
