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

API_KEY = (os.environ.get("LUMA_API_KEY") or "").strip()
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

STATUS_MAP = {"approved": "approved", "going": "approved", "registered": "approved", "checked_in": "approved",
              "pending_approval": "pending", "pending": "pending", "requested": "pending",
              "waitlist": "waitlist", "waitlisted": "waitlist",
              "declined": "declined", "rejected": "declined", "not_going": "declined"}
PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com",
                    "live.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
                    "rediffmail.com", "aol.com", "zoho.com", "ymail.com"}
# Questions whose answers identify a person are never used for analytics.
SKIP_WORDS = ("name", "email", "phone", "mobile", "linkedin", "url", "website", "twitter",
              "github", "x handle", "instagram", "address", "teammate", "colleague")
MAX_DISTINCT_ANSWERS = 20
CITY_ALIASES = {"bangalore": "Bengaluru", "bengaluru": "Bengaluru", "blr": "Bengaluru",
                "bombay": "Mumbai", "mumbai": "Mumbai", "new delhi": "Delhi", "delhi": "Delhi",
                "gurgaon": "Gurugram", "gurugram": "Gurugram", "noida": "Noida", "madras": "Chennai",
                "hyderabad": "Hyderabad", "hyd": "Hyderabad", "pune": "Pune", "calcutta": "Kolkata",
                "usa": "United States", "us": "United States", "united states of america": "United States",
                "uk": "United Kingdom", "uae": "UAE", "india": "India", "singapore": "Singapore"}
COUNTRIES = {"India", "United States", "United Kingdom", "UAE", "Singapore", "Canada", "Germany", "Australia"}


def get(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-luma-api-key": API_KEY, "authorization": f"Bearer {API_KEY}",
                                               "accept": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            try:
                e.luma_message = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                e.luma_message = ""
            raise
    raise RuntimeError(f"Luma kept refusing {path}")


EVENT_PARAM = None


def get_with_event(path, event_id, extra):
    """Luma has used both event_api_id and event_id for this parameter; use whichever it accepts."""
    global EVENT_PARAM
    names = [EVENT_PARAM] if EVENT_PARAM else ["event_api_id", "event_id", "api_id"]
    last = None
    for name in names:
        try:
            result = get(path, {name: event_id, **extra})
            EVENT_PARAM = name
            return result
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 400:
                raise
    raise last


def guests(event_id):
    cursor = None
    while True:
        params = {"pagination_limit": 100}
        if cursor:
            params["pagination_cursor"] = cursor
        page = get_with_event("/v1/event/get-guests", event_id, params)
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


def _clean(x):
    if isinstance(x, dict):
        x = x.get("label") or x.get("value") or x.get("name") or ""
    x = " ".join(str(x).split())[:60]
    return x


def answer_values(a):
    v = a.get("answer", a.get("value", a.get("answers")))
    if v is None or v == "" or isinstance(v, bool):
        return []
    if isinstance(v, dict):
        return []  # handled separately (company / job title)
    if isinstance(v, list):
        return [c for c in (_clean(x) for x in v) if c]
    c = _clean(v)
    return [c] if c else []


def question_label(a):
    return " ".join(str(a.get("label") or a.get("question") or a.get("question_label") or "").split())



# ---------- How each registration question becomes a chart ----------
SKIP_PHRASES = ("linkedin", "website", "url", "gst", "address", "consent", "certification", "if you chose other",
                "please specify", "email", "phone", "mobile", "twitter", "github", "instagram", "teammate",
                "colleague", "full name", "first name", "last name")
SKIP_EXACT = {"company", "company name", "name", "organisation", "organization", "startup name"}
RENAME = {
    "industry": "Sector",
    "number of employees": "Team size",
    "where does ai sit in your product journey today?": "Where AI sits in the product",
    "customer segment": "Customer segment",
    "market geography": "Markets served",
    "would you like to meet investors as part of vcbridge?": "Wants to meet investors (VCBridge)",
    "would you like to be considered for the showcase on 4 march?": "Wants to be considered for The Showcase",
}
DIM_ORDER = ["Annual revenue (ARR)", "ARR band", "Funding raised", "Sector", "Team size", "Company age",
             "Where AI sits in the product", "Customer segment", "Average contract value (ACV)", "Markets served",
             "City", "Country", "Wants to meet investors (VCBridge)", "Wants to be considered for The Showcase",
             "Seniority (from job title)", "Work or personal email"]
INR_PER_USD = 85


def parse_money(text):
    """Turn '$1.5M', '250000', '1-5M', '50 lakh', 'Pre-revenue' into a USD number (lower end of a range)."""
    t = text.lower().replace(",", "").replace("usd", "").replace("$", "").strip()
    if not t:
        return None
    if any(w in t for w in ("pre-revenue", "pre revenue", "no revenue", "none", "nil", "bootstrapped", "not raised", "zero")) or t in ("0", "na", "n/a", "-"):
        return 0.0
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|m|mn|mm|million|b|bn|billion|cr|crore|crores|l|lakh|lakhs|lac)?\b", t)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or ""
    if not unit:  # a range like '1-5m' puts the unit after the second number
        tail = re.search(r"\d\s*(k|m|mn|million|b|bn|cr|crore|lakh|lac)\b", t)
        unit = tail.group(1) if tail else ""
    mult = {"k": 1e3, "m": 1e6, "mn": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9,
            "cr": 1e7 / INR_PER_USD, "crore": 1e7 / INR_PER_USD, "crores": 1e7 / INR_PER_USD,
            "l": 1e5 / INR_PER_USD, "lakh": 1e5 / INR_PER_USD, "lakhs": 1e5 / INR_PER_USD, "lac": 1e5 / INR_PER_USD}
    return n * mult.get(unit, 1)


def bucket(v, edges, labels):
    for edge, label in zip(edges, labels):
        if v < edge:
            return label
    return labels[-1]


def parse_number(text):
    import re
    m = re.search(r"\d+", text.replace(",", ""))
    return int(m.group()) if m else None


def place(text):
    return CITY_ALIASES.get(text.lower().strip(" .,"), text.strip(" .,").title())


def transform(label, values, year):
    """Return (chart name, answer) pairs for one question, or [] to leave it out."""
    l = label.lower().strip()
    if not l or l in SKIP_EXACT or any(p in l for p in SKIP_PHRASES):
        return []
    out = []
    for v in values:
        if "arr" in l.split() or "(arr" in l or "annual revenue" in l:
            n = parse_money(v)
            if n is None:
                out.append(("Annual revenue (ARR)", "Not clear from answer"))
                continue
            out.append(("Annual revenue (ARR)", bucket(n, [1, 1e6, 5e6, 20e6, float("inf")],
                                                       ["Pre-revenue", "Under $1M", "$1–5M", "$5–20M", "$20M+"])))
            out.append(("ARR band", "$0–5M" if n < 5e6 else "$5M+"))
        elif "acv" in l or "contract value" in l:
            n = parse_money(v)
            out.append(("Average contract value (ACV)", "Not clear from answer" if n is None else
                        bucket(n, [1e3, 1e4, 5e4, 1e5, float("inf")], ["Under $1K", "$1–10K", "$10–50K", "$50–100K", "$100K+"])))
        elif "funding" in l or "raised" in l:
            n = parse_money(v)
            out.append(("Funding raised", "Not clear from answer" if n is None else
                        bucket(n, [1, 1e6, 5e6, 20e6, float("inf")],
                               ["Bootstrapped / none", "Under $1M", "$1–5M", "$5–20M", "$20M+"])))
        elif "employees" in l or "team size" in l:
            n = parse_number(v)
            out.append(("Team size", v if n is None else
                        bucket(n, [11, 51, 201, 501, float("inf")], ["1–10", "11–50", "51–200", "201–500", "500+"])))
        elif "founded" in l:
            n = parse_number(v)
            if n and 1900 < n <= year:
                age = year - n
                out.append(("Company age", bucket(age, [2, 5, 10, float("inf")],
                                                  ["Under 2 years", "2–4 years", "5–9 years", "10+ years"])))
        elif "location" in l or "city" in l or "country" in l or "based" in l:
            parts = [x for x in (y.strip() for y in v.replace("/", ",").split(",")) if x]
            if len(parts) >= 2:
                out.append(("City", place(parts[0])))
                out.append(("Country", place(parts[-1])))
            elif parts:
                p = place(parts[0])
                out.append(("Country" if p in COUNTRIES else "City", p))
        else:
            out.append((RENAME.get(l, label), v[:60]))
    return out


SENIORITY = [
    ("Founder / Co-founder", ("founder", "co-founder", "cofounder")),
    ("CEO / MD", ("ceo", "chief executive", "managing director", " md")),
    ("CTO / Tech leader", ("cto", "chief technology", "vp engineering", "head of engineering", "engineering head")),
    ("Other CXO", ("cxo", "coo", "cfo", "cpo", "cmo", "chief")),
    ("VP / Director / Head", ("vp", "vice president", "director", "head")),
    ("Investor / Partner", ("investor", "partner", "principal", "associate", "angel", "venture")),
    ("Researcher / Academic", ("research", "professor", "faculty", "phd", "scientist", "scholar")),
    ("Engineer / Manager", ("engineer", "developer", "manager", "lead", "architect")),
]


def seniority(title):
    t = " " + title.lower()
    for bucket, words in SENIORITY:
        if any(w in t for w in words):
            return bucket
    return "Other roles"


def scramble(guest_id):
    return hashlib.sha256(f"one27:{guest_id}".encode()).hexdigest()[:16]


SLUGS = {"one27-founders": "founders", "one27-tech": "tech", "one27-investors": "investors"}


def discover_events():
    """Look up the three events on the calendar by their Luma link, so the ids are always right."""
    found, cursor, pages = {}, None, 0
    try:
        while pages < 30:
            params = {"pagination_limit": 50}
            if cursor:
                params["pagination_cursor"] = cursor
            page = get("/v1/calendar/list-events", params)
            pages += 1
            for entry in page.get("entries", []):
                ev = entry.get("event", entry)
                slug = str(ev.get("url", "")).rstrip("/").split("/")[-1]
                if slug in SLUGS:
                    found[SLUGS[slug]] = ev.get("api_id") or ev.get("id")
                    print(f"Found on calendar: {ev.get('name')} -> {found[SLUGS[slug]]}")
            if not page.get("has_more") or len(found) == 3:
                break
            cursor = page.get("next_cursor")
    except urllib.error.HTTPError as e:
        print(f"Note: couldn't list calendar events (HTTP {e.code}): {getattr(e, 'luma_message', '')}")
    for aud, eid in found.items():
        if eid:
            EVENTS[aud] = eid
    missing = [a for a in SLUGS.values() if a not in found]
    if missing:
        print(f"Note: not found on this calendar by link, using saved ids for: {', '.join(missing)}")


def main():
    if not API_KEY:
        sys.exit("LUMA_API_KEY is not set.")
    print(f"Key check: {len(API_KEY)} characters, looks like a full Luma key: {API_KEY.startswith(('secret-', 'luma_sk_')) and len(API_KEY) > 25}, "
          f"only plain characters: {API_KEY.isascii() and ' ' not in API_KEY}")
    try:
        me = get("/v1/user/get-self", {})
        print("Luma accepted the key. Signed in as:", (me.get("user") or me).get("name", "(name hidden)"))
    except urllib.error.HTTPError as e:
        sys.exit(f"STOP: Luma rejected the key itself (HTTP {e.code}). The key is wrong, deleted, "
                 "or the calendar has no active Luma Plus plan.")
    discover_events()
    for aud, event_id in EVENTS.items():
        try:
            get_with_event("/v1/event/get-guests", event_id, {"pagination_limit": 1})
            print(f"Access OK: {aud} event ({event_id}), using '{EVENT_PARAM}'")
        except urllib.error.HTTPError as e:
            sys.exit(f"STOP: can't read guests for the {aud} event {event_id} (HTTP {e.code}). "
                     f"Luma said: {getattr(e, 'luma_message', '')}")
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
    qinfo = {aud: defaultdict(Counter) for aud in EVENTS}
    pending_ages, checked_in = [], Counter()
    new_state = {}

    for aud, event_id in EVENTS.items():
        raw_status, raw_ticket, seen = Counter(), Counter(), 0
        for g in guests(event_id):
            seen += 1
            raw = str(g.get("approval_status") or g.get("status") or "none").lower()
            raw_status[raw] += 1
            raw_ticket[(g.get("event_ticket") or {}).get("name") or "none"] += 1
            status = STATUS_MAP.get(raw)
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
                    label = question_label(a)
                    raw = a.get("answer", a.get("value"))
                    if isinstance(raw, dict):
                        title = raw.get("job_title") or raw.get("title") or ""
                        if title:
                            answers[aud]["Seniority (from job title)"][seniority(str(title))] += 1
                        continue
                    if label and "title" in label.lower() and "job" in label.lower():
                        for v in answer_values(a):
                            answers[aud]["Seniority (from job title)"][seniority(v)] += 1
                        continue
                    for dim, v in transform(label, answer_values(a), now.year):
                        answers[aud][dim][v] += 1
            for a in g.get("registration_answers") or []:
                qinfo[aud][question_label(a) or "(no label)"][type(a.get("answer", a.get("value"))).__name__] += 1
        print(f"{aud}: {seen} guests read. Statuses: {dict(raw_status)}. Tickets: {dict(raw_ticket)}")

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
        print(f"{aud} registration questions (label: answer types):")
        for label, types in qinfo[aud].items():
            print(f"   - {label}: {dict(types)}")
        for label, counts in answers[aud].items():
            total = sum(counts.values())
            if not total:
                continue
            if len(counts) <= MAX_DISTINCT_ANSWERS:
                dims[label] = counts.most_common()
            else:
                # Typed answers (e.g. city): keep if the top answers cover most people
                top = counts.most_common(12)
                covered = sum(n for _, n in top)
                if covered / total >= 0.6:
                    dims[label] = top + [("Other answers", total - covered)]
                else:
                    print(f"   (skipped '{label}': free-text answers, too varied to chart)")
        if email_type[aud]:
            dims["Work or personal email"] = email_type[aud].most_common()
        order = {k: i for i, k in enumerate(DIM_ORDER)}
        profile[aud] = dict(sorted(dims.items(), key=lambda kv: order.get(kv[0], 50)))

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
