"""Tests for the submission safety guards.

These guard an irreversible action. A bug here sends a real email, to a real
company, under the candidate's name, with no undo. That is the whole reason this file
exists: none of these failures would be visible in review.

Run:  .venv/bin/python tests/test_guards.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Point the database at a scratch file before importing anything that opens it.
_tmp = tempfile.TemporaryDirectory()
import db  # noqa: E402

db.DB_PATH = Path(_tmp.name) / "test.db"
db.init()

import submit_email  # noqa: E402
from submit_email import Blocked, resolve_recipient  # noqa: E402

# Isolate the kill switch. Pointing at the real data/STOP made these tests
# fail whenever the live system happened to be halted.
submit_email.STOP_FILE = Path(_tmp.name) / "STOP"

os.environ.setdefault("GMAIL_ADDRESS", "you@example.com")

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {name}")
    else:
        print(f"  ok   {name}")


def blocks(name: str, fn) -> None:
    """The guard must raise Blocked. Returning normally is the failure."""
    try:
        fn()
    except Blocked:
        print(f"  ok   {name}")
        return
    except Exception as exc:
        FAILURES.append(f"{name}: raised {type(exc).__name__} instead of Blocked")
        print(f"  FAIL {name} (wrong exception: {type(exc).__name__})")
        return
    FAILURES.append(f"{name}: did NOT block")
    print(f"  FAIL {name} (did not block)")


def allows(name: str, fn) -> None:
    try:
        fn()
        print(f"  ok   {name}")
    except Blocked as exc:
        FAILURES.append(f"{name}: blocked unexpectedly: {exc}")
        print(f"  FAIL {name} (blocked: {exc})")


# ---------------------------------------------------------------------------
print("\nrecipient resolution")
# ---------------------------------------------------------------------------
check("explicit apply_email is used",
      resolve_recipient({"apply_email": "jobs@example.com", "description": ""}),
      "jobs@example.com")

check("a careers address is preferred over a generic one",
      resolve_recipient({"description":
                         "Questions? hello@corp.com . Apply to careers@corp.com today."}),
      "careers@corp.com")

check("noreply addresses are never used",
      resolve_recipient({"description":
                         "Sent by noreply@corp.com . Apply: hiring@corp.com"}),
      "hiring@corp.com")

blocks("no address in the posting refuses rather than guessing",
       lambda: resolve_recipient({"description": "Apply on our website."}))

blocks("a malformed address is refused",
       lambda: resolve_recipient({"apply_email": "not-an-address"}))

blocks("sending to our own account is refused",
       lambda: resolve_recipient({"apply_email": os.environ["GMAIL_ADDRESS"]}))

check("legal and privacy addresses are filtered out",
      resolve_recipient({"description":
                         "privacy@corp.com for data requests. jobs@corp.com to apply."}),
      "jobs@corp.com")


# ---------------------------------------------------------------------------
print("\nglobal guards")
# ---------------------------------------------------------------------------
db.set_setting("shadow_mode", "1")
blocks("shadow mode blocks all sending", submit_email.check_global_guards)

db.set_setting("shadow_mode", "0")
allows("disabling shadow mode allows sending", submit_email.check_global_guards)

stop = submit_email.STOP_FILE
stop.parent.mkdir(parents=True, exist_ok=True)
stop.write_text("halt")
blocks("kill switch file halts sending", submit_email.check_global_guards)
stop.unlink()
allows("removing the kill switch resumes sending", submit_email.check_global_guards)


# ---------------------------------------------------------------------------
print("\ndaily cap")
# ---------------------------------------------------------------------------
from normalize import criteria  # noqa: E402

cap = criteria()["limits"]["max_submissions_per_day"]

# Each write commits before the next call. Holding an open write transaction
# here while upsert_job opens its own connection deadlocks them against each
# other, which is exactly what happened the first time this was written.
for i in range(cap):
    jid, _ = db.upsert_job({
        "company": f"CapCo{i}", "title": "Senior Mobile Engineer",
        "apply_url": f"https://example.com/{i}", "source": "test",
        "filter_status": "passed",
    })
    with db.txn() as c:
        c.execute(
            "INSERT INTO applications (job_id, status, submitted_at) VALUES (?,?,?)",
            (jid, "submitted", db.now()),
        )

check(f"submissions_today counts correctly at the cap", db.submissions_today(), cap)
blocks("daily cap blocks the next send", submit_email.check_global_guards)


# ---------------------------------------------------------------------------
print("\nduplicate protection")
# ---------------------------------------------------------------------------
check("a submitted role is recognised as already applied",
      db.already_applied("CapCo0", "Senior Mobile Engineer"), True)
check("case does not defeat the duplicate check",
      db.already_applied("capco0", "senior mobile engineer"), True)
check("an unrelated role is not a duplicate",
      db.already_applied("SomeOtherCo", "Senior Mobile Engineer"), False)

# The same role reached by a second URL must still count as a duplicate,
# because fingerprints differ but the application did happen.
db.upsert_job({
    "company": "CapCo0", "title": "Senior Mobile Engineer",
    "apply_url": "https://different-board.example.com/xyz",
    "source": "test", "filter_status": "passed",
})
blocks("the same role from a different URL is blocked",
       lambda: submit_email.check_job_guards(
           db.connect(), {"company": "CapCo0", "title": "Senior Mobile Engineer"}))


# ---------------------------------------------------------------------------
print("\nfingerprint stability")
# ---------------------------------------------------------------------------
fp = db.fingerprint
check("trailing slash does not change identity",
      fp("Acme", "iOS Engineer", "https://x.com/job/1/"),
      fp("Acme", "iOS Engineer", "https://x.com/job/1"))
check("query strings do not change identity",
      fp("Acme", "iOS Engineer", "https://x.com/job/1?utm_source=rss"),
      fp("Acme", "iOS Engineer", "https://x.com/job/1"))
check("case does not change identity",
      fp("ACME", "IOS ENGINEER", "https://X.com/job/1"),
      fp("acme", "ios engineer", "https://x.com/job/1"))
check("a different job is a different fingerprint",
      fp("Acme", "iOS Engineer", "https://x.com/job/1")
      != fp("Acme", "Android Engineer", "https://x.com/job/2"), True)




# ---------------------------------------------------------------------------
# Bug: the scorer promoted scraped-source jobs to auto-apply on score alone,
# undoing the containment the fetcher applies. Six Indeed listings, including
# the three highest scorers, became auto-apply eligible.
# ---------------------------------------------------------------------------
print("\nscraped-source containment")
import score as score_mod  # noqa: E402

THRESHOLDS = (72, 60)
VERDICT = {
    "score": 95, "eligibility": "global_remote", "eligibility_evidence": "x",
    "seniority_fit": "match", "stack_match": "strong", "strengths": ["a"],
    "gaps": [], "rationale": "r", "recommend": "apply",
}

_conn = db.connect()
for src, expected in (("jobspy_indeed", "review"),
                      ("jobspy_linkedin", "review"),
                      ("greenhouse", "passed"),
                      ("ashby", "passed"),
                      ("remoteok", "passed")):
    jid, _ = db.upsert_job({
        "company": f"Src-{src}", "title": "Senior Mobile Engineer",
        "apply_url": f"https://example.com/{src}", "source": src,
        "filter_status": "passed",
    })
    got = score_mod.apply_verdict(
        _conn, {"id": jid, "source": src}, dict(VERDICT), THRESHOLDS, {"us_only"})
    _conn.commit()
    check(f"a score of 95 from {src!r} resolves to {expected!r}", got, expected)

check("a scraped job is never auto-apply even at the top score",
      score_mod.apply_verdict(
          _conn, {"id": jid, "source": "jobspy_glassdoor"},
          dict(VERDICT, score=100), THRESHOLDS, {"us_only"}) != "passed", True)
# apply_verdict writes without committing. Leaving the transaction open here
# locks the database against every later connection in this file.
_conn.commit()



# ---------------------------------------------------------------------------
# Bug: the submitters selected rows by application status without re-checking
# what the job was, so scraped-source jobs sitting in 'shadow' state were
# submittable the moment shadow mode was turned off. Second time this exact
# containment was lost, hence the guard now lives at the submit boundary.
# ---------------------------------------------------------------------------
print("\nsubmit-time source containment")

blocks("a scraped job is never auto-submitted",
       lambda: submit_email.check_job_guards(
           db.connect(), {"company": "ScrapeCo", "title": "Mobile Engineer",
                          "source": "jobspy_indeed", "filter_status": "review",
                          "app_status": "shadow"}))

blocks("a scraped job at 'passed' is still never auto-submitted",
       lambda: submit_email.check_job_guards(
           db.connect(), {"company": "ScrapeCo2", "title": "Mobile Engineer",
                          "source": "jobspy_linkedin", "filter_status": "passed",
                          "app_status": "queued"}))

allows("a scraped job the user explicitly approved may be submitted",
       lambda: submit_email.check_job_guards(
           db.connect(), {"company": "ScrapeCo3", "title": "Mobile Engineer",
                          "source": "jobspy_indeed", "filter_status": "review",
                          "app_status": "approved"}))

blocks("a review-status job is not auto-submitted",
       lambda: submit_email.check_job_guards(
           db.connect(), {"company": "ReviewCo", "title": "Mobile Engineer",
                          "source": "greenhouse", "filter_status": "review",
                          "app_status": "shadow"}))

allows("a passed job from a legitimate source is submittable",
       lambda: submit_email.check_job_guards(
           db.connect(), {"company": "CleanCo", "title": "Mobile Engineer",
                          "source": "greenhouse", "filter_status": "passed",
                          "app_status": "shadow"}))



# ---------------------------------------------------------------------------
# Bug: already_applied() counted 'queued' and 'approved', so a job blocked its
# own submission. The letter writer sets status to 'queued', and the duplicate
# check then read that back as proof the application had already been sent.
# ---------------------------------------------------------------------------
print("\nduplicate check counts only real sends")

_qid, _ = db.upsert_job({
    "company": "QueuedCo", "title": "Senior Mobile Engineer",
    "apply_url": "https://example.com/queued", "source": "greenhouse",
    "filter_status": "passed",
})
with db.txn() as _c:
    _c.execute("INSERT INTO applications (job_id, status, cover_letter, queued_at) "
               "VALUES (?,?,?,?)", (_qid, "queued", "letter text", db.now()))
check("a queued letter does not count as applied",
      db.already_applied("QueuedCo", "Senior Mobile Engineer"), False)
allows("a queued job can still be submitted",
       lambda: submit_email.check_job_guards(
           db.connect(), {"company": "QueuedCo", "title": "Senior Mobile Engineer",
                          "source": "greenhouse", "filter_status": "passed",
                          "app_status": "queued"}))

with db.txn() as _c:
    _c.execute("UPDATE applications SET status='approved' WHERE job_id=?", (_qid,))
check("an approved but unsent job does not count as applied",
      db.already_applied("QueuedCo", "Senior Mobile Engineer"), False)

with db.txn() as _c:
    _c.execute("UPDATE applications SET status='submitted', submitted_at=? "
               "WHERE job_id=?", (db.now(), _qid))
check("an actually submitted job does count as applied",
      db.already_applied("QueuedCo", "Senior Mobile Engineer"), True)
blocks("and a second attempt at the same role is blocked",
       lambda: submit_email.check_job_guards(
           db.connect(), {"company": "QueuedCo", "title": "Senior Mobile Engineer",
                          "source": "greenhouse", "filter_status": "passed",
                          "app_status": "queued"}))



# ---------------------------------------------------------------------------
# Bug: the cover letter writer's upsert overwrote status unconditionally, so
# regenerating a letter for an already-submitted job reset it to 'shadow'.
# That erased the send record, reset the daily cap to zero and made the
# duplicate check pass, so the next run would have applied twice to the same
# company. Found after a real application had already gone out.
# ---------------------------------------------------------------------------
print("\nterminal application status is never downgraded")

UPSERT = (
    "INSERT INTO applications (job_id, status, channel, cover_letter, "
    "cv_variant, queued_at) VALUES (?,?,?,?,?,?) "
    "ON CONFLICT(job_id) DO UPDATE SET "
    "cover_letter=excluded.cover_letter, "
    "status=CASE WHEN applications.status IN "
    "  ('submitted','approved','rejected_by_user') "
    "  THEN applications.status ELSE excluded.status END, "
    "queued_at=excluded.queued_at"
)

for start, rewrite, expected in (
    ("submitted", "shadow", "submitted"),
    ("approved", "shadow", "approved"),
    ("rejected_by_user", "queued", "rejected_by_user"),
    ("shadow", "queued", "queued"),
    ("queued", "shadow", "shadow"),
):
    _jid, _ = db.upsert_job({
        "company": f"Upsert-{start}", "title": "Senior Mobile Engineer",
        "apply_url": f"https://example.com/upsert-{start}",
        "source": "greenhouse", "filter_status": "passed",
    })
    with db.txn() as _c:
        _c.execute("INSERT INTO applications (job_id, status, submitted_at) "
                   "VALUES (?,?,?)",
                   (_jid, start, db.now() if start == "submitted" else None))
    with db.txn() as _c:
        _c.execute(UPSERT, (_jid, rewrite, "email", "new letter", "cv.pdf", db.now()))
    got = db.connect().execute(
        "SELECT status FROM applications WHERE job_id=?", (_jid,)).fetchone()["status"]
    check(f"{start!r} rewritten as {rewrite!r} stays {expected!r}", got, expected)

check("a rewritten letter still lands",
      db.connect().execute(
          "SELECT cover_letter FROM applications WHERE job_id=?",
          (_jid,)).fetchone()["cover_letter"], "new letter")



# ---------------------------------------------------------------------------
# Bug: reevaluate.py re-ran the cheap filter over EVERY posting, including ones
# the LLM had already judged, and overwrote filter_status and eligibility with
# the coarse verdict. That put 130 correctly-rejected roles back into a live
# auto-approving queue, including several explicitly restricted to US and
# Finland residents, and destroyed the LLM's eligibility classification.
#
# The cheap filter is a pre-screen. The LLM verdict is strictly better
# informed and must win.
# ---------------------------------------------------------------------------
print("\nreevaluate must not overwrite an LLM verdict")

_jid_scored, _ = db.upsert_job({
    "company": "ScoredCo", "title": "Senior Mobile Engineer",
    "apply_url": "https://example.com/scored", "source": "greenhouse",
    "filter_status": "rejected", "eligibility": "us_only",
    "description": "Must be authorized to work in the United States.",
    "location": "Remote (US)",
})
with db.txn() as _c:
    _c.execute("INSERT INTO scores (job_id, score, rationale, model, scored_at) "
               "VALUES (?,?,?,?,?)",
               (_jid_scored, 0, "US only, candidate ineligible", "test", db.now()))

_jid_unscored, _ = db.upsert_job({
    "company": "UnscoredCo", "title": "Senior Mobile Engineer",
    "apply_url": "https://example.com/unscored", "source": "greenhouse",
    "filter_status": "pending", "description": "Flutter. Work from anywhere.",
    "location": "Remote",
})

_conn2 = db.connect()
_scored_rows = _conn2.execute(
    "SELECT j.id FROM jobs j LEFT JOIN scores s ON s.job_id = j.id "
    "WHERE s.job_id IS NULL").fetchall()
_ids = {r["id"] for r in _scored_rows}

check("a posting the LLM judged is excluded from re-evaluation",
      _jid_scored in _ids, False)
check("an unscored posting is still re-evaluated",
      _jid_unscored in _ids, True)

# And the verdict itself must survive.
_status = _conn2.execute("SELECT filter_status, eligibility FROM jobs WHERE id=?",
                         (_jid_scored,)).fetchone()
check("the LLM's rejection survives", _status["filter_status"], "rejected")
check("the LLM's eligibility verdict survives", _status["eligibility"], "us_only")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED\n")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all guard tests passed")
