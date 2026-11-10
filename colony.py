#!/usr/bin/env python3
"""Spider Colony core: watches, price log, deal rules, search queue.

Scouts (Claude scheduled runs) use the CLI; the web app (server.py) imports this module.

Deal rules
  * Only ALL-IN prices (--basis total: taxes and fees included) can trigger an alert.
    pre_tax/unknown prices are logged for reference but never alert.
  * Flights and cars: flat target price, as a TOTAL or PER DAY (hotels: per night). Per-day targets are multiplied by the
    trip's days first. Effective target = total * (1 + 0.10 * loosen_steps), steps 0..9. All targets are all-in.
  * Hotels: target if one is set, otherwise "30% below this source's own trailing-24h baseline".
  * Cheap airlines (Frontier, Spirit, Allegiant) are searched too but kept in their own "cheap airlines" section (detail tier=budget),
    never mixed into the normal best price. The profile setting hide_budget=1 drops them entirely. United Basic Economy is dropped unless the
    profile toggle include_united_be is on, in which case the pre-paid bag fee is added to the price.
  * Cars: with the profile setting require_unlimited_mileage ON (default is off), a price counts toward a deal ONLY if the source confirmed
    unlimited mileage (--mileage unlimited). Otherwise it is logged as 'unknown' basis and never alerts.
  * One alert per continuous run of deal prices per (watch, source).

CLI
  watch-add CATEGORY LABEL --params JSON [--target N] [--unit total|day]   watch-list   watch-set ID --status active|paused|done
  log SOURCE PRICE --watch ID [--basis total] [--url U] [--airline A] [--fare-class F] [--bag-fee N] [--currency USD] [--via V]
  fail SOURCE REASON [--watch ID]
  pending-searches            result REQ_ID SOURCE TITLE PRICE [--url U] [--detail JSON]   search-done REQ_ID
  profile                     status
"""
import argparse
import json
import math
import re
import sqlite3
from urllib.parse import urlencode
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "prices.db"
CONFIG = json.loads((ROOT / "config.json").read_text())
BUDGET_AIRLINES = ("frontier", "spirit", "allegiant")  # matched anywhere in the airline name, so "Southwest + Allegiant" is caught


def is_budget(text):
    t = (text or "").lower()
    return any(name in t for name in BUDGET_AIRLINES)
MAX_LOOSEN_STEPS = 9  # 9 x 10% = 90%
DEFAULT_BAG_FEE = 45.0  # United pre-paid carry-on/bag fee used when scout gives none
DEFAULT_PROFILE = {"name": "", "phone": "", "email": CONFIG.get("alert_email", ""), "sms_on": "1", "email_on": "1", "include_united_be": "0", "require_unlimited_mileage": "0", "hide_budget": "0"}


def now():
    return datetime.now(timezone.utc)


def connect():
    DB.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL, price REAL NOT NULL,
            currency TEXT NOT NULL, basis TEXT NOT NULL, url TEXT, via TEXT, baseline REAL,
            is_deal INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS failures (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL, reason TEXT);
        CREATE TABLE IF NOT EXISTS alerts (source TEXT NOT NULL, run_start TEXT NOT NULL, sent_ts TEXT NOT NULL, PRIMARY KEY (source, run_start));
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY, category TEXT NOT NULL, label TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}',
            target REAL, loosen_steps INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alerts2 (watch_id INTEGER NOT NULL, source TEXT NOT NULL, run_start TEXT NOT NULL, sent_ts TEXT NOT NULL, PRIMARY KEY (watch_id, source, run_start));
        CREATE TABLE IF NOT EXISTS profile (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS search_requests (id INTEGER PRIMARY KEY, category TEXT NOT NULL, params TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS search_results (
            id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL, ts TEXT NOT NULL, source TEXT NOT NULL,
            title TEXT, price REAL, url TEXT, detail TEXT
        );
        """
    )
    wcols = {r["name"] for r in db.execute("PRAGMA table_info(watches)")}
    if "target_unit" not in wcols:
        db.execute("ALTER TABLE watches ADD COLUMN target_unit TEXT NOT NULL DEFAULT 'total'")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(prices)")}
    for col, ddl in (("watch_id", "INTEGER"), ("detail", "TEXT")):
        if col not in cols:
            db.execute(f"ALTER TABLE prices ADD COLUMN {col} {ddl}")
    for k, v in DEFAULT_PROFILE.items():
        db.execute("INSERT OR IGNORE INTO profile (k, v) VALUES (?, ?)", (k, v))
    db.commit()
    return db


def profile(db):
    return {r["k"]: r["v"] for r in db.execute("SELECT k, v FROM profile")}


def trip_days(category, params):
    """Billing days: hotel = nights, car = 24h periods (rounded up, min 1). None if dates are missing."""
    try:
        if category == "hotel":
            a, b = params.get("checkin"), params.get("checkout")
            return max((date.fromisoformat(b) - date.fromisoformat(a)).days, 1) if a and b else None
        if category == "car":
            a = datetime.fromisoformat(f"{params['from']}T{params.get('pickupTime') or '10:00'}")
            b = datetime.fromisoformat(f"{params['to']}T{params.get('dropoffTime') or '10:00'}")
            return max(math.ceil((b - a).total_seconds() / 86400), 1)
    except (KeyError, ValueError):
        return None
    return None


VENDOR_HOME = {
    "avis": "https://www.avis.com/", "hertz": "https://www.hertz.com/", "budget": "https://www.budget.com/",
    "sixt": "https://www.sixt.com/", "alamo": "https://www.alamo.com/", "national": "https://www.nationalcar.com/",
    "thrifty": "https://www.thrifty.com/", "enterprise": "https://www.enterprise.com/",
    "ace": "https://www.acerentacar.com/", "fox rac": "https://www.foxrentacar.com/", "fox": "https://www.foxrentacar.com/",
}


# Hertz uses its own location IDs, not airport codes. Only IDs actually checked on hertz.com are listed; any other
# airport falls back to the Hertz home page. (Verified: ATL -> ATLT11.)
HERTZ_LOCATION_IDS = {"ATL": "ATLT11"}


def airport_code(text):
    """'ATL', 'atl airport' -> 'ATL'. Anything else (a city name) -> None."""
    m = re.fullmatch(r"\s*([A-Za-z]{3})(?:\s+(?:airport|apt|intl))?\s*", text or "")
    return m.group(1).upper() if m else None


def vendor_link(vendor, params):
    """Company-website link for a rental car. Returns (url, prefilled).
    VERIFIED to open with the trip filled in: Avis (pickup must be an airport code like ATL) and Hertz (only airports in HERTZ_LOCATION_IDS);
    every other company, and any city-name pickup, falls back to the company's home page (prefilled False)."""
    v = (vendor or "").strip().lower()
    home = VENDOR_HOME.get(v)
    if v not in ("avis", "hertz"):
        return home, False
    pick = airport_code(params.get("pickup"))
    drop = airport_code(params.get("dropoff")) if params.get("dropoff") else pick
    try:
        d1, d2 = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
        h1, m1 = map(int, (params.get("pickupTime") or "10:00").split(":"))
        h2, m2 = map(int, (params.get("dropoffTime") or "10:00").split(":"))
    except (KeyError, ValueError):
        return home, False
    if not (pick and drop):
        return home, False
    if v == "hertz":
        pid, did = HERTZ_LOCATION_IDS.get(pick), HERTZ_LOCATION_IDS.get(drop)
        if not (pid and did):
            return home, False
        q = [("pid", pid), ("pdate", f"{d1.isoformat()}T{h1:02d}:{m1:02d}:00"), ("did", did),
             ("ddate", f"{d2.isoformat()}T{h2:02d}:{m2:02d}:00"), ("pCountryCode", "US")]
        return "https://www.hertz.com/us/en/book/vehicles?" + urlencode(q), True
    q = [("dropoff_suggestion_type_code", "AIRPORT"), ("pickup_hour", f"{h1 % 12 or 12:02d}"), ("pickup_location_region", "NAM"),
         ("pickup_minute", f"{m1:02d}"), ("pickup_am_pm", "AM" if h1 < 12 else "PM"), ("pickup_day", f"{d1.day:02d}"),
         ("pickup_month", f"{d1.month:02d}"), ("pickup_suggestion_type_code", "AIRPORT"), ("pickup_year", str(d1.year)),
         ("residency_value", "US"), ("return_hour", f"{h2 % 12 or 12:02d}"), ("return_minute", f"{m2:02d}"),
         ("return_am_pm", "AM" if h2 < 12 else "PM"), ("return_day", f"{d2.day:02d}"), ("return_month", f"{d2.month:02d}"),
         ("return_year", str(d2.year)), ("pickup_location_code", pick), ("return_location_code", drop),
         ("country", "us"), ("locale", "en-US"), ("brand", "avis")]
    return "https://www.avis.com/en/reservation/vehicle-availability?" + urlencode(q), True


DATE_FIELDS = {"car": ("from", "to"), "hotel": ("checkin", "checkout"), "flight": ("depart", "ret")}


def check_dates(category, params, today=None):
    """Return a plain-English problem with the trip's dates, or None if they look fine.
    A search for dates that have already passed can never be answered, so the app refuses it."""
    today = today or date.today()
    start_key, end_key = DATE_FIELDS.get(category, (None, None))
    try:
        start = date.fromisoformat(params[start_key]) if params.get(start_key) else None
        end = date.fromisoformat(params[end_key]) if params.get(end_key) else None
    except (ValueError, TypeError):
        return "One of the dates isn't a valid date."
    if start and start < today:
        return f"The start date ({start.strftime('%b %d, %Y')}) has already passed. Pick a date from today on."
    if end and end < today:
        return f"The end date ({end.strftime('%b %d, %Y')}) has already passed. Pick a date from today on."
    if start and end and end < start:
        return "The end date is before the start date."
    return None


def search_target_total(category, params):
    """All-in TOTAL that a search's target works out to, or None if no usable target was given.
    Per-day/per-night targets are multiplied by the trip's days; flights are always a total."""
    try:
        t = float(params.get("target"))
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    unit = "total" if category == "flight" else params.get("unit", "day")
    if unit == "day":
        d = trip_days(category, params)
        return round(t * d, 2) if d else None
    return t


SCOUT_WINDOW_DAYS = 7  # the scout runs for one week at a time, then stops and asks


def scout_status(db):
    """Where the scout's one-week window stands. `expired` is True when there is no window or it has ended."""
    p = profile(db)
    exp = p.get("scout_expires") or ""
    if not exp:
        return {"expires": None, "expired": True, "seconds_left": 0, "notified": p.get("scout_notified") == "1"}
    left = (datetime.fromisoformat(exp) - now()).total_seconds()
    return {"expires": exp, "expired": left <= 0, "seconds_left": max(0, int(left)), "notified": p.get("scout_notified") == "1"}


def scout_extend(db, days=SCOUT_WINDOW_DAYS):
    """Start (or restart) the one-week window from now and clear the "week is up" notice flag."""
    exp = (now() + timedelta(days=days)).isoformat()
    db.execute("INSERT OR REPLACE INTO profile (k, v) VALUES ('scout_expires', ?)", (exp,))
    db.execute("INSERT OR REPLACE INTO profile (k, v) VALUES ('scout_notified', '0')")
    db.commit()
    return scout_status(db)


def effective_target(w):
    """All-in TOTAL the price must reach. A per-day target is multiplied by the trip's days first."""
    t = w["target"]
    if t is None:
        return None
    if w["target_unit"] == "day":
        d = trip_days(w["category"], json.loads(w["params"] or "{}"))
        if not d:
            return None
        t = t * d
    return round(t * (1 + 0.10 * min(w["loosen_steps"], MAX_LOOSEN_STEPS)), 2)


def baseline_for(db, source, currency, basis, before_ts, watch_id):
    since = (datetime.fromisoformat(before_ts) - timedelta(hours=CONFIG["baseline_window_hours"])).isoformat()
    rows = db.execute(
        "SELECT price FROM prices WHERE source=? AND currency=? AND basis=? AND is_deal=0 AND ts>=? AND ts<? "
        "AND IFNULL(watch_id,-1)=IFNULL(?,-1)",
        (source, currency, basis, since, before_ts, watch_id),
    ).fetchall()
    if len(rows) < CONFIG["min_baseline_samples"]:
        return None, len(rows)
    return sum(r["price"] for r in rows) / len(rows), len(rows)


def deal_run_start(db, source, watch_id, latest_ts):
    rows = db.execute(
        "SELECT ts, is_deal FROM prices WHERE source=? AND IFNULL(watch_id,-1)=IFNULL(?,-1) AND ts<=? ORDER BY ts DESC",
        (source, watch_id, latest_ts),
    ).fetchall()
    start = None
    for r in rows:
        if not r["is_deal"]:
            break
        start = r["ts"]
    return start


def fmt_duration(start_iso):
    secs = int((now() - datetime.fromisoformat(start_iso)).total_seconds())
    h, m = divmod(secs // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def log_price(db, source, price, *, watch_id=None, basis="unknown", currency="USD", url=None, via=None,
              airline=None, fare_class=None, bag_fee=None, mileage=None):
    """Record one observation. Returns a dict describing what happened (and any alert to send)."""
    ts = now().isoformat()
    w = db.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone() if watch_id else None
    detail = {k: v for k, v in (("airline", airline), ("fare_class", fare_class), ("bag_fee", bag_fee)) if v is not None}
    out = {"source": source, "watch_id": watch_id, "send_alert": False}

    if w is not None and w["category"] == "flight":
        al = (airline or "").strip().lower()
        if is_budget(al):
            if profile(db).get("hide_budget") == "1":
                return {**out, "excluded": f"{airline} is a cheap airline and cheap airlines are hidden in settings"}
            detail["tier"] = "budget"
            out["budget"] = True
        if al.startswith("united") and "basic" in (fare_class or "").lower():
            if profile(db).get("include_united_be") != "1":
                return {**out, "excluded": "United Basic Economy (no carry-on) hidden; toggle it on in settings"}
            fee = bag_fee if bag_fee is not None else DEFAULT_BAG_FEE
            price += fee
            detail.update(bag_fee=fee, note="United Basic Economy + bag fee")

    if w is not None and w["category"] == "car":
        detail["mileage"] = mileage or "unknown"
        if profile(db).get("require_unlimited_mileage") == "1" and mileage != "unlimited":
            basis = "unknown"  # logged for reference, never counts toward a deal
            detail["note"] = "unlimited mileage not confirmed"
            out["mileage_unconfirmed"] = True

    target = effective_target(w) if w is not None else None
    baseline, n = (None, 0)
    is_deal = 0
    if basis == "total":
        if target is not None:
            is_deal = int(price <= target)
        else:
            baseline, n = baseline_for(db, source, currency, basis, ts, watch_id)
            if baseline:
                is_deal = int(price <= baseline * (1 - CONFIG["deal_drop_pct"] / 100))
    db.execute(
        "INSERT INTO prices (ts, source, price, currency, basis, url, via, baseline, is_deal, watch_id, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, source, price, currency, basis, url, via, baseline, is_deal, watch_id, json.dumps(detail) if detail else None),
    )
    out.update(price=price, currency=currency, basis=basis, target=target, baseline=round(baseline, 2) if baseline else None,
               is_deal=bool(is_deal), counts_toward_deals=(basis == "total"))
    if is_deal:
        start = deal_run_start(db, source, watch_id, ts)
        out.update(live_since=start, live_for=fmt_duration(start), url=url)
        seen = db.execute("SELECT 1 FROM alerts2 WHERE watch_id=? AND source=? AND run_start=?", (watch_id or 0, source, start)).fetchone()
        if not seen:
            db.execute("INSERT INTO alerts2 VALUES (?,?,?,?)", (watch_id or 0, source, start, ts))
            label = w["label"] if w else CONFIG["hotel"]
            d = trip_days(w["category"], json.loads(w["params"] or "{}")) if w is not None and w["target_unit"] == "day" else None
            per = f", about {currency} {price / d:,.0f}/day for {d} days" if d else ""
            ref = f"your target {currency} {target:,.0f} total" if target is not None else f"normal {currency} {baseline:,.0f}"
            ref += per
            out["send_alert"] = True
            tag = "CHEAP AIRLINE DEAL (bags may cost extra)" if detail.get("tier") == "budget" else "DEAL"
            out["alert_text"] = (f"{tag}: {label} on {source}: {currency} {price:,.0f} all-in ({ref}), live {out['live_for']}. "
                                 f"Check the details above, then tap Book it now: {url or '(no link)'}").strip()
            p = profile(db)
            out["channels"] = {"push": True, "sms": p["sms_on"] == "1" and bool(p["phone"]),
                               "email": p["email_on"] == "1" and bool(p["email"]), "email_to": p["email"], "phone": p["phone"]}
    db.commit()
    return out


BUDGET_TAG = '%"tier": "budget"%'


def watch_summary(db, w):
    since = (now() - timedelta(hours=6)).isoformat()
    cols = "SELECT source, price, currency, url, ts, is_deal, detail FROM prices WHERE watch_id=? AND basis='total' AND ts>=? "
    best = db.execute(cols + "AND COALESCE(detail,'') NOT LIKE ? ORDER BY price ASC LIMIT 1", (w["id"], since, BUDGET_TAG)).fetchone()
    best_budget = db.execute(cols + "AND detail LIKE ? ORDER BY price ASC LIMIT 1", (w["id"], since, BUDGET_TAG)).fetchone()
    last = db.execute("SELECT MAX(ts) t FROM prices WHERE watch_id=?", (w["id"],)).fetchone()["t"]
    params = json.loads(w["params"] or "{}")
    days = trip_days(w["category"], params)
    eff = effective_target(w)
    b = dict(best) if best else None
    unconfirmed = None
    if not b:
        u = db.execute("SELECT source, price, currency, url, ts, detail FROM prices WHERE watch_id=? AND ts>=? AND COALESCE(detail,'') NOT LIKE ? "
                       "ORDER BY price ASC LIMIT 1", (w["id"], since, BUDGET_TAG)).fetchone()
        unconfirmed = dict(u) if u else None
    if b and days:
        b["per_day"] = round(b["price"] / days, 2)
    bb = dict(best_budget) if best_budget else None
    return {**dict(w), "params": params, "effective_target": eff, "days": days,
            "effective_per_day": round(eff / days, 2) if eff and days else None,
            "best": b, "best_budget": bb, "unconfirmed": unconfirmed, "last_checked": last}


def find_duplicate_watch(db, category, params, target, target_unit):
    """An active watch for the exact same trip and target, if one exists (stops double clicks making copies)."""
    want = json.dumps(params, sort_keys=True)
    for w in db.execute("SELECT * FROM watches WHERE status!='done' AND category=? AND target IS ? AND target_unit=?",
                        (category, target, target_unit)):
        if json.dumps(json.loads(w["params"] or "{}"), sort_keys=True) == want:
            return w["id"]
    return None


def add_watch(db, category, label, params, target=None, target_unit="total"):
    if target_unit not in ("total", "day"):
        raise ValueError("target_unit must be 'total' or 'day'")
    cur = db.execute("INSERT INTO watches (category, label, params, target, target_unit, created) VALUES (?,?,?,?,?,?)",
                     (category, label, json.dumps(params), target, target_unit, now().isoformat()))
    db.commit()
    return cur.lastrowid


# ---------- CLI ----------
def _j(x):
    print(json.dumps(x, default=str))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("watch-add"); s.add_argument("category", choices=["hotel", "flight", "car"]); s.add_argument("label")
    s.add_argument("--params", default="{}"); s.add_argument("--target", type=float)
    s.add_argument("--unit", choices=["total", "day"], default="total", help="day = target is per day/night, all-in")
    sub.add_parser("watch-list")
    s = sub.add_parser("watch-set"); s.add_argument("id", type=int); s.add_argument("--status", choices=["active", "paused", "done"], required=True)
    s = sub.add_parser("log"); s.add_argument("source"); s.add_argument("price", type=float); s.add_argument("--watch", type=int)
    s.add_argument("--currency", default="USD"); s.add_argument("--basis", default="unknown", choices=["total", "pre_tax", "unknown"])
    s.add_argument("--url"); s.add_argument("--via"); s.add_argument("--airline"); s.add_argument("--fare-class"); s.add_argument("--bag-fee", type=float)
    s.add_argument("--mileage", choices=["unlimited", "limited", "unknown"], help="cars: only \"unlimited\" counts when require_unlimited_mileage is on")
    s = sub.add_parser("fail"); s.add_argument("source"); s.add_argument("reason"); s.add_argument("--watch", type=int)
    sub.add_parser("pending-searches")
    s = sub.add_parser("result"); s.add_argument("request_id", type=int); s.add_argument("source"); s.add_argument("title")
    s.add_argument("price", type=float, nargs="?", default=None, help="omit for a no-price note row"); s.add_argument("--url"); s.add_argument("--detail", default="{}")
    s = sub.add_parser("search-done"); s.add_argument("request_id", type=int)
    s = sub.add_parser("vendor-link", help="company website link for a car result"); s.add_argument("vendor"); s.add_argument("--params", default="{}")
    sub.add_parser("scout-status", help="is the scout's one-week window still open?")
    s = sub.add_parser("scout-extend", help="start another week"); s.add_argument("--days", type=int, default=SCOUT_WINDOW_DAYS)
    sub.add_parser("scout-mark-notified", help="record that the 'week is up' notice was sent")
    sub.add_parser("profile"); sub.add_parser("status")
    a = p.parse_args()
    db = connect()

    if a.cmd == "watch-add":
        _j({"id": add_watch(db, a.category, a.label, json.loads(a.params), a.target, a.unit)})
    elif a.cmd == "watch-list":
        _j([watch_summary(db, w) for w in db.execute("SELECT * FROM watches WHERE status='active'")])
    elif a.cmd == "watch-set":
        db.execute("UPDATE watches SET status=? WHERE id=?", (a.status, a.id)); db.commit(); _j({"id": a.id, "status": a.status})
    elif a.cmd == "log":
        _j(log_price(db, a.source, a.price, watch_id=a.watch, basis=a.basis, currency=a.currency, url=a.url, via=a.via,
                     airline=a.airline, fare_class=a.fare_class, bag_fee=a.bag_fee, mileage=a.mileage))
    elif a.cmd == "fail":
        db.execute("INSERT INTO failures (ts, source, reason) VALUES (?,?,?)", (now().isoformat(), f"{a.source}" + (f"#w{a.watch}" if a.watch else ""), a.reason))
        db.commit(); _j({"source": a.source, "failed": a.reason})
    elif a.cmd == "pending-searches":
        _j([{**dict(r), "params": json.loads(r["params"])} for r in db.execute("SELECT * FROM search_requests WHERE status='pending' ORDER BY id")])
    elif a.cmd == "result":
        db.execute("INSERT INTO search_results (request_id, ts, source, title, price, url, detail) VALUES (?,?,?,?,?,?,?)",
                   (a.request_id, now().isoformat(), a.source, a.title, a.price, a.url, a.detail)); db.commit(); _j({"ok": True})
    elif a.cmd == "search-done":
        db.execute("UPDATE search_requests SET status='done' WHERE id=?", (a.request_id,)); db.commit(); _j({"ok": True})
    elif a.cmd == "vendor-link":
        url, pre = vendor_link(a.vendor, json.loads(a.params))
        _j({"url": url, "prefilled": pre})
    elif a.cmd == "scout-status":
        _j(scout_status(db))
    elif a.cmd == "scout-extend":
        _j(scout_extend(db, a.days))
    elif a.cmd == "scout-mark-notified":
        db.execute("INSERT OR REPLACE INTO profile (k, v) VALUES ('scout_notified', '1')"); db.commit(); _j(scout_status(db))
    elif a.cmd == "profile":
        _j(profile(db))
    elif a.cmd == "status":
        for w in db.execute("SELECT * FROM watches"):
            s = watch_summary(db, w)
            b = s["best"]
            print(f"#{s['id']} [{s['category']}] {s['label']}  target={s['effective_target']}  status={s['status']}  "
                  f"best={'%s %.0f via %s' % (b['currency'], b['price'], b['source']) if b else 'no all-in price yet'}")


if __name__ == "__main__":
    sys.exit(main())
