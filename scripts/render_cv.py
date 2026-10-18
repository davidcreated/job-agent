"""Generate a tailored CV PDF per job from config/cv.yaml.

Tailoring is deterministic keyword matching, not an LLM call. Three reasons:
it costs no quota, it is reproducible, and it cannot invent a claim. The model
never touches the CV; it only ever selects and reorders content that is
already true.

The relevance signal is tag overlap between the job description and the tags
on each bullet, skill group and project. Highest-signal content moves up, the
weakest is dropped to hold the page count.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, KeepTogether, ListFlowable, ListItem, Paragraph,
    SimpleDocTemplate, Spacer,
)

ROOT = Path(__file__).resolve().parent.parent

# Which words in a job description imply which CV tag. Deliberately narrow:
# a false tag match reorders the CV wrongly, which is worse than no tailoring.
TAG_SIGNALS = {
    "flutter": ["flutter", "dart", "bloc", "cubit", "riverpod", "getx"],
    "react_native": ["react native", "react-native", "expo", "redux"],
    "ios": ["ios", "swift", "swiftui", "uikit", "xcode", "testflight", "apns"],
    "android": ["android", "jetpack", "compose", "gradle", "play console"],
    "kotlin": ["kotlin"],
    "python": ["python", "django", "flask", "fastapi"],
    "javascript": ["javascript", "typescript", "node", "nodejs", "node.js"],
    "frontend": ["frontend", "front-end", "react", "vue", "angular", "css"],
    "cloud": ["aws", "gcp", "azure", "kubernetes", "terraform", "docker"],
    "devops": ["devops", "sre", "site reliability", "ci/cd", "observability"],
    "data": ["data engineer", "data pipeline", "spark", "airflow", "etl"],
    "ml": ["machine learning", "ml ", "llm", "pytorch", "tensorflow"],
    "java": ["java ", "spring", "jvm"],
    "rust": ["rust"],
    "kmp": ["kotlin multiplatform", "kmp", "multiplatform"],
    "swift": ["swift", "swiftui"],
    "backend": ["golang", " go ", "backend", "api", "fastapi", "python",
                "microservice", "node", "postgres", "graphql"],
    "fintech": ["fintech", "payment", "wallet", "banking", "transaction",
                "kyc", "card", "ledger", "money"],
    "health": ["health", "medical", "telemedicine", "patient", "clinical", "hipaa"],
    "iot": ["iot", "device", "firmware", "embedded", "mqtt", "hardware",
            "bluetooth", "ble", "sensor"],
    # Deliberately strict. A bare "lead" matches "leading", "leader" and
    # "leads" in almost any description, which inflated leadership above the
    # role's actual stack and retitled a Flutter CV as an engineering lead.
    "leadership": ["team lead", "tech lead", "engineering manager", "mentor",
                   "principal engineer", "staff engineer", "head of",
                   "line management", "manage a team", "lead the team",
                   "architect"],
    "mobile": ["mobile", "app store", "google play", "cross-platform",
               "cross platform"],
}


def load_cv() -> dict:
    return yaml.safe_load((ROOT / "config" / "cv.yaml").read_text())


TITLE_WEIGHT = 6


def derive_tags(text: str, title: str = "") -> dict[str, int]:
    """Weight each tag by how strongly the posting signals it.

    The job title counts far more than the body. A title is a deliberate
    statement of what the role is; the body mentions everything the company
    happens to use. Without this, incidental words in a long description
    outvote the one line that actually names the job.
    """
    low = f" {(text or '').lower()} "
    title_low = f" {(title or '').lower()} "
    weights: dict[str, int] = {}
    for tag, signals in TAG_SIGNALS.items():
        hits = sum(low.count(s) for s in signals)
        hits += TITLE_WEIGHT * sum(title_low.count(s) for s in signals)
        if hits:
            weights[tag] = hits
    return weights


def relevance(item_tags: list[str] | None, weights: dict[str, int]) -> int:
    if not item_tags:
        return 0
    return sum(weights.get(t, 0) for t in item_tags)


def primary_stack(weights: dict[str, int], emphasis_keys=()) -> str | None:
    """Which emphasis paragraph to append to the summary.

    Candidates are the keys the CV defines under summary.emphasis, so any
    stack works as long as cv.yaml has a paragraph for it.
    """
    order = list(emphasis_keys)
    ranked = sorted(
        ((t, weights.get(t, 0)) for t in order), key=lambda kv: -kv[1]
    )
    return ranked[0][0] if ranked and ranked[0][1] > 0 else None


def build_styles():
    base = getSampleStyleSheet()
    return {
        "name": ParagraphStyle(
            "name", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=17, leading=20, spaceAfter=1),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontName="Helvetica",
            fontSize=8.6, leading=11, textColor=colors.HexColor("#444444"),
            spaceAfter=1),
        "contact": ParagraphStyle(
            "contact", parent=base["Normal"], fontName="Helvetica",
            fontSize=8, leading=10.5, textColor=colors.HexColor("#333333"),
            spaceAfter=5),
        "section": ParagraphStyle(
            "section", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=9.4, leading=12, textColor=colors.HexColor("#111111"),
            spaceBefore=7, spaceAfter=2),
        "role": ParagraphStyle(
            "role", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=9.2, leading=11.5, spaceBefore=4),
        "meta": ParagraphStyle(
            "meta", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=7.9, leading=10, textColor=colors.HexColor("#555555"),
            spaceAfter=2),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontName="Helvetica", fontSize=8.5,
            leading=11, alignment=TA_JUSTIFY, spaceAfter=1),
        "bullet": ParagraphStyle(
            "bullet", parent=base["Normal"], fontName="Helvetica", fontSize=8.4,
            leading=10.8, alignment=TA_JUSTIFY),
        "skill": ParagraphStyle(
            "skill", parent=base["Normal"], fontName="Helvetica", fontSize=8.3,
            leading=10.6, spaceAfter=1.5),
    }


def esc(text: str) -> str:
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def render(cv: dict, weights: dict[str, int], out_path: Path,
           role_title: str | None = None, max_skills: int = 7,
           max_bullets: int = 5) -> Path:
    st = build_styles()
    h = cv["header"]
    story = []

    story.append(Paragraph(esc(h["name"]), st["name"]))
    story.append(Paragraph(esc(role_title or h["default_subtitle"]), st["subtitle"]))
    contact = " | ".join([
        esc(h["email"]), esc(h["phone"]), esc(h["location"]),
        esc(h["linkedin"]), esc(h["github"]), esc(h["portfolio"]),
    ])
    story.append(Paragraph(contact, st["contact"]))
    story.append(HRFlowable(width="100%", thickness=0.6,
                            color=colors.HexColor("#999999"), spaceAfter=3))

    # Summary, with at most one emphasis sentence appended.
    summary = clean(cv["summary"]["base"])
    stack = primary_stack(weights, cv["summary"].get("emphasis", {}))
    if stack and stack in cv["summary"]["emphasis"]:
        summary += " " + clean(cv["summary"]["emphasis"][stack])
    story.append(Paragraph("PROFILE", st["section"]))
    story.append(Paragraph(esc(summary), st["body"]))

    # Skills, most relevant group first.
    groups = sorted(cv["skills"],
                    key=lambda g: -relevance(g.get("tags"), weights))
    story.append(Paragraph("CORE SKILLS", st["section"]))
    for g in groups[:max_skills]:
        story.append(Paragraph(
            f"<b>{esc(g['name'])}:</b> {esc(clean(g['items']))}", st["skill"]))

    # Experience stays in reverse-chronological order. Only the bullets within
    # each role are reordered, because shuffling employers reads as dishonest.
    story.append(Paragraph("EXPERIENCE", st["section"]))
    for job in cv["experience"]:
        bullets = sorted(job["bullets"],
                         key=lambda b: -relevance(b.get("tags"), weights))
        keep = bullets[:max_bullets]
        # Restore the CV's own order among those kept, so the narrative reads
        # naturally rather than by descending keyword score.
        original = {id(b): i for i, b in enumerate(job["bullets"])}
        keep.sort(key=lambda b: original[id(b)])

        block = [
            Paragraph(f"{esc(job['company'])} | {esc(job['role'])}", st["role"]),
            Paragraph(esc(job["dates"]), st["meta"]),
            ListFlowable(
                [ListItem(Paragraph(esc(clean(b["text"])), st["bullet"]),
                          leftIndent=9) for b in keep],
                bulletType="bullet", start="circle", leftIndent=9,
                bulletFontSize=5, spaceBefore=0, spaceAfter=0,
            ),
        ]
        story.append(KeepTogether(block))

    apps = sorted(cv["published_apps"],
                  key=lambda a: -relevance(a.get("tags"), weights))[:4]
    if apps:
        story.append(Paragraph("PUBLISHED APPS", st["section"]))
        story.append(ListFlowable(
            [ListItem(Paragraph(esc(clean(a["text"])), st["bullet"]), leftIndent=9)
             for a in apps],
            bulletType="bullet", start="circle", leftIndent=9, bulletFontSize=5))

    projects = sorted(cv["projects"],
                      key=lambda p: -relevance(p.get("tags"), weights))[:2]
    if projects:
        story.append(Paragraph("SELECTED PROJECTS", st["section"]))
        for p in projects:
            story.append(Paragraph(
                f"<b>{esc(p['name'])}:</b> {esc(clean(p['detail']))}", st["skill"]))

    story.append(Paragraph("EDUCATION", st["section"]))
    for line in cv["education"]:
        story.append(Paragraph(esc(line), st["skill"]))

    lead = sorted(cv["leadership"],
                  key=lambda l: -relevance(l.get("tags"), weights))
    if weights.get("leadership", 0) > 0 and lead:
        story.append(Paragraph("LEADERSHIP & MENTORSHIP", st["section"]))
        for item in lead[:2]:
            story.append(Paragraph(esc(clean(item["text"])), st["skill"]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"{h['name']} CV", author=h["name"],
    ).build(story)
    return out_path


def render_cover_letter(text: str, job: dict, profile_header: dict,
                        out_path: Path) -> Path:
    """Render a cover letter as its own PDF.

    Some forms offer a cover letter file upload rather than a text box.
    Attaching the CV there, which is what happened before this existed, looks
    careless to whoever opens it.
    """
    st = build_styles()
    body = ParagraphStyle("cl_body", parent=st["body"], fontSize=10,
                          leading=14, spaceAfter=8)
    story = [
        Paragraph(esc(profile_header["name"]), st["name"]),
        Paragraph(" | ".join([
            esc(profile_header["email"]), esc(profile_header["phone"]),
            esc(profile_header["location"]), esc(profile_header["portfolio"]),
        ]), st["contact"]),
        HRFlowable(width="100%", thickness=0.6,
                   color=colors.HexColor("#999999"), spaceAfter=10),
        Paragraph(esc(f"Application: {job.get('title','')} at "
                      f"{job.get('company','')}"), st["role"]),
        Spacer(1, 8),
        Paragraph("Hello,", body),
    ]
    for para in [p for p in (text or "").split("\n") if p.strip()]:
        story.append(Paragraph(esc(clean(para)), body))
    story += [
        Spacer(1, 6),
        Paragraph("Best regards,", body),
        Paragraph(esc(profile_header["name"]), body),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Cover Letter - {job.get('company','')}",
        author=profile_header["name"],
    ).build(story)
    return out_path


def tailor_for_job(job: dict, out_dir: Path | None = None) -> Path:
    """Build a CV tailored to one posting. Returns the PDF path."""
    cv = load_cv()
    weights = derive_tags(job.get("description", ""), job.get("title", ""))

    # Mirror the posting's own title when it names a stack we genuinely have.
    title = cv["header"]["default_subtitle"]
    stack = primary_stack(weights, cv["summary"].get("emphasis", {}))
    # Optional per-stack headline in cv.yaml: header.title_variants.<stack>
    title = cv["header"].get("title_variants", {}).get(stack, title)

    out_dir = out_dir or (ROOT / "data" / "cv" / "tailored")
    slug = re.sub(r"[^a-z0-9]+", "-",
                  f"{job.get('company','job')}-{job.get('title','')}".lower())[:60]
    return render(cv, weights, out_dir / f"{slug}.pdf", role_title=title)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", type=int, help="tailor for a stored posting")
    ap.add_argument("--out", type=Path, help="output path")
    ap.add_argument("--base", action="store_true",
                    help="render the untailored base CV")
    args = ap.parse_args()

    if args.base or not args.job_id:
        cv = load_cv()
        out = args.out or (ROOT / "data" / "cv" / "generated-base.pdf")
        render(cv, {}, out)
        print(f"wrote {out} ({out.stat().st_size // 1024} kb)")
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import db
    row = db.connect().execute(
        "SELECT * FROM jobs WHERE id = ?", (args.job_id,)
    ).fetchone()
    if not row:
        print(f"no job with id {args.job_id}")
        return 1
    job = dict(row)
    weights = derive_tags(job["description"], job["title"])
    out = tailor_for_job(job, args.out.parent if args.out else None)
    print(f"wrote {out} ({out.stat().st_size // 1024} kb)")
    print(f"tags: {dict(sorted(weights.items(), key=lambda kv: -kv[1]))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
