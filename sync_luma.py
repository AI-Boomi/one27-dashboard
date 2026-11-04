"""
AIBoomi ONE '27: pulls registrations from Luma and writes dashboard.json.

Only counts leave this script. Guest names, emails and phone numbers are never
written anywhere. state.json keeps a scrambled id per guest (not reversible)
so the dashboard can tell when an application was approved or declined.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

API_KEY = os.environ.get("LUMA_API_KEY")
BASE = "https://public-api.luma.com"
IST = timezone(timedelta(hours=5, minutes=30))

EVENTS = {
    "founders": "evt-rTttpqVrd9fvFz1",
    "tech": "evt-wNI2JgOwa6T51Y1",
    "investors": "evt-P9Zsm7ZXZXKnUqp",
}
PASSES = {
    "founders": ["Builder", "Builder+", "Navigator"],
    "tech": ["Architect", "Scholar", "Navigator"],
    "investors": ["Backer", "Backer+"],
}
TIERS = ["Super Early Bird", "Early Bird"]
BOOTCAMP_CAP = 80
REVIEW_DEADLINE = "2027-03-01T00:00:00+05:30"

STATUS_MAP = {"approved": "approved", "pending_approval": "pending",
              "waitlist": "waitlist", "declined": "declined"}
PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com",
                    "live.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
                    "rediffmail.com", "aol.com", "zoho.com", "ymail.com"}
# Questions whose answers identify a person are never used for analytics.
SKIP_WORDS = ("name", "email", "phone", "mobile", "linkedin", "url", "website", "twitter",
              "github", "x handle", "instagram", "address", "teammate", "colleague")
MAX_DISTINCT_ANSWERS = 20


def get(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-luma-api-key": API_KEY, "accept": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"Luma kept refusing {path}")


def guests(event_id):
    cursor = None
    while True:
        params = {"event_id": event_id, "pagination_limit": 100}
        if cursor:
            params["pagination_cursor"] = cursor
        page = get("/v1/event/get-guests", params)
        for entry in page.get("entries", []):
            yield entry.get("guest", entry)
        if not page.get("has_more"):
            return
        cursor = page.get("next_cursor")
        time.sleep(0.2)


def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(IST)
    except ValueError:
        return None


def split_ticket(name):
    name = (name or "Unknown").strip()
    for sep in ("·", "|", " - "):
        if sep in name:
            p, t = name.split(sep, 1)
            return p.strip(), t.strip()
    return name, ""


def answer_values(a):
    v = a.get("answer", a.get("value"))
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x).strip()[:60] for x in v if str(x).strip()]
    return [str(v).strip()[:60]]


def scramble(guest_id):
    return hashlib.sha256(f"one27:{guest_id}".encode()).hexdigest()[:16]


def main():
    if not API_KEY:
        sys.exit("LUMA_API_KEY is not set.")
    now = datetime.now(IST)
    today = now.date().isoformat()

    try:
        with open("state.json") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    first_run = not state

    passes = {}
    for aud, names in PASSES.items():
        for p in names:
            for t in TIERS:
                passes[(aud, p, t)] = Counter()
    headcount = Counter()
    apps_by_day = defaultdict(Counter)
    answers = {aud: defaultdict(Counter) for aud in EVENTS}
    email_type = {aud: Counter() for aud in EVENTS}
    pending_ages, checked_in = [], Counter()
    new_state = {}

    for aud, event_id in EVENTS.items():
        for g in guests(event_id):
            status = STATUS_MAP.get(g.get("approval_status"))
            if not status:
                continue
            pass_name, tier = split_ticket((g.get("event_ticket") or {}).get("name"))
            key = (aud, pass_name, tier)
            passes.setdefault(key, Counter())
            passes[key][status] += 1

            reg = parse_time(g.get("registered_at") or g.get("created_at"))
            reg_day = reg.date().isoformat() if reg else today
            apps_by_day[reg_day][aud] += 1

            gid = scramble(g.get("api_id") or g.get("id") or json.dumps(g, sort_keys=True)[:200])
            old = state.get(gid, {})
            decided = old.get("d")
            if status in ("approved", "declined") and old.get("s") != status:
                decided = None if first_run else today
            if status in ("pending", "waitlist"):
                decided = None
            new_state[gid] = {"s": status, "r": reg_day, "d": decided}

            if status == "pending" and reg:
                pending_ages.append((now - reg).days)
            if g.get("checked_in_at"):
                checked_in[aud] += 1

            if status == "approved":
                people = 1
                if pass_name == "Backer+":
                    for a in g.get("registration_answers") or []:
                        label = (a.get("label") or "").lower()
                        if ("teammate" in label or "colleague" in label) and answer_values(a):
                            people += 1
                    people = min(people, 3)
                headcount[key] += people

                email = (g.get("user_email") or g.get("email") or "").lower()
                if "@" in email:
                    domain = email.split("@", 1)[1]
                    email_type[aud]["Personal email" if domain in PERSONAL_DOMAINS else "Work email"] += 1
                for a in g.get("registration_answers") or []:
                    label = (a.get("label") or "").strip()
                    if not label or any(w in label.lower() for w in SKIP_WORDS):
                        continue
                    for v in answer_values(a):
                        answers[aud][label][v] += 1

    # Daily series from the first application to today
    days = sorted(apps_by_day)
    start = datetime.fromisoformat(days[0]).date() if days else now.date()
    decisions = defaultdict(Counter)
    decision_times = []
    for s in new_state.values():
        if s.get("d"):
            decisions[s["d"]]["approved" if s["s"] == "approved" else "declined"] += 1
            try:
                decision_times.append((datetime.fromisoformat(s["d"]) - datetime.fromisoformat(s["r"])).days)
            except ValueError:
                pass
    daily, d = [], start
    while d <= now.date():
        k = d.isoformat()
        daily.append({"date": k, "founders": apps_by_day[k]["founders"], "tech": apps_by_day[k]["tech"],
                      "investors": apps_by_day[k]["investors"],
                      "approved": decisions[k]["approved"], "declined": decisions[k]["declined"]})
        d += timedelta(days=1)

    pass_rows = []
    order = {a: i for i, a in enumerate(EVENTS)}
    for (aud, p, t), c in sorted(passes.items(), key=lambda x: (order[x[0][0]], TIERS.index(x[0][2]) if x[0][2] in TIERS else 9,
                                                                 PASSES[x[0][0]].index(x[0][1]) if x[0][1] in PASSES[x[0][0]] else 9)):
        applied = sum(c.values())
        row = {"audience": aud, "pass": p, "tier": t, "applied": applied,
               "approved": c["approved"], "pending": c["pending"],
               "waitlist": c["waitlist"], "declined": c["declined"]}
        if p == "Backer+":
            row["headcount_approved"] = headcount[(aud, p, t)]
        if applied == 0:
            row["upcoming"] = True
        pass_rows.append(row)

    profile = {}
    for aud in EVENTS:
        dims = {}
        for label, counts in answers[aud].items():
            if 2 <= len(counts) <= MAX_DISTINCT_ANSWERS:
                dims[label] = counts.most_common()
        if email_type[aud]:
            dims["Work or personal email"] = email_type[aud].most_common()
        profile[aud] = dims

    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "event": {"name": "AIBoomi ONE ’27", "bootcamp_cap": BOOTCAMP_CAP, "review_deadline": REVIEW_DEADLINE},
        "passes": pass_rows,
        "daily": daily,
        "profile": profile,
        "checked_in": dict(checked_in),
        "review": {
            "oldest_pending_days": max(pending_ages) if pending_ages else None,
            "avg_decision_days": round(sum(decision_times) / len(decision_times), 1) if decision_times else None,
        },
    }
    with open("dashboard.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open("state.json", "w") as f:
        json.dump(new_state, f, separators=(",", ":"))

    total = sum(r["applied"] for r in pass_rows)
    approved = sum(r["approved"] for r in pass_rows)
    print(f"Done: {total} applications, {approved} approved. Wrote dashboard.json")


if __name__ == "__main__":
    main()
