"""Regression tests for the pre-LLM filter.

Every case here comes from a real posting that the filter got wrong. These
guard the failure mode that matters: silently rejecting a good role. A bad
pass costs one Gemini call; a bad reject costs an opportunity you never see.

Run:  .venv/bin/python tests/test_filters.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from normalize import (  # noqa: E402
    detect_eligibility, detect_region, evaluate, parse_salary,
    region_allowed, stack_hits, title_verdict,
)

import copy  # noqa: E402

import normalize  # noqa: E402

# These cases come from real postings, so pin the config they depend on rather
# than whatever a user has put in config/criteria.yaml or profile.yaml. The
# shipped defaults allow every region; these tests need a deny list, a stack
# and a home region to exist.
_cfg = copy.deepcopy(normalize.criteria())
_cfg["regions"]["deny"] = ["india", "pakistan", "bangladesh", "sri lanka"]
_cfg["stack"]["core"] = ["flutter", "dart", "react native", "swift", "kotlin"]
_cfg["stack"]["secondary"] = ["go", "golang", "python", "typescript"]
_cfg["stack"]["disqualifying"] = ["unity", "unreal", "cobol"]
normalize.criteria = lambda: _cfg
normalize._profile = lambda: {}
normalize._home_markers = lambda: ["nigeria", "lagos"]

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {name}")
    else:
        print(f"  ok   {name}")


# ---------------------------------------------------------------------------
# Bug 1: "unpaid" in a benefits section rejected a Senior Flutter role at Tide.
# ---------------------------------------------------------------------------
TIDE_BENEFITS = (
    "We offer 25 days holiday, adoption leave to support your family journey. "
    "Sabbatical - Extended unpaid and paid leave options after completing "
    "milestone years with us. Private healthcare."
)

print("\nbug 1: unpaid-leave false positive")
verdict = evaluate({
    "title": "Senior Software Engineer, Flutter",
    "description": "Build our Flutter app. " + TIDE_BENEFITS,
    "location": "London or Remote",
    "is_remote": 1,
})
check("flutter role with unpaid sabbatical is not rejected",
      verdict["filter_status"] != "rejected", True)

check("a genuinely unpaid role is still rejected",
      evaluate({
          "title": "Senior Mobile Engineer",
          "description": "This is an unpaid position for portfolio experience.",
          "location": "Remote",
      })["filter_status"], "rejected")


# ---------------------------------------------------------------------------
# Bug 2: "$250 billion in transaction value" parsed as a $250/year salary,
# rejecting three Moniepoint Mobile Architect roles below the floor.
# ---------------------------------------------------------------------------
print("\nbug 2: business metrics parsed as salary")
check("'$250 billion in transaction value' yields no salary",
      parse_salary("We process $250 billion in digital payment transaction "
                   "value annually. About the role: Mobile Architect."), {})

check("'$2.5 million users' yields no salary",
      parse_salary("Serving $2.5 million customers across Africa."), {})

moniepoint = evaluate({
    "title": "Mobile Architect",
    "description": ("Moniepoint processes $250 billion in digital payment "
                    "transaction value annually. As Mobile Architect you will "
                    "lead our Flutter and Android platform work in Lagos."),
    "location": "Lagos, Nigeria",
    "is_remote": 0,
})
check("Moniepoint Mobile Architect is not rejected",
      moniepoint["filter_status"] != "rejected", True)
check("Moniepoint resolves to home-region eligibility",
      moniepoint["eligibility"], "home_region")

real = parse_salary("Compensation: $150,000 - $190,000 per year plus equity.")
check("a real salary range still parses", real.get("salary_max"), 190000.0)
check("a real salary keeps its currency", real.get("salary_currency"), "USD")
check("hourly contract rate survives the plausibility floor",
      parse_salary("Rate: $65 per hour, 40 hours a week.").get("salary_max"),
      65.0)


# ---------------------------------------------------------------------------
# Bug 3: phrase-only title list dropped 50 real mobile roles.
# ---------------------------------------------------------------------------
print("\nbug 3: title matching too narrow")
for title in [
    "Senior Software Engineer - Android",
    "Senior Software Engineer, iOS",
    "Software Engineer, Mobile",
    "Staff Software Engineer, Mobile (App Infrastructure)",
    "Senior Software Engineer, Android SDUI",
    "Mobile Engineering Manager",
    "Senior Flutter Developer",
    "React Native Engineer",
]:
    check(f"accepts {title!r}", title_verdict(title)[0], True)

for title in [
    "Mobile Product Designer",
    "Product Manager, Mobile Apps",
    "Creative Strategist, Mobile",
    "Junior Android Developer",
    "Android Engineering Intern",
    "Mobile Marketing Lead",
    "Unity Game Developer",
]:
    check(f"rejects {title!r}", title_verdict(title)[0], False)


# ---------------------------------------------------------------------------
# Bug 4: a passing mention of India in a company blurb rejected a
# Lithuania-based Senior Flutter role at Tide. Location is authoritative.
# ---------------------------------------------------------------------------
print("\nbug 4: denylist matched a country the job is not in")
TIDE_BLURB = (
    "Tide is a UK fintech. We have offices in London, Sofia, Belgrade and "
    "Hyderabad, India, and our engineering teams span several countries. "
    "We are hiring a Senior Engineer, Flutter to join the team."
)
check("Lithuania location wins over an India mention in the body",
      detect_region("Lithuania", TIDE_BLURB), "lithuania")
check("Tide Flutter role is not rejected",
      evaluate({
          "title": "Senior Engineer, Flutter",
          "description": TIDE_BLURB + " You will build with Flutter, Dart and BLoC. "
                         "This is a great opportunity.",
          "location": "Lithuania", "is_remote": 0,
      })["filter_status"] != "rejected", True)
check("a job actually located in India is still denied",
      detect_region("Hyderabad, India", TIDE_BLURB), "india")
check("empty location still falls back to the description",
      detect_region("", "This role is based in Bangalore, India."), "india")


# ---------------------------------------------------------------------------
# Bug 5: substring matching found 'unity' inside 'opportunity'.
# ---------------------------------------------------------------------------
print("\nbug 5: stack terms matched inside longer words")
check("'opportunity' does not register as Unity",
      stack_hits("This is a great opportunity to build mobile apps.")["disqualifying"],
      [])
check("'going' does not register as Go",
      stack_hits("We are going to scale the app.")["secondary"], [])
check("a real Unity requirement is still caught",
      stack_hits("Experience with Unity and C# required.")["disqualifying"], ["unity"])
check("a real Go requirement is still caught",
      "go" in stack_hits("Backend services written in Go.")["secondary"], True)
check("Flutter is still detected as core",
      "flutter" in stack_hits("Build with Flutter and Dart.")["core"], True)


# ---------------------------------------------------------------------------
# Standing guarantees. These must never regress.
# ---------------------------------------------------------------------------
print("\nstanding guarantees")
check("India is denied", region_allowed(detect_region("Bangalore, India", "")), False)
check("Pakistan is denied", region_allowed(detect_region("Karachi, Pakistan", "")), False)
check("Indiana is not mistaken for India",
      region_allowed(detect_region("Indianapolis, Indiana", "")), True)
check("Nigeria is allowed", region_allowed(detect_region("Lagos, Nigeria", "")), True)
check("Remote worldwide is allowed",
      region_allowed(detect_region("Remote - Worldwide", "")), True)

check("US-only authorization is rejected",
      detect_eligibility("Remote (US)",
                         "You must be authorized to work in the United States.", True),
      "us_only")
check("security clearance is rejected",
      detect_eligibility("Remote", "Active security clearance required.", True),
      "citizen_only")
check("work-from-anywhere is global_remote",
      detect_eligibility("Remote", "You can work from anywhere in the world.", True),
      "global_remote")
check("employer of record is eor",
      detect_eligibility("Remote", "We hire via an employer of record.", True), "eor")
check("visa sponsorship is sponsor",
      detect_eligibility("Berlin", "Visa sponsorship and relocation package offered.", False),
      "sponsor")

check("a US-only role is rejected end to end",
      evaluate({
          "title": "Senior iOS Engineer",
          "description": "Must be authorized to work in the United States. No visa sponsorship.",
          "location": "Remote (US)", "is_remote": 1,
      })["filter_status"], "rejected")

check("an India-based role is rejected end to end",
      evaluate({
          "title": "Senior Flutter Developer",
          "description": "Join our Bangalore team building Flutter apps.",
          "location": "Bangalore, India", "is_remote": 0,
      })["filter_status"], "rejected")

check("unlisted salary does not reject",
      evaluate({
          "title": "Senior Mobile Engineer",
          "description": "Flutter and Kotlin Multiplatform. Work from anywhere.",
          "location": "Remote", "is_remote": 1,
      })["filter_status"], "passed")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED\n")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all filter tests passed")
