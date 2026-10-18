"""Telegram approval queue and daily digest.

Anything the pipeline will not auto-submit lands here: scraped sources, visa
sponsorship roles, and anything scored between the review and apply
thresholds. Each arrives as a message with Approve and Skip buttons.

Approving does not send anything by itself. It marks the application approved,
and the submission script picks it up on its next run, where every guard runs
again. Nothing here bypasses shadow mode.

Commands:
  send    push queued items to Telegram
  poll    process button presses (run on a loop or via cron)
  digest  daily summary
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import llm  # loads .env

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_KEY = "telegram_update_offset"


class NotConfigured(RuntimeError):
    pass


def creds() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        raise NotConfigured(
            "TELEGRAM_BOT_TOKEN is not set in .env\n"
            "  1. Message @BotFather, send /newbot, follow the prompts\n"
            "  2. Paste the token into .env\n"
            "  3. Message your bot, then run scripts/telegram_setup.py"
        )
    if not chat:
        raise NotConfigured(
            "TELEGRAM_CHAT_ID is not set. Run scripts/telegram_setup.py"
        )
    return token, chat


def call(method: str, payload: dict | None = None, token: str | None = None) -> dict:
    token = token or creds()[0]
    r = requests.post(API.format(token=token, method=method),
                      json=payload or {}, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {data.get('description')}")
    return data.get("result")


def escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_job(job: dict) -> str:
    score = job.get("score")
    bits = [
        f"<b>{escape(job['title'])}</b>",
        f"{escape(job['company'])}",
    ]
    meta = []
    if score is not None:
        meta.append(f"score {score}")
    if job.get("eligibility"):
        meta.append(job["eligibility"])
    if job.get("salary_tier") and job["salary_tier"] != "unlisted":
        meta.append(job["salary_tier"])
    if job.get("location"):
        meta.append(escape(job["location"][:40]))
    if meta:
        bits.append(" | ".join(meta))
    if job.get("rationale"):
        bits.append(f"\n<i>{escape(job['rationale'][:280])}</i>")
    if job.get("gaps"):
        bits.append(f"\n<b>Gaps:</b> {escape(job['gaps'][:200])}")
    if job.get("url"):
        bits.append(f"\n{escape(job['url'])}")
    if job.get("cover_letter"):
        bits.append(f"\n<b>Letter ready</b> ({len(job['cover_letter'].split())} words)")
    return "\n".join(bits)


def cmd_send(args) -> int:
    token, chat = creds()
    conn = db.connect()
    rows = conn.execute(
        """SELECT j.*, s.score, s.rationale, s.gaps, a.cover_letter
           FROM jobs j
           LEFT JOIN scores s ON s.job_id = j.id
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE j.filter_status = 'review'
             AND (a.status IS NULL OR a.status IN ('shadow','queued'))
             AND NOT EXISTS (SELECT 1 FROM events e
                             WHERE e.job_id = j.id AND e.kind = 'queued_telegram')
           ORDER BY s.score DESC NULLS LAST
           LIMIT ?""",
        (args.limit,),
    ).fetchall()

    if not rows:
        print("nothing new to queue")
        return 0

    sent = 0
    for row in rows:
        job = dict(row)
        try:
            call("sendMessage", {
                "chat_id": chat,
                "text": format_job(job),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": [[
                    {"text": "Approve", "callback_data": f"a:{job['id']}"},
                    {"text": "Skip", "callback_data": f"r:{job['id']}"},
                ]]},
            }, token)
            db.log_event(job["id"], "queued_telegram", job["title"])
            sent += 1
            time.sleep(0.4)  # Telegram tolerates roughly 30 messages/second
        except Exception as exc:
            print(f"  failed for job {job['id']}: {exc}")

    print(f"queued {sent} jobs to Telegram")
    return 0


def cmd_poll(args) -> int:
    token, chat = creds()
    offset = int(db.get_setting(OFFSET_KEY, "0") or 0)
    forever = getattr(args, "forever", False)
    deadline = time.time() + args.seconds
    handled = 0
    if forever:
        print("polling forever. ctrl-c to stop.", flush=True)

    while forever or time.time() < deadline:
        try:
            updates = call("getUpdates", {
                "offset": offset, "timeout": 20,
                "allowed_updates": ["callback_query"],
            }, token)
        except Exception as exc:
            print(f"poll error: {exc}")
            time.sleep(5)
            continue

        for u in updates or []:
            offset = max(offset, u["update_id"] + 1)
            cq = u.get("callback_query")
            if not cq:
                continue
            data = cq.get("data") or ""
            action, _, raw_id = data.partition(":")
            if action not in ("a", "r") or not raw_id.isdigit():
                continue
            job_id = int(raw_id)

            status = "approved" if action == "a" else "rejected_by_user"
            with db.txn() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM applications WHERE job_id=?", (job_id,)
                ).fetchone()
                if existing:
                    conn.execute(
                        # A tap must not undo a completed send. Tapping
                        # approve or skip on a job already submitted would
                        # otherwise erase the record and let it go out twice.
                        "UPDATE applications SET status=? WHERE job_id=? "
                        "AND status != 'submitted'",
                        (status, job_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO applications (job_id, status, queued_at) "
                        "VALUES (?,?,?)", (job_id, status, db.now()),
                    )
                if action == "r":
                    conn.execute(
                        "UPDATE jobs SET filter_status='rejected', "
                        "filter_reason='skipped by user' WHERE id=?", (job_id,),
                    )
            db.log_event(job_id, f"telegram_{status}", "")

            label = "Approved" if action == "a" else "Skipped"
            try:
                call("answerCallbackQuery",
                     {"callback_query_id": cq["id"], "text": label}, token)
                msg = cq.get("message") or {}
                if msg.get("message_id"):
                    call("editMessageReplyMarkup", {
                        "chat_id": chat, "message_id": msg["message_id"],
                        "reply_markup": {"inline_keyboard": [[
                            {"text": f"{label}", "callback_data": "noop"}]]},
                    }, token)
            except Exception:
                pass
            handled += 1
            print(f"  job {job_id}: {status}", flush=True)

        db.set_setting(OFFSET_KEY, str(offset))

    print(f"handled {handled} responses")
    return 0


def cmd_approve_all(args) -> int:
    """Approve every job that was sent to Telegram and not yet acted on.

    Deliberately a separate command rather than a default. Approving is what
    lets a job be submitted, so doing it in bulk should be an explicit act.
    Jobs already submitted are never touched.
    """
    conn = db.connect()
    from normalize import criteria
    cfg = criteria().get("approval", {})
    allow_scraped = bool(cfg.get("auto_approve_scraped"))

    # In auto mode, act on everything in the review queue rather than only
    # what has already been pushed to Telegram, otherwise approval waits on a
    # notification round trip.
    gate = ("" if args.auto else
            "JOIN events e ON e.job_id = j.id AND e.kind = 'queued_telegram'")
    scraped_clause = ("" if (allow_scraped or not args.auto)
                      else "AND j.source NOT LIKE 'jobspy%'")
    rows = conn.execute(
        f"""SELECT DISTINCT j.id, j.company, j.title, j.source, s.score
           FROM jobs j
           {gate}
           LEFT JOIN applications a ON a.job_id = j.id
           LEFT JOIN scores s ON s.job_id = j.id
           WHERE (a.status IS NULL OR a.status IN ('shadow','queued'))
             AND j.filter_status IN ('review','passed')
             {scraped_clause}
           ORDER BY s.score DESC NULLS LAST"""
    ).fetchall()

    if not rows:
        print("nothing pending. Everything sent to Telegram has been acted on.")
        return 0

    print(f"approving {len(rows)} job(s) sent to Telegram:\n")
    for r in rows:
        print(f"  {r['score'] if r['score'] is not None else '-':>4}  "
              f"{r['company'][:24]:26} {r['title'][:38]:40} {r['source']}")

    if args.dry_run:
        print("\ndry run, nothing changed. Drop --dry-run to apply.")
        return 0

    for r in rows:
        with db.txn() as conn2:
            existing = conn2.execute(
                "SELECT 1 FROM applications WHERE job_id=?", (r["id"],)).fetchone()
            if existing:
                conn2.execute(
                    "UPDATE applications SET status='approved' WHERE job_id=? "
                    "AND status != 'submitted'", (r["id"],))
            else:
                conn2.execute(
                    "INSERT INTO applications (job_id, status, queued_at) "
                    "VALUES (?,?,?)", (r["id"], "approved", db.now()))
        db.log_event(r["id"], "telegram_approved", "bulk approve")

    print(f"\napproved {len(rows)}. They will be submitted on the next cycle, "
          f"subject to every guard and the daily cap.")
    return 0


def cmd_digest(args) -> int:
    token, chat = creds()
    conn = db.connect()

    def count(sql: str, params: tuple = ()) -> int:
        return conn.execute(sql, params).fetchone()["n"]

    stats = {
        "seen today": count(
            "SELECT COUNT(*) n FROM jobs WHERE date(fetched_at)=date('now')"),
        "candidates": count(
            "SELECT COUNT(*) n FROM jobs WHERE filter_status IN ('passed','review')"),
        "awaiting you": count(
            "SELECT COUNT(*) n FROM jobs WHERE filter_status='review'"),
        "submitted today": db.submissions_today(),
        "submitted total": count(
            "SELECT COUNT(*) n FROM applications WHERE status='submitted'"),
    }

    top = conn.execute(
        """SELECT j.company, j.title, s.score FROM jobs j JOIN scores s ON s.job_id=j.id
           WHERE j.filter_status IN ('passed','review')
           ORDER BY s.score DESC LIMIT 5"""
    ).fetchall()

    lines = ["<b>job-agent daily digest</b>", ""]
    lines += [f"{k}: <b>{v}</b>" for k, v in stats.items()]
    if db.shadow_mode():
        lines.append("\n<i>shadow mode is ON, nothing is being submitted</i>")
    if top:
        lines.append("\n<b>Top matches</b>")
        lines += [f"{r['score']} - {escape(r['company'])}: {escape(r['title'][:44])}"
                  for r in top]

    call("sendMessage", {
        "chat_id": chat, "text": "\n".join(lines),
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }, token)
    print("digest sent")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("send", help="push queued jobs for approval")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("poll", help="process button presses")
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--forever", action="store_true",
                   help="run continuously, for use as a service")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("approve-all",
                       help="approve everything already sent to Telegram")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--auto", action="store_true",
                   help="approve the whole review queue, not only what was "
                        "already pushed to Telegram. Honours "
                        "approval.auto_approve_scraped in criteria.yaml.")
    p.set_defaults(fn=cmd_approve_all)

    p = sub.add_parser("digest", help="send the daily summary")
    p.set_defaults(fn=cmd_digest)

    args = ap.parse_args()
    db.init()
    try:
        return args.fn(args)
    except NotConfigured as exc:
        print(f"NOT CONFIGURED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
