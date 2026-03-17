"""
seed_test_data.py — Generate synthetic emails and POST them to /ingest.

Produces a realistic corpus of ~30 emails across three fictional project
threads — enough to exercise the full seeding flow (LLM analysis, map-reduce,
retag, embedding, situation formation) without needing 30 days of real Outlook
data.

Usage:
    python seed_test_data.py                          # post to default URL
    python seed_test_data.py --url http://localhost:8001
    python seed_test_data.py --url https://squire.hexcaliper.com/page/api

The script posts directly to /ingest without Cloudflare headers, so run it
from a context that can reach the API without CF Access (local or tunnelled).
"""
import sys
import uuid
import requests
from datetime import datetime, timedelta, timezone

PAGE_API_URL = "https://squire.hexcaliper.com/page/api"


def _ts(days_ago: float, hour: int = 8) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


# ── Corpus ────────────────────────────────────────────────────────────────────
#
# Three threads with shared keywords and cross-references so the seed
# map-reduce has clear signal and the situation engine has material.
#
#   Thread A  — "Reactor A" coolant system issues   (keywords: reactor, PROJ-88)
#   Thread B  — Safety audit Zone 3                 (keywords: safety audit, zone 3, CAP)
#   Thread C  — Shift scheduling & staffing         (keywords: shift schedule, coverage)

EMAILS = [
    # ── Thread A: Reactor coolant ──────────────────────────────────────────
    {
        "title":   "SHIFT HIGHLIGHTS — Reactor A coolant pressure drop",
        "body":    "Reactor A coolant pressure dropped to 87% at 02:14. Ops team initiated manual stabilisation. Maintenance notified. PROJ-88 valve inspection moved up. Unit back to 94% by 05:00. Continue monitoring.\n\nAction: Engineering to review pressure trend data before 08:00 standup.",
        "author":  "Night Shift Lead <nightshift@ops.example.com>",
        "ts_days": 1,
        "hour":    6,
        "metadata": {"to": "ops-all@ops.example.com", "cc": ""},
    },
    {
        "title":   "Re: Reactor A coolant — PROJ-88 valve inspection results",
        "body":    "Inspection complete. Valve seat showing wear, within tolerance but recommend replacement at next planned outage. PROJ-88 parts order raised — ETA 5 days. Coolant pressure stable at 96% since 07:30.",
        "author":  "Maintenance Lead <maint@ops.example.com>",
        "ts_days": 1,
        "hour":    10,
        "metadata": {"to": "ops-engineering@ops.example.com", "cc": "ops-all@ops.example.com"},
    },
    {
        "title":   "SHIFT ACTIVITIES — Reactor A stable, PROJ-88 parts on order",
        "body":    "Reactor A running at 96% capacity. PROJ-88 valve replacement parts ordered, expected Friday. No further action required until parts arrive. Recommend scheduling replacement for Saturday nightshift window.",
        "author":  "Day Shift Lead <dayshift@ops.example.com>",
        "ts_days": 1,
        "hour":    18,
        "metadata": {"to": "ops-all@ops.example.com", "cc": ""},
    },
    {
        "title":   "PROJ-88 parts arrived — scheduling valve replacement",
        "body":    "Spares confirmed in stores (ref PROJ-88). Planning to execute replacement Saturday 22:00–02:00. Ops and maintenance teams confirmed. Please ensure Reactor A is in reduced-load mode by 21:30.",
        "author":  "Maintenance Planner <planner@ops.example.com>",
        "ts_days": 0,
        "hour":    9,
        "metadata": {"to": "ops-engineering@ops.example.com; ops-all@ops.example.com", "cc": ""},
    },
    {
        "title":   "Re: PROJ-88 Saturday window — crew confirmation needed",
        "body":    "Can ops confirm crew availability for Saturday night? Need at least two qualified operators on site during the PROJ-88 valve work. Please reply by Thursday noon.",
        "author":  "Operations Manager <opsmanager@ops.example.com>",
        "ts_days": 0,
        "hour":    11,
        "metadata": {"to": "ops-all@ops.example.com", "cc": ""},
    },
    {
        "title":   "Reactor A — coolant trending down again",
        "body":    "Overnight data shows coolant pressure trending down again (93%). May not hold until Saturday. Engineering reviewing whether to accelerate PROJ-88 timeline.",
        "author":  "Process Engineer <engineer@ops.example.com>",
        "ts_days": 0,
        "hour":    14,
        "metadata": {"to": "ops-engineering@ops.example.com", "cc": "opsmanager@ops.example.com"},
    },

    # ── Thread B: Safety audit Zone 3 ─────────────────────────────────────
    {
        "title":   "Safety audit Zone 3 — findings report",
        "body":    "Six findings from this week's safety audit of Zone 3. Two are high-priority: (1) fire suppression cabinet 3C blocked by equipment, (2) emergency lighting unit 3-EL-04 non-functional. Corrective Action Plan (CAP) required within 5 working days.",
        "author":  "Safety Officer <safety@ops.example.com>",
        "ts_days": 5,
        "hour":    9,
        "metadata": {"to": "ops-all@ops.example.com; opsmanager@ops.example.com", "cc": ""},
    },
    {
        "title":   "Re: Safety audit Zone 3 — CAP draft",
        "body":    "Draft CAP attached. High-priority items 1 and 2 scheduled for remediation by Friday. Remaining four items assigned to area supervisors with 30-day deadlines. Please review and sign off by tomorrow.",
        "author":  "Zone 3 Supervisor <zone3sup@ops.example.com>",
        "ts_days": 4,
        "hour":    14,
        "metadata": {"to": "safety@ops.example.com", "cc": "opsmanager@ops.example.com"},
    },
    {
        "title":   "Safety audit CAP — sign-off required",
        "body":    "CAP is awaiting your signature before submission to the safety regulator. Deadline is end of business today. Please prioritise.",
        "author":  "Safety Officer <safety@ops.example.com>",
        "ts_days": 3,
        "hour":    8,
        "metadata": {"to": "opsmanager@ops.example.com", "cc": ""},
    },
    {
        "title":   "SHIFT HIGHLIGHTS — Safety audit remediation items checked",
        "body":    "Zone 3 fire suppression cabinet cleared and emergency light 3-EL-04 replaced. Both high-priority CAP items closed. Remaining four items on schedule. No new safety findings during shift.",
        "author":  "Day Shift Lead <dayshift@ops.example.com>",
        "ts_days": 2,
        "hour":    18,
        "metadata": {"to": "ops-all@ops.example.com", "cc": "safety@ops.example.com"},
    },
    {
        "title":   "Safety audit Zone 3 — final CAP submission",
        "body":    "CAP submitted to regulator. Awaiting acknowledgement. Remaining four items tracked in internal action log. Next scheduled audit in 90 days.",
        "author":  "Safety Officer <safety@ops.example.com>",
        "ts_days": 2,
        "hour":    10,
        "metadata": {"to": "opsmanager@ops.example.com; ops-all@ops.example.com", "cc": ""},
    },
    {
        "title":   "Re: Safety audit — regulator acknowledgement received",
        "body":    "Regulator confirmed receipt of CAP. No further immediate action required. Reminder: four open action items still need close-out before next audit.",
        "author":  "Operations Manager <opsmanager@ops.example.com>",
        "ts_days": 1,
        "hour":    16,
        "metadata": {"to": "ops-all@ops.example.com", "cc": "safety@ops.example.com"},
    },

    # ── Thread C: Shift scheduling ─────────────────────────────────────────
    {
        "title":   "Shift coverage shortage — next two weeks",
        "body":    "Two operators on leave next week creates a coverage gap on the night shift. Requesting volunteers for overtime Saturday and Sunday nights. Please confirm availability with your supervisor by Wednesday.",
        "author":  "Scheduling Coordinator <scheduler@ops.example.com>",
        "ts_days": 6,
        "hour":    10,
        "metadata": {"to": "ops-all@ops.example.com", "cc": ""},
    },
    {
        "title":   "Re: Shift coverage — I can cover Saturday night",
        "body":    "Happy to cover Saturday night shift. Please add me to the schedule.",
        "author":  "Operator Jones <jones@ops.example.com>",
        "ts_days": 5,
        "hour":    11,
        "metadata": {"to": "scheduler@ops.example.com", "cc": ""},
    },
    {
        "title":   "Shift schedule update — week of March 22",
        "body":    "Updated shift schedule for next week attached. Saturday night coverage confirmed with Jones and Kowalski. Sunday night still one operator short — seeking one more volunteer.",
        "author":  "Scheduling Coordinator <scheduler@ops.example.com>",
        "ts_days": 4,
        "hour":    15,
        "metadata": {"to": "ops-all@ops.example.com", "cc": "opsmanager@ops.example.com"},
    },
    {
        "title":   "SHIFT HIGHLIGHTS — Handoff notes, Sunday crew short-staffed",
        "body":    "Sunday night ran with two operators instead of three. Managed without incident but workload was high. Recommend resolving the ongoing shift coverage gap before the PROJ-88 maintenance window adds further demand.",
        "author":  "Night Shift Lead <nightshift@ops.example.com>",
        "ts_days": 3,
        "hour":    6,
        "metadata": {"to": "ops-all@ops.example.com", "cc": "opsmanager@ops.example.com"},
    },
    {
        "title":   "Urgent: shift coverage gap persists — decision needed",
        "body":    "We are still one operator short for the coming weekend. Options: (1) approve overtime, (2) contract a temp operator, (3) defer the PROJ-88 maintenance window. Please advise by Thursday.",
        "author":  "Operations Manager <opsmanager@ops.example.com>",
        "ts_days": 2,
        "hour":    9,
        "metadata": {"to": "ops-all@ops.example.com", "cc": "planner@ops.example.com"},
    },

    # ── Misc / general ────────────────────────────────────────────────────
    {
        "title":   "Monthly ops review — agenda",
        "body":    "Monthly ops review is Thursday 14:00. Items: reactor performance, safety audit status, shift scheduling. Please come prepared with your area updates.",
        "author":  "Operations Manager <opsmanager@ops.example.com>",
        "ts_days": 7,
        "hour":    8,
        "metadata": {"to": "ops-all@ops.example.com", "cc": ""},
    },
    {
        "title":   "IT maintenance window — systems offline 23:00-01:00",
        "body":    "Planned IT maintenance tonight. SCADA historian and email may be intermittent. Operations not affected. Contact the IT helpdesk if issues persist after 01:00.",
        "author":  "IT Support <it@ops.example.com>",
        "ts_days": 4,
        "hour":    16,
        "metadata": {"to": "ops-all@ops.example.com", "cc": ""},
    },
    {
        "title":   "Training reminder — confined space entry refresher",
        "body":    "Mandatory confined space entry refresher is due for seven operators by end of month. Please book your session via the training portal.",
        "author":  "Training Coordinator <training@ops.example.com>",
        "ts_days": 8,
        "hour":    9,
        "metadata": {"to": "ops-all@ops.example.com", "cc": ""},
    },
]


def build_items() -> list[dict]:
    items = []
    for i, e in enumerate(EMAILS):
        ts = _ts(e["ts_days"], e.get("hour", 8))
        items.append({
            "source":    "outlook",
            "item_id":   f"seed-test-{i:03d}-{uuid.uuid4().hex[:6]}",
            "title":     e["title"],
            "body":      e["body"],
            "url":       "",
            "author":    e["author"],
            "timestamp": ts,
            "metadata":  e.get("metadata", {}),
        })
    return items


def post(items: list[dict], api_url: str) -> None:
    print(f"Posting {len(items)} test items to {api_url}/ingest...", flush=True)
    try:
        r = requests.post(
            f"{api_url}/ingest",
            json={"items": items},
            timeout=30,
        )
        r.raise_for_status()
        result = r.json()
        print(f"Accepted {result.get('received', '?')}, skipped {result.get('skipped', '?')}")
        print("Done. Wait a minute for LLM analysis to complete, then run ◈ Seed in the UI.")
    except requests.ConnectionError:
        sys.exit(f"ERROR: Could not reach {api_url} — is the API running?")
    except Exception as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    url = PAGE_API_URL
    if "--url" in sys.argv:
        idx = sys.argv.index("--url")
        url = sys.argv[idx + 1]

    items = build_items()
    post(items, url)
