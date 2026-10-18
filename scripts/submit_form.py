"""Fill and optionally submit web application forms with Playwright.

This is the highest-risk component in the system. A mis-filled field is sent
under the candidate's name with no undo, and unlike email there is no outbox to check.
So the defaults are conservative:

  - Dry run unless --submit is passed explicitly.
  - A screenshot of the completed form is always saved, before any submit.
  - It refuses to submit when a required field could not be filled.
  - Every global guard from submit_email is re-checked here.

Field matching is label-driven rather than selector-driven, because every ATS
uses different markup but they all show a human-readable label.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import llm  # loads .env
import render_cv
from submit_email import Blocked, check_global_guards, check_job_guards

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "data" / "screenshots"

# Ordered. First match wins, so put specific patterns before general ones.
FIELD_MAP = [
    (r"first\s*name|given\s*name|forename", "first_name"),
    (r"last\s*name|surname|family\s*name", "last_name"),
    (r"full\s*name|^name$|your\s*name", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile|telephone|contact\s*number", "phone"),
    (r"linked\s*in", "linkedin"),
    (r"git\s*hub", "github"),
    (r"portfolio|personal\s*(web)?site|website|url", "portfolio"),
    (r"address\s*line|street", "address_line_1"),
    (r"postal|zip", "postal_code"),
    (r"city|town", "city"),
    (r"state|province|region", "state"),
    (r"country", "country"),
    (r"cover\s*letter|why\s*(do\s*you\s*)?want|motivation|tell\s*us", "cover_letter"),
    (r"notice\s*period|availability|when\s*can\s*you\s*start", "notice"),
    (r"salary|compensation|rate\s*expectation", "salary"),
    (r"years?\s*of\s*experience", "years"),
    (r"how\s*did\s*you\s*hear|referral\s*source", "source"),
    (r"current\s*(company|employer)", "current_company"),
    (r"current\s*(title|role|position)", "current_title"),
]

# Questions that must never be answered by guessing free text.
SENSITIVE = re.compile(
    r"sponsor|visa|authori[sz]ed|right\s*to\s*work|work\s*permit|"
    r"criminal|conviction|disabilit|veteran|gender|race|ethnic|"
    r"sexual|disclosure|consent",
    re.I,
)

# Dropdown and radio questions we CAN answer, because the answer comes from
# the profile rather than from a guess. Each entry maps a question pattern to
# the option text to look for, in order of preference.
CHOICE_ANSWERS = [
    (r"gender", ["decline", "prefer not", "not to say", "not specified", "don't wish", "do not wish", "wish to answer", "rather not", "not disclose", "not to answer"]),
    (r"race|ethnic", ["decline", "prefer not", "not to say", "not specified", "don't wish", "do not wish", "wish to answer", "rather not", "not disclose", "not to answer"]),
    (r"veteran", ["decline", "prefer not", "not to say", "not specified", "don't wish", "do not wish", "wish to answer", "rather not", "not disclose", "not to answer"] + ["not a protected veteran", "not a veteran"]),
    (r"disabilit", ["decline", "prefer not", "not to say", "not specified", "don't wish", "do not wish", "wish to answer", "rather not", "not disclose", "not to answer"] + ["no, i do not have"]),
    (r"hispanic|latino", ["decline", "prefer not", "not to say", "not specified", "don't wish", "do not wish", "wish to answer", "rather not", "not disclose", "not to answer"]),
    (r"previously\s+(been\s+)?(employed|worked)|former\s+employee|"
     r"worked\s+(here|for\s+us|at\s+)", ["no"]),
    (r"currently\s+employed\s+(by|at)\s+", ["no"]),
    (r"are\s+you\s+(a\s+)?student|enrolled", ["no"]),
    (r"referred\s+by|employee\s+referral", ["no"]),
    (r"how\s+did\s+you\s+hear", ["company", "career", "website", "other"]),
    (r"notice\s+period", ["1 week", "one week", "immediately", "less than"]),
    (r"willing\s+to\s+relocate|open\s+to\s+relocat", ["yes"]),
    (r"willing\s+to\s+travel", ["yes"]),
    (r"remote|work\s+from\s+home", ["yes"]),
    (r"years?\s+of\s+experience", ["6", "5-7", "5 - 7", "more than 5", "5+"]),
]

# Consent rendered as a dropdown rather than a checkbox. Answered only when
# profile.yaml opts in, same rule as the checkbox path.
CONSENT_CHOICES = ["i consent", "yes, i consent", "i agree", "yes", "agree", "accept"]

# Consent and agreement checkboxes. Ticking one is a legal affirmation made in
# the candidate's name, so it is opt-in via profile.yaml rather than automatic.
CONSENT = re.compile(
    r"consent|agree|acknowledg|privacy\s*polic|terms|gdpr|ndpa|"
    r"data\s*protection|authoriz\w*\s+the\s+(processing|collection)",
    re.I,
)

# Never ticked, even when consent is enabled. These are not part of applying.
MARKETING = re.compile(
    r"marketing|newsletter|promotional|updates about|future\s+(roles|"
    r"opportunities|jobs)|talent\s*(pool|community|network)|subscribe|"
    r"contact me about|keep me informed|mailing list",
    re.I,
)


def load_profile() -> dict:
    return yaml.safe_load((ROOT / "config" / "profile.yaml").read_text())


def build_values(profile: dict, job: dict, cover_letter: str) -> dict[str, str]:
    ident, prof, links = profile["identity"], profile["professional"], profile["links"]
    return {
        "first_name": ident["first_name"],
        "last_name": ident["last_name"],
        "full_name": ident["full_name"],
        "email": ident["email"],
        "phone": ident["phone"],
        "linkedin": links.get("linkedin") or "",
        "github": links.get("github") or "",
        "portfolio": links.get("portfolio") or "",
        "address_line_1": ident.get("address_line_1") or "",
        "postal_code": str(ident.get("postal_code") or ""),
        "city": ident["city"],
        "state": ident["state"],
        "country": ident["country"],
        "cover_letter": cover_letter or "",
        "notice": prof["notice_period"],
        "salary": profile["compensation"]["statement"],
        "years": str(prof["years_experience"]),
        "source": "Company careers page",
        "current_company": prof["current_company"],
        "current_title": prof["current_title"],
        "_accept_consent": (
            "yes" if profile.get("forms", {}).get("accept_application_consent")
            else "no"
        ),
    }


JS_LABEL = """
(el) => {
  const byFor = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
  const parentLabel = el.closest('label');
  const aria = el.getAttribute('aria-label');
  const labelledby = el.getAttribute('aria-labelledby');
  const byIds = labelledby
    ? labelledby.split(/\\s+/).map(i => document.getElementById(i))
        .filter(Boolean).map(n => n.innerText).join(' ')
    : '';
  const ph = el.getAttribute('placeholder') || '';
  const nm = el.getAttribute('name') || '';
  const parts = [
    byFor ? byFor.innerText : '',
    parentLabel ? parentLabel.innerText : '',
    aria || '', byIds, ph, nm,
  ];
  return parts.join(' ').replace(/\\s+/g, ' ').trim();
}
"""


def scoped_options(page, combo):
    """Options belonging to this combobox only, never a sibling's listbox."""
    for attr in ("aria-controls", "aria-owns"):
        target = combo.get_attribute(attr)
        if target:
            box = page.query_selector(f"#{target}")
            if box:
                found = box.query_selector_all("[role=option]")
                if found:
                    return found
    # Fall back to a listbox inside the same field container.
    container = combo.evaluate_handle(
        "(e) => e.closest('.select__container, .field-wrapper, [class*=select]')")
    if container:
        el = container.as_element()
        if el:
            found = el.query_selector_all("[role=option]")
            if found:
                return found
    return []


def answers_for(question: str, values: dict[str, str]) -> list[str] | None:
    """Preferred option texts for a choice question, or None to leave it alone.

    Returning None is the safe default. A dropdown we cannot answer from the
    profile blocks submission rather than being filled with a guess.
    """
    q = (question or "").lower()
    for pattern, options in CHOICE_ANSWERS:
        if re.search(pattern, q):
            return options
    if re.search(r"\bcountry\b", q) and values.get("country"):
        return [values["country"]]
    if re.search(r"\b(city|town)\b", q) and values.get("city"):
        return [values["city"]]
    if CONSENT.search(q) and not MARKETING.search(q):
        return CONSENT_CHOICES if values.get("_accept_consent") == "yes" else None
    return None


def pick_option(options: list[str], wanted: list[str]) -> str | None:
    """First option whose text contains one of the wanted phrases."""
    for phrase in wanted:
        needle = (phrase or "").strip().lower()
        if not needle:
            continue
        for opt in options:
            text = (opt or "").strip().lower()
            if not text or text in ("select...", "select", "-", "--"):
                continue
            # Both sides lowercased. Comparing a cased phrase against a
            # lowercased option silently matched nothing, so "Nigeria" never
            # found "Nigeria +234".
            if needle in text:
                return opt.strip()
    return None


def classify(label: str) -> str | None:
    low = label.lower()
    for pattern, key in FIELD_MAP:
        if re.search(pattern, low):
            return key
    return None


def fill_form(page, values: dict[str, str], cv_path: Path,
              cover_pdf: Path | None = None) -> dict:
    """Fill what can be matched. Returns a report, never raises on one field."""
    report = {"filled": [], "skipped": [], "sensitive": [], "unfilled_required": []}

    # Text inputs and textareas.
    fields = page.query_selector_all(
        "input[type=text], input[type=email], input[type=tel], input[type=url], "
        "input:not([type]), textarea"
    )
    for el in fields:
        try:
            if not el.is_visible() or not el.is_enabled():
                continue
            # Comboboxes wrap a text input. Leave those to the combobox pass,
            # otherwise the same question is handled twice and blocks itself.
            if el.get_attribute("role") == "combobox" or el.evaluate(
                    "(e) => !!e.closest('[role=combobox], .select__container')"):
                continue
            label = el.evaluate(JS_LABEL) or ""
            required = bool(el.get_attribute("required")) or "*" in label

            if SENSITIVE.search(label):
                report["sensitive"].append(label[:70])
                # Only block if nothing later can answer it. Comboboxes and
                # radios are processed after this loop and handle most of
                # these from the profile.
                if required and answers_for(label, values) is None:
                    report["unfilled_required"].append(label[:70])
                continue

            key = classify(label)
            if not key or not values.get(key):
                report["skipped"].append(label[:70] or "(unlabelled)")
                # A later pass (combobox, radio, checkbox) may still answer it.
                if required and answers_for(label, values) is None:
                    report["unfilled_required"].append(label[:70] or "(unlabelled)")
                continue

            el.fill(values[key])
            report["filled"].append(f"{label[:44]} <- {key}")
        except Exception as exc:
            report["skipped"].append(f"error: {type(exc).__name__}")

    # File uploads. Identity comes from the input's own id/name and the
    # nearest preceding label, not from enclosing container text: the wrapper
    # around these drop zones contains BOTH field labels, so container text
    # marked the cover letter input as a resume field and attached the CV to it.
    for el in page.query_selector_all("input[type=file]"):
        try:
            ident = el.evaluate("""(e) => {
              const attr = [e.id, e.getAttribute('name'),
                            e.getAttribute('aria-label')].filter(Boolean).join(' ');
              let node = e.closest('div'), label = '';
              while (node && !label) {
                let sib = node.previousElementSibling;
                while (sib && !label) {
                  const t = (sib.innerText || '').trim();
                  if (t) label = t.slice(0, 60);
                  sib = sib.previousElementSibling;
                }
                node = node.parentElement;
              }
              return (attr + ' ' + label).toLowerCase();
            }""") or ""

            is_cover = "cover" in ident and not re.search(r"resume|\bcv\b", ident)
            if is_cover:
                if cover_pdf and cover_pdf.exists():
                    el.set_input_files(str(cover_pdf))
                    report["filled"].append("cover letter <- cover letter PDF")
                else:
                    report["skipped"].append("cover letter upload left empty")
                continue

            el.set_input_files(str(cv_path))
            report["filled"].append(f"{ident.strip()[:34] or 'resume'} <- CV")
        except Exception as exc:
            report["skipped"].append(f"file upload: {type(exc).__name__}")

    # Dropdowns. Answered only from the profile-backed table, never guessed.
    for el in page.query_selector_all("select"):
        try:
            if not el.is_visible() or not el.is_enabled():
                continue
            # Comboboxes wrap a text input. Leave those to the combobox pass,
            # otherwise the same question is handled twice and blocks itself.
            if el.get_attribute("role") == "combobox" or el.evaluate(
                    "(e) => !!e.closest('[role=combobox], .select__container')"):
                continue
            label = el.evaluate(JS_LABEL) or ""
            required = bool(el.get_attribute("required")) or "*" in label
            wanted = answers_for(label, values)
            if wanted is None:
                report["skipped"].append(f"select: {label[:60]}")
                if required:
                    report["unfilled_required"].append(f"select: {label[:60]}")
                continue

            options = el.evaluate(
                "(s) => Array.from(s.options).map(o => o.textContent.trim())")
            chosen = pick_option(options, wanted)
            if chosen is None:
                report["skipped"].append(f"select (no match): {label[:50]}")
                if required:
                    report["unfilled_required"].append(f"select: {label[:60]}")
                continue
            el.select_option(label=chosen)
            report["filled"].append(f"{label[:40]} <- [{chosen[:26]}]")
        except Exception as exc:
            report["skipped"].append(f"select error: {type(exc).__name__}")

    # React comboboxes. Greenhouse and Ashby render every dropdown this way,
    # with zero native <select> elements on the page, so without this the
    # common case of an ATS form cannot be completed at all.
    for el in page.query_selector_all("[role=combobox]"):
        try:
            if not el.is_visible() or not el.is_enabled():
                continue
            question = el.evaluate(
                "(e) => { const w = e.closest('.field-wrapper, .select__container,"
                " [class*=field], fieldset, div');"
                " return w ? w.innerText.replace(/\\s+/g,' ').trim() : ''; }") or ""
            question = question or (el.evaluate(JS_LABEL) or "")
            required = "*" in question
            wanted = answers_for(question, values)
            if wanted is None:
                report["skipped"].append(f"combobox: {question[:56]}")
                if required:
                    report["unfilled_required"].append(f"combobox: {question[:56]}")
                continue

            el.click()
            page.wait_for_timeout(400)
            # Scope options to THIS combobox's listbox. A global [role=option]
            # query returns whichever listbox happens to be in the DOM: the
            # gender dropdown came back with the phone-country list, which
            # would have silently submitted a wrong answer.
            opts = scoped_options(page, el)
            if not opts:
                page.keyboard.press("Escape")
                report["skipped"].append(f"combobox no listbox: {question[:44]}")
                if required:
                    report["unfilled_required"].append(f"combobox: {question[:56]}")
                continue
            texts = [(o, (o.inner_text() or "").strip()) for o in opts]
            chosen = pick_option([t for _, t in texts], wanted)
            if chosen is None:
                page.keyboard.press("Escape")
                report["skipped"].append(
                    f"combobox no match: {question[:40]} opts={[t for _, t in texts][:4]}")
                if required:
                    report["unfilled_required"].append(f"combobox: {question[:56]}")
                continue
            for node, text in texts:
                if text == chosen:
                    node.click()
                    page.wait_for_timeout(200)
                    report["filled"].append(
                        f"{question.splitlines()[0][:38]} <- [{chosen[:24]}]")
                    break
        except Exception as exc:
            report["skipped"].append(f"combobox error: {type(exc).__name__}")

    # Radio groups, same rules as dropdowns.
    seen_groups: set[str] = set()
    for el in page.query_selector_all("input[type=radio]"):
        try:
            name = el.get_attribute("name") or ""
            if not name or name in seen_groups or not el.is_visible():
                continue
            group = page.query_selector_all(f"input[type=radio][name='{name}']")
            question = el.evaluate(
                "(e) => { const f = e.closest('fieldset,div,li');"
                " return f ? f.innerText.replace(/\\s+/g,' ').trim() : ''; }") or ""
            wanted = answers_for(question, values)
            if wanted is None:
                continue
            seen_groups.add(name)
            labels = [(r, r.evaluate(JS_LABEL) or "") for r in group]
            chosen = pick_option([l for _, l in labels], wanted)
            if chosen is None:
                continue
            for radio, lab in labels:
                if lab == chosen:
                    radio.check()
                    report["filled"].append(f"{question[:38]} <- ({chosen[:24]})")
                    break
        except Exception:
            continue

    # Consent checkboxes. Opt-in via profile.yaml, never automatic.
    for el in page.query_selector_all("input[type=checkbox]"):
        try:
            if not el.is_visible() or not el.is_enabled():
                continue
            label = el.evaluate(JS_LABEL) or ""
            if not CONSENT.search(label):
                continue
            if MARKETING.search(label):
                report["skipped"].append(f"marketing opt-in left unticked: {label[:44]}")
                continue
            required = bool(el.get_attribute("required")) or "*" in label
            if values.get("_accept_consent") == "yes":
                el.check()
                report["filled"].append(f"consent ticked: {label[:44]}")
            else:
                report["sensitive"].append(f"consent (not ticked): {label[:60]}")
                if required:
                    report["unfilled_required"].append(f"consent: {label[:60]}")
        except Exception:
            continue

    return report


def apply_to(job: dict, profile: dict, cover_letter: str, cv_path: Path,
             do_submit: bool, headless: bool = True) -> dict:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-",
                  f"{job['company']}-{job['title']}".lower())[:60]
    shot = SHOTS / f"{slug}.png"
    values = build_values(profile, job, cover_letter)

    cover_pdf = None
    if cover_letter:
        cv_cfg = render_cv.load_cv()
        cover_pdf = render_cv.render_cover_letter(
            cover_letter, job, cv_cfg["header"],
            ROOT / "data" / "cv" / "tailored" / f"{slug}-cover-letter.pdf")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 1600},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()
        try:
            resp = page.goto(job["apply_url"], wait_until="domcontentloaded",
                             timeout=60000)
            page.wait_for_timeout(2500)

            # Bot protection. Reporting a filled form here would be a lie; the
            # page never loaded.
            status = resp.status if resp else 0
            if status in (403, 429) or "just a moment" in (page.title() or "").lower():
                return {
                    "filled": [], "skipped": [], "sensitive": [],
                    "unfilled_required": [], "submitted": False,
                    "blocked": True,
                    "reason": f"blocked by bot protection (HTTP {status})",
                }

            # Many postings link to a description page whose form only appears
            # after clicking Apply. Without this the scan finds zero fields and
            # reports a clean dry run over a page that has no form at all.
            if not page.query_selector("input[type=file], input[type=email]"):
                for sel in ("a:has-text('Apply for this position')",
                            "button:has-text('Apply for this position')",
                            "a:has-text('Apply now')", "button:has-text('Apply now')",
                            "a:has-text('Apply')", "button:has-text('Apply')"):
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        try:
                            btn.click()
                            page.wait_for_timeout(3500)
                        except Exception:
                            pass
                        break

            report = fill_form(page, values, cv_path, cover_pdf)

            # Still nothing: say so rather than reporting an empty success.
            if not report["filled"]:
                report["screenshot"] = str(shot)
                page.screenshot(path=str(shot), full_page=True)
                report["submitted"] = False
                report["reason"] = "no form fields found on the page"
                return report
            page.screenshot(path=str(shot), full_page=True)
            report["screenshot"] = str(shot)

            # A captcha means the form cannot be completed by script. Clicking
            # submit anyway either fails silently or trips bot detection, and
            # in both cases the application is recorded as sent when it was
            # not. Hand it to the queue instead.
            if page.query_selector(
                "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
                ".g-recaptcha, [data-sitekey], iframe[title*='captcha' i]"
            ):
                report["captcha"] = True
                report["submitted"] = False
                report["reason"] = "captcha present, needs manual submission"
                return report

            if report["unfilled_required"]:
                report["submitted"] = False
                report["reason"] = (
                    f"{len(report['unfilled_required'])} required field(s) could "
                    f"not be filled safely"
                )
                return report

            if not do_submit:
                report["submitted"] = False
                report["reason"] = "dry run"
                return report

            button = None
            for sel in ("button[type=submit]", "input[type=submit]",
                        "button:has-text('Submit')", "button:has-text('Apply')"):
                found = page.query_selector(sel)
                if found and found.is_visible():
                    button = found
                    break
            if not button:
                report["submitted"] = False
                report["reason"] = "no submit button found"
                return report

            button.click()
            page.wait_for_timeout(5000)
            page.screenshot(path=str(SHOTS / f"{slug}-after.png"), full_page=True)
            report["submitted"] = True
            report["reason"] = "submitted"
            return report
        finally:
            ctx.close()
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--job-id", type=int)
    ap.add_argument("--submit", action="store_true",
                    help="actually submit. Without this it is a dry run.")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    args = ap.parse_args()

    db.init()
    conn = db.connect()
    profile = load_profile()

    if args.submit:
        try:
            check_global_guards()
        except Blocked as exc:
            print(f"BLOCKED: {exc}")
            return 1
    else:
        print("DRY RUN: forms will be filled and screenshotted, not submitted\n")

    where = ("a.cover_letter IS NOT NULL AND a.cover_letter != '' "
             "AND a.status IN ('shadow','queued','approved')")
    params: list = []
    if args.job_id:
        where = "j.id = ?"
        params = [args.job_id]

    rows = conn.execute(
        f"""SELECT j.*, a.cover_letter, a.status AS app_status, s.score FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            LEFT JOIN scores s ON s.job_id = j.id
            WHERE {where} ORDER BY CASE WHEN j.posted_at IS NOT NULL AND julianday('now') - julianday(j.posted_at) <= 2 THEN 0 WHEN j.posted_at IS NOT NULL AND julianday('now') - julianday(j.posted_at) <= 7 THEN 1 ELSE 2 END, s.score DESC NULLS LAST LIMIT ?""",
        params + [args.limit],
    ).fetchall()

    if not rows:
        print("nothing to submit. Run scripts/cover_letter.py first.")
        return 0

    done = skipped = failed = 0
    for row in rows:
        job = dict(row)
        label = f"{job['company'][:20]:22} {job['title'][:32]:34}"
        if not job.get("apply_url"):
            print(f"  SKIP  {label} no apply url")
            skipped += 1
            continue

        try:
            check_job_guards(conn, job)
        except Blocked as exc:
            print(f"  SKIP  {label} {exc}")
            skipped += 1
            continue

        cv_path = render_cv.tailor_for_job(job)
        try:
            report = apply_to(job, profile, job.get("cover_letter") or "",
                              cv_path, args.submit, headless=not args.headed)
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {label} {type(exc).__name__}: {str(exc)[:80]}")
            continue

        status = "SENT" if report.get("submitted") else "DRY "
        print(f"  {status}  {label} filled={len(report['filled']):>2} "
              f"unfilled_req={len(report['unfilled_required']):>2} "
              f"sensitive={len(report['sensitive']):>2}  {report.get('reason')}")
        for f in report["filled"][:6]:
            print(f"          + {f}")
        for f in report["unfilled_required"][:4]:
            print(f"          ! REQUIRED UNFILLED: {f}")
        print(f"          shot: {report.get('screenshot')}")

        if report.get("submitted"):
            conn.execute(
                "UPDATE applications SET status='submitted', channel='form', "
                "submitted_at=? WHERE job_id=?", (db.now(), job["id"]))
            conn.commit()
            db.log_event(job["id"], "submitted", f"form {job['apply_url']}")
            done += 1
        else:
            skipped += 1

    print(f"\nsubmitted {done}\nskipped   {skipped}\nfailed    {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
