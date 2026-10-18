"""Send applications by email, with every safety rail enforced at send time.

This is the only module that acts irreversibly on the outside world. Its job
is to be paranoid. Every guard is re-checked here rather than trusted from
earlier in the pipeline, because the gap between "decided to apply" and
"actually applied" is where duplicates and mistakes happen.

Guards, in order:
  1. shadow mode        nothing sends at all
  2. kill switch        data/STOP halts everything immediately
  3. daily cap          hard stop, not a warning
  4. per-company cap    avoids carpet-bombing one employer
  5. duplicate check    re-run at send time, not just at ingest
  6. CV attachment      must exist and be non-empty
  7. recipient sanity   must be a real address, never the candidate's own
"""

from __future__ import annotations

import argparse
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import llm  # loads .env
import render_cv
from normalize import criteria

ROOT = Path(__file__).resolve().parent.parent
STOP_FILE = ROOT / "data" / "STOP"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class Blocked(RuntimeError):
    """A guard refused. Never caught and ignored."""


def load_profile() -> dict:
    return yaml.safe_load((ROOT / "config" / "profile.yaml").read_text())


def check_global_guards() -> None:
    if db.shadow_mode():
        raise Blocked(
            "shadow mode is ON, nothing will be sent.\n"
            "  Disable only after reviewing generated letters:\n"
            "  .venv/bin/python -c \"import sys;sys.path.insert(0,'scripts');"
            "import db;db.set_setting('shadow_mode','0')\""
        )
    if STOP_FILE.exists():
        raise Blocked(f"kill switch present at {STOP_FILE}, refusing to send")

    cap = criteria()["limits"]["max_submissions_per_day"]
    sent = db.submissions_today()
    if sent >= cap:
        raise Blocked(f"daily cap reached: {sent}/{cap} already submitted today")


SCRAPED_PREFIX = "jobspy"


def check_job_guards(conn, job: dict) -> None:
    """Per-job guards. Re-checked at send time, never trusted from upstream.

    The two checks below exist because the containment applied at fetch time
    was silently lost twice: once when the scorer overwrote filter_status on
    score alone, and once here, where the submitter selected on application
    status without re-checking what the job actually was.
    """
    app_status = str(job.get("app_status") or job.get("status") or "")
    user_approved = app_status == "approved"

    # Scraped sources require an explicit human approval, always.
    source = str(job.get("source") or "")
    if source.startswith(SCRAPED_PREFIX) and not user_approved:
        raise Blocked(
            f"scraped source ({source}) requires explicit approval, "
            f"never auto-submitted"
        )

    # Anything not cleared for auto-apply needs a human approval too.
    if job.get("filter_status") not in ("passed", None) and not user_approved:
        raise Blocked(
            f"job status is '{job.get('filter_status')}', not approved for "
            f"automatic submission"
        )

    if db.already_applied(job["company"], job["title"]):
        raise Blocked(f"already applied to {job['company']} / {job['title']}")

    cap = criteria()["limits"]["max_submissions_per_company_per_month"]
    n = conn.execute(
        """SELECT COUNT(*) n FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE lower(j.company) = lower(?) AND a.status = 'submitted'
             AND a.submitted_at >= date('now', '-30 days')""",
        (job["company"],),
    ).fetchone()["n"]
    if n >= cap:
        raise Blocked(f"company cap reached for {job['company']}: {n}/{cap} in 30 days")


def resolve_recipient(job: dict) -> str:
    """Find the address to apply to. Refuses to guess."""
    if job.get("apply_email"):
        candidate = job["apply_email"].strip()
    else:
        blob = f"{job.get('description') or ''}"
        matches = EMAIL_RE.findall(blob)
        # Filter out addresses that are obviously not for applications.
        noise = ("noreply", "no-reply", "privacy", "legal", "support@",
                 "info@", "sales@", "press@", "security@", "abuse@")
        usable = [m for m in matches if not any(n in m.lower() for n in noise)]
        preferred = [m for m in usable
                     if any(k in m.lower() for k in
                            ("job", "career", "hiring", "recruit", "apply", "hr@", "talent"))]
        pool = preferred or usable
        if not pool:
            raise Blocked("no application email address found in the posting")
        candidate = pool[0]

    if not EMAIL_RE.fullmatch(candidate):
        raise Blocked(f"recipient is not a valid address: {candidate!r}")
    own = os.environ.get("GMAIL_ADDRESS", "").lower()
    if candidate.lower() == own:
        raise Blocked("recipient equals the sending account, refusing")
    return candidate


def subject_role(title: str) -> str:
    """A single role name fit for an email subject.

    Hacker News postings list every open role in one line, which produced a
    180-character subject naming four different jobs. Take the first role and
    cap the length.
    """
    first = re.split(r"\s*(?:,|/|\||;| or )\s*", (title or "").strip())[0]
    first = first.strip(" -|,")
    if len(first) > 70:
        first = first[:67].rsplit(" ", 1)[0] + "..."
    return first or "Application"


def build_message(profile: dict, job: dict, body: str, recipient: str,
                  cv_path: Path) -> EmailMessage:
    ident = profile["identity"]
    sender = os.environ["GMAIL_ADDRESS"]

    msg = EmailMessage()
    msg["From"] = f"{ident['full_name']} <{sender}>"
    msg["To"] = recipient
    msg["Reply-To"] = ident["email"]
    msg["Subject"] = f"{subject_role(job['title'])} - {ident['full_name']}"

    greeting = "Hello,"
    signoff = (
        f"\n\nBest regards,\n{ident['full_name']}\n"
        f"{profile['professional']['headline']}\n"
        f"{ident['email']} | {ident['phone']}\n"
        f"{profile['links']['portfolio']} | {profile['links']['github']}"
    )
    msg.set_content(f"{greeting}\n\n{body}{signoff}")

    data = cv_path.read_bytes()
    msg.add_attachment(
        data,
        maintype="application",
        subtype="pdf",
        filename=f"{ident['full_name'].replace(' ', '-')}-CV.pdf",
    )
    return msg


def send(msg: EmailMessage) -> None:
    """Deliver over whichever SMTP port this network actually allows.

    465 is tried first. Port 587 worked here initially and then started timing
    out at the TCP layer while 465 stayed up, which is a network or ISP block
    on the submission port rather than anything to do with Gmail. Trying both
    means a blocked port degrades to a slower path instead of a dead system.
    """
    addr = os.environ["GMAIL_ADDRESS"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    ctx = ssl.create_default_context()
    errors = []

    for port, use_ssl in ((465, True), (587, False)):
        try:
            if use_ssl:
                conn = smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=45,
                                        context=ctx)
            else:
                conn = smtplib.SMTP("smtp.gmail.com", port, timeout=45)
                conn.starttls(context=ctx)
            with conn as s:
                s.login(addr, pw)
                s.send_message(msg)
            return
        except (OSError, smtplib.SMTPException) as exc:
            errors.append(f"{port}: {type(exc).__name__}")

    raise RuntimeError("all SMTP routes failed (" + ", ".join(errors) + ")")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true",
                    help="run every guard and build the message, but do not send")
    args = ap.parse_args()

    db.init()
    conn = db.connect()
    profile = load_profile()

    cv_path = ROOT / profile["cv"]["base_pdf"]
    if not cv_path.exists() or cv_path.stat().st_size == 0:
        print(f"CV missing or empty at {cv_path}")
        return 1

    if not args.dry_run:
        try:
            check_global_guards()
        except Blocked as exc:
            print(f"BLOCKED: {exc}")
            return 1
    else:
        print("DRY RUN: no email will be sent\n")

    rows = conn.execute(
        """SELECT j.*, a.cover_letter, a.status AS app_status, s.score FROM applications a
           JOIN jobs j ON j.id = a.job_id
           LEFT JOIN scores s ON s.job_id = j.id
           WHERE a.cover_letter IS NOT NULL AND a.cover_letter != ''
             AND a.status IN ('shadow','queued','approved')
           ORDER BY CASE
                      WHEN j.posted_at IS NOT NULL
                       AND julianday('now') - julianday(j.posted_at) <= 2 THEN 0
                      WHEN j.posted_at IS NOT NULL
                       AND julianday('now') - julianday(j.posted_at) <= 7 THEN 1
                      ELSE 2 END,
                    s.score DESC"""
    ).fetchall()

    sent = blocked = failed = 0
    for row in rows:
        if sent >= args.limit:
            break
        job = dict(row)
        label = f"{job['company'][:22]:24} {job['title'][:34]:36}"
        try:
            # Run in dry-run too, otherwise a dry run reports sends that the
            # real run would refuse.
            check_job_guards(conn, job)
            recipient = resolve_recipient(job)
            # Attach a CV restructured for this posting, matching what the
            # form submitter does. Sending the generic one by email was an
            # inconsistency, not a decision.
            try:
                attach = render_cv.tailor_for_job(job)
            except Exception:
                attach = cv_path
            msg = build_message(profile, job, job["cover_letter"], recipient, attach)
        except Blocked as exc:
            blocked += 1
            print(f"  SKIP  {label} {exc}")
            continue

        if args.dry_run:
            sent += 1
            print(f"  WOULD SEND  {label} -> {recipient}  "
                  f"({len(job['cover_letter'].split())}w, cv {cv_path.stat().st_size // 1024}kb)")
            continue

        try:
            send(msg)
            conn.execute(
                "UPDATE applications SET status='submitted', channel='email', "
                "submitted_at=?, error=NULL WHERE job_id=?",
                (db.now(), job["id"]),
            )
            conn.commit()
            db.log_event(job["id"], "submitted", f"email to {recipient}")
            sent += 1
            print(f"  SENT  {label} -> {recipient}")
        except Exception as exc:
            failed += 1
            conn.execute(
                "UPDATE applications SET status='failed', error=? WHERE job_id=?",
                (f"{type(exc).__name__}: {exc}", job["id"]),
            )
            conn.commit()
            print(f"  FAIL  {label} {type(exc).__name__}: {str(exc)[:80]}")

    verb = "would send" if args.dry_run else "sent"
    print(f"\n{verb}   {sent}")
    print(f"skipped  {blocked}")
    print(f"failed   {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
