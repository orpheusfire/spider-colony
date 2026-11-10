"""Spider Colony tests. Run: python3 -m unittest discover -s tests -v (stdlib only, no installs)."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import colony  # noqa: E402
import server  # noqa: E402

CAR = {"pickup": "Chicago, IL", "from": "2026-11-17", "pickupTime": "10:00", "to": "2026-11-21", "dropoffTime": "10:00"}
FLIGHT = {"from": "MSP", "to": "MSY", "depart": "2026-12-04", "ret": "2026-12-07"}
HOTEL = {"where": "Hotel Monteleone", "checkin": "2026-12-07", "checkout": "2026-12-09"}
# Dates for API tests must be in the future, or the app (correctly) rejects them as already passed.
FUT_START = (date.today() + timedelta(days=30)).isoformat()
FUT_END = (date.today() + timedelta(days=44)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        colony.DB = Path(self._tmp.name) / "prices.db"
        self.db = colony.connect()

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def watch(self, cat, params, target, unit="total"):
        return colony.add_watch(self.db, cat, f"test {cat}", params, target, unit)

    def log(self, wid, price, **kw):
        kw.setdefault("basis", "total")
        return colony.log_price(self.db, "src", price, watch_id=wid, **kw)

    def set_profile(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO profile (k, v) VALUES (?, ?)", (k, v))
        self.db.commit()


class TripDays(Base):
    def test_hotel_nights(self):
        self.assertEqual(colony.trip_days("hotel", HOTEL), 2)

    def test_car_exact_days(self):
        self.assertEqual(colony.trip_days("car", CAR), 4)

    def test_car_late_return_counts_extra_day(self):
        self.assertEqual(colony.trip_days("car", {**CAR, "dropoffTime": "11:00"}), 5)

    def test_missing_dates_is_none(self):
        self.assertIsNone(colony.trip_days("car", {"pickup": "Chicago"}))
        self.assertIsNone(colony.trip_days("hotel", {"where": "x"}))
        self.assertIsNone(colony.trip_days("flight", FLIGHT))


class Targets(Base):
    def target(self, wid):
        return colony.effective_target(self.db.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone())

    def test_total_target(self):
        self.assertEqual(self.target(self.watch("flight", FLIGHT, 200)), 200)

    def test_per_day_multiplies_by_days(self):
        self.assertEqual(self.target(self.watch("car", CAR, 52, "day")), 208)

    def test_per_day_without_dates_has_no_target(self):
        self.assertIsNone(self.target(self.watch("car", {"pickup": "x"}, 52, "day")))

    def test_loosen_steps_and_cap(self):
        wid = self.watch("flight", FLIGHT, 100)
        self.db.execute("UPDATE watches SET loosen_steps=3 WHERE id=?", (wid,))
        self.assertEqual(self.target(wid), 130)
        self.db.execute("UPDATE watches SET loosen_steps=50 WHERE id=?", (wid,))
        self.assertEqual(self.target(wid), 190)  # capped at 9 steps = +90%

    def test_bad_unit_rejected(self):
        with self.assertRaises(ValueError):
            colony.add_watch(self.db, "car", "x", CAR, 10, "week")


class DealRules(Base):
    def test_deal_when_at_or_below_target(self):
        w = self.watch("flight", FLIGHT, 200)
        self.assertFalse(self.log(w, 201)["is_deal"])
        self.assertTrue(self.log(w, 200)["is_deal"])

    def test_pre_tax_and_unknown_never_deal_or_alert(self):
        w = self.watch("flight", FLIGHT, 200)
        for basis in ("pre_tax", "unknown"):
            r = self.log(w, 50, basis=basis)
            self.assertFalse(r["is_deal"])
            self.assertFalse(r["send_alert"])

    def test_one_alert_per_run_then_realert_after_break(self):
        w = self.watch("flight", FLIGHT, 200)
        self.assertTrue(self.log(w, 150)["send_alert"])
        self.assertFalse(self.log(w, 149)["send_alert"])  # same run
        self.assertFalse(self.log(w, 300)["is_deal"])     # run ends
        self.assertTrue(self.log(w, 150)["send_alert"])   # new run alerts again

    def test_per_day_car_alert_text_mentions_per_day(self):
        self.assertEqual(colony.profile(self.db)["require_unlimited_mileage"], "0")  # default: user checks mileage at booking
        w = self.watch("car", CAR, 52, "day")
        r = self.log(w, 200, mileage="unknown")
        self.assertTrue(r["send_alert"])
        self.assertIn("/day", r["alert_text"])
        self.assertIn("Book it now", r["alert_text"])

    def test_alert_channels_follow_profile(self):
        self.set_profile("phone", "5550001111")
        self.set_profile("sms_on", "0")
        self.set_profile("email", "test@example.com")
        w = self.watch("flight", FLIGHT, 200)
        ch = self.log(w, 100)["channels"]
        self.assertFalse(ch["sms"])
        self.assertTrue(ch["email"])


class FlightExclusions(Base):  # cheap-airline tier, United Basic Economy
    def test_cheap_airlines_are_logged_in_their_own_tier(self):
        w = self.watch("flight", FLIGHT, 500)
        for name in ("Frontier Airlines", "Spirit", "Allegiant Air", "Southwest + Allegiant"):
            r = self.log(w, 90, airline=name)
            self.assertNotIn("excluded", r)
            self.assertTrue(r["budget"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prices").fetchone()[0], 4)

    def test_cheap_airlines_never_mix_into_the_normal_best_price(self):
        w = self.watch("flight", FLIGHT, 100)
        self.log(w, 90, airline="Frontier Airlines")
        self.log(w, 300, airline="Delta")
        s = colony.watch_summary(self.db, self.db.execute("SELECT * FROM watches WHERE id=?", (w,)).fetchone())
        self.assertEqual(s["best"]["price"], 300)
        self.assertEqual(s["best_budget"]["price"], 90)

    def test_cheap_airline_alert_is_labelled(self):
        w = self.watch("flight", FLIGHT, 100)
        r = self.log(w, 90, airline="Spirit Airlines")
        self.assertTrue(r["send_alert"])
        self.assertIn("CHEAP AIRLINE", r["alert_text"])
        self.assertNotIn("CHEAP AIRLINE", self.log(w, 50, airline="Delta", basis="total").get("alert_text", ""))

    def test_hide_cheap_airlines_setting_drops_them(self):
        self.set_profile("hide_budget", "1")
        w = self.watch("flight", FLIGHT, 500)
        self.assertIn("excluded", self.log(w, 90, airline="Allegiant Air"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prices").fetchone()[0], 0)

    def test_is_budget_matches_inside_combined_names(self):
        self.assertTrue(colony.is_budget("MSY→TPA (Southwest, Allegiant)"))
        self.assertFalse(colony.is_budget("Delta"))

    def test_flexible_flag_makes_a_different_watch(self):
        a = self.watch("flight", FLIGHT, 400)
        self.assertEqual(colony.find_duplicate_watch(self.db, "flight", FLIGHT, 400, "total"), a)
        self.assertIsNone(colony.find_duplicate_watch(self.db, "flight", {**FLIGHT, "flex": True}, 400, "total"))

    def test_united_basic_economy_hidden_by_default(self):
        w = self.watch("flight", FLIGHT, 500)
        self.assertIn("excluded", self.log(w, 190, airline="United Airlines", fare_class="Basic Economy"))

    def test_united_basic_economy_adds_bag_fee_when_enabled(self):
        self.set_profile("include_united_be", "1")
        w = self.watch("flight", FLIGHT, 500)
        self.assertEqual(self.log(w, 190, airline="United", fare_class="Basic Economy")["price"], 235)

    def test_other_airlines_pass_through(self):
        w = self.watch("flight", FLIGHT, 500)
        self.assertEqual(self.log(w, 300, airline="Delta Air Lines")["price"], 300)


class MileageRule(Base):
    def test_strict_mode_blocks_unconfirmed_mileage(self):
        self.set_profile("require_unlimited_mileage", "1")
        w = self.watch("car", CAR, 52, "day")
        r = self.log(w, 100, mileage="unknown")
        self.assertFalse(r["is_deal"])
        self.assertTrue(r["mileage_unconfirmed"])
        self.assertTrue(self.log(w, 100, mileage="unlimited")["is_deal"])

    def test_relaxed_mode_counts_cars(self):
        self.set_profile("require_unlimited_mileage", "0")
        w = self.watch("car", CAR, 52, "day")
        self.assertTrue(self.log(w, 100, mileage="unknown")["is_deal"])


class HotelBaseline(Base):
    def test_no_alert_until_enough_history(self):
        w = self.watch("hotel", HOTEL, None)
        for _ in range(colony.CONFIG["min_baseline_samples"] - 1):
            self.log(w, 500)
        r = self.log(w, 100)
        self.assertFalse(r["is_deal"])  # still warming up

    def test_big_drop_after_history_is_a_deal(self):
        w = self.watch("hotel", HOTEL, None)
        for _ in range(colony.CONFIG["min_baseline_samples"]):
            self.log(w, 500)
        self.assertTrue(self.log(w, 300)["is_deal"])  # 40% below baseline


class DateChecks(unittest.TestCase):
    TODAY = colony.date(2026, 10, 21)

    def test_past_start_rejected(self):
        msg = colony.check_dates("car", {"from": "2026-10-17", "to": "2026-10-19"}, self.TODAY)
        self.assertIn("already passed", msg)

    def test_past_end_rejected(self):
        self.assertIn("already passed", colony.check_dates("hotel", {"checkin": "2026-10-21", "checkout": "2026-10-20"}, self.TODAY))

    def test_end_before_start_rejected(self):
        self.assertIn("before the start", colony.check_dates("flight", {"depart": "2026-12-07", "ret": "2026-12-04"}, self.TODAY))

    def test_good_and_partial_dates_pass(self):
        self.assertIsNone(colony.check_dates("car", {"from": "2026-10-21", "to": "2026-10-23"}, self.TODAY))  # today is fine
        self.assertIsNone(colony.check_dates("flight", {"depart": "2026-12-04"}, self.TODAY))  # one-way
        self.assertIsNone(colony.check_dates("hotel", {"where": "x"}, self.TODAY))  # no dates yet

    def test_garbage_date_rejected(self):
        self.assertIn("valid date", colony.check_dates("car", {"from": "soon"}, self.TODAY))


class ScoutWeek(Base):
    def test_no_window_means_stopped(self):
        st = colony.scout_status(self.db)
        self.assertTrue(st["expired"])
        self.assertIsNone(st["expires"])

    def test_extend_opens_a_week(self):
        st = colony.scout_extend(self.db)
        self.assertFalse(st["expired"])
        self.assertAlmostEqual(st["seconds_left"], 7 * 86400, delta=5)
        self.assertFalse(st["notified"])

    def test_window_closes_after_a_week(self):
        colony.scout_extend(self.db)
        past = (colony.now() - colony.timedelta(seconds=1)).isoformat()
        self.set_profile("scout_expires", past)
        self.assertTrue(colony.scout_status(self.db)["expired"])
        self.assertEqual(colony.scout_status(self.db)["seconds_left"], 0)

    def test_extending_after_expiry_restarts_and_clears_notice(self):
        self.set_profile("scout_expires", (colony.now() - colony.timedelta(days=1)).isoformat())
        self.set_profile("scout_notified", "1")
        self.assertTrue(colony.scout_status(self.db)["notified"])
        st = colony.scout_extend(self.db)
        self.assertFalse(st["expired"])
        self.assertFalse(st["notified"])


class VendorLinks(unittest.TestCase):
    TRIP = {"pickup": "ATL", "from": "2026-10-24", "pickupTime": "10:00", "to": "2026-11-07", "dropoffTime": "10:00"}

    def test_airport_code(self):
        self.assertEqual(colony.airport_code("atl"), "ATL")
        self.assertEqual(colony.airport_code("MSY airport"), "MSY")
        self.assertIsNone(colony.airport_code("Atlanta"))
        self.assertIsNone(colony.airport_code("Chicago, IL"))
        self.assertIsNone(colony.airport_code(None))

    def test_avis_link_is_prefilled_and_correct(self):
        url, pre = colony.vendor_link("Avis", self.TRIP)
        self.assertTrue(pre)
        for part in ("pickup_location_code=ATL", "return_location_code=ATL", "pickup_day=24", "pickup_month=10",
                     "return_day=07", "return_month=11", "pickup_hour=10", "pickup_am_pm=AM", "return_year=2026", "brand=avis"):
            self.assertIn(part, url)
        self.assertNotIn("age=", url)  # never guess a driver age

    def test_pm_and_noon_hours(self):
        url, _ = colony.vendor_link("avis", {**self.TRIP, "pickupTime": "13:30", "dropoffTime": "12:00"})
        self.assertIn("pickup_hour=01", url); self.assertIn("pickup_minute=30", url); self.assertIn("pickup_am_pm=PM", url)
        self.assertIn("return_hour=12", url); self.assertIn("return_am_pm=PM", url)

    def test_city_pickup_falls_back_to_home_page(self):
        self.assertEqual(colony.vendor_link("Avis", {**self.TRIP, "pickup": "Atlanta"}), ("https://www.avis.com/", False))

    def test_hertz_link_for_verified_airport(self):
        url, pre = colony.vendor_link("Hertz", self.TRIP)
        self.assertTrue(pre)
        self.assertEqual(url, "https://www.hertz.com/us/en/book/vehicles?pid=ATLT11&pdate=2026-10-24T10%3A00%3A00&did=ATLT11&ddate=2026-11-07T10%3A00%3A00&pCountryCode=US")
        self.assertNotIn("CDP", url)  # never add a discount code
        self.assertNotIn("age=", url)

    def test_hertz_unverified_airport_is_home_page(self):
        self.assertEqual(colony.vendor_link("Hertz", {**self.TRIP, "pickup": "ORD"}), ("https://www.hertz.com/", False))

    def test_other_vendors_are_home_page_only(self):
        self.assertEqual(colony.vendor_link("Budget", self.TRIP), ("https://www.budget.com/", False))
        self.assertEqual(colony.vendor_link("Mystery Cars", self.TRIP), (None, False))


class WebApi(Base):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return raw if path == "/" else json.loads(raw)

    def test_home_page_serves_and_has_key_text(self):
        html = self.call("/").decode()
        for text in ("Spider", "Book it now", "good travel deals", "Watch it"):
            self.assertIn(text, html)

    def test_create_watch_loosen_and_remove(self):
        wid = self.call("/api/watch", {"category": "car", "label": "t", "params": CAR, "target": 52, "target_unit": "day"})["id"]
        w = self.call("/api/state")["watches"][0]
        self.assertEqual((w["days"], w["effective_target"]), (4, 208.0))
        self.assertEqual(self.call(f"/api/watch/{wid}/loosen", {})["effective_target"], 228.8)
        self.call(f"/api/watch/{wid}/delete", {})
        self.assertEqual(self.call("/api/state")["watches"], [])

    def test_search_queue_round_trip(self):
        rid = self.call("/api/search", {"category": "hotel", "params": HOTEL})["id"]
        self.assertEqual(self.call(f"/api/search/{rid}")["status"], "pending")
        colony.add_watch  # (scouts fill results through the CLI/DB)
        self.db.execute("INSERT INTO search_results (request_id, ts, source, title, price) VALUES (?,?,?,?,?)",
                        (rid, colony.now().isoformat(), "t", "Room", 99.0))
        self.db.execute("UPDATE search_requests SET status='done' WHERE id=?", (rid,))
        self.db.commit()
        r = self.call(f"/api/search/{rid}")
        self.assertEqual((r["status"], r["results"][0]["price"]), ("done", 99.0))

    def test_book_it_now_only_when_under_target(self):
        # per-day target x 14 days = $700 total; at or under -> within_target True, over -> False
        params = {"pickup": "Atlanta", "from": FUT_START, "pickupTime": "10:00", "to": FUT_END, "dropoffTime": "10:00",
                  "target": "50", "unit": "day"}
        rid = self.call("/api/search", {"category": "car", "params": params})["id"]
        for price in (589.71, 700.00, 700.01, None):
            self.db.execute("INSERT INTO search_results (request_id, ts, source, title, price) VALUES (?,?,?,?,?)",
                            (rid, colony.now().isoformat(), "t", "car", price))
        self.db.commit()
        r = self.call(f"/api/search/{rid}")
        self.assertEqual(r["target_total"], 700.0)
        flags = {x["price"]: x["within_target"] for x in r["results"]}
        self.assertEqual(flags, {589.71: True, 700.0: True, 700.01: False, None: None})

    def test_cheap_airline_results_are_flagged_and_can_be_hidden(self):
        rid = self.call("/api/search", {"category": "flight", "params": {**FLIGHT, "flex": True}})["id"]
        for title in ("MSP→MSY (Delta)", "MSP→MSY (Frontier)"):
            self.db.execute("INSERT INTO search_results (request_id, ts, source, title, price) VALUES (?,?,?,?,?)",
                            (rid, colony.now().isoformat(), "t", title, 100.0))
        self.db.commit()
        flags = {x["title"]: x["budget"] for x in self.call(f"/api/search/{rid}")["results"]}
        self.assertEqual(flags, {"MSP→MSY (Delta)": False, "MSP→MSY (Frontier)": True})
        self.call("/api/profile", {"hide_budget": "1"})
        self.assertEqual([x["title"] for x in self.call(f"/api/search/{rid}")["results"]], ["MSP→MSY (Delta)"])

    def test_flexible_dates_checkbox_and_cheap_section_are_in_the_page(self):
        html = self.call("/").decode()
        for text in ("Flexible dates", 'name="flex"', "Cheap airlines", "s_hide"):
            self.assertIn(text, html)

    def test_flexible_flag_is_saved_with_the_watch(self):
        self.call("/api/watch", {"category": "flight", "label": "t", "params": {**FLIGHT, "flex": True}, "target": 300, "target_unit": "total"})
        self.assertTrue(self.call("/api/state")["watches"][0]["params"]["flex"])

    def test_car_results_get_company_link(self):
        params = {"pickup": "ATL", "from": FUT_START, "pickupTime": "10:00", "to": FUT_END, "dropoffTime": "10:00"}
        rid = self.call("/api/search", {"category": "car", "params": params})["id"]
        for title in ("Avis · Economy · x", "Hertz · Economy · x"):
            self.db.execute("INSERT INTO search_results (request_id, ts, source, title, price) VALUES (?,?,?,?,?)",
                            (rid, colony.now().isoformat(), "t", title, 100.0))
        self.db.commit()
        by = {x["title"].split(" ")[0]: x for x in self.call(f"/api/search/{rid}")["results"]}
        self.assertTrue(by["Avis"]["prefilled"]); self.assertIn("pickup_location_code=ATL", by["Avis"]["url"])
        self.assertEqual(by["Avis"]["link_kind"], "prefilled")
        self.assertTrue(by["Hertz"]["prefilled"]); self.assertIn("pid=ATLT11", by["Hertz"]["url"])

    def test_search_response_includes_category_and_params(self):
        rid = self.call("/api/search", {"category": "car", "params": {"pickup": "ORD", "from": "2026-11-17"}})["id"]
        r = self.call(f"/api/search/{rid}")
        self.assertEqual((r["category"], r["params"]["pickup"]), ("car", "ORD"))

    def test_stored_specific_link_is_kept(self):
        rid = self.call("/api/search", {"category": "car", "params": {"pickup": "atlanta"}})["id"]
        stored = "https://www.avis.com/en/reservation/vehicle-availability?pickup_location_code=ATL"
        self.db.execute("INSERT INTO search_results (request_id, ts, source, title, price, url) VALUES (?,?,?,?,?,?)",
                        (rid, colony.now().isoformat(), "t", "Avis · Economy", 100.0, stored))
        self.db.commit()
        r = self.call(f"/api/search/{rid}")["results"][0]
        self.assertEqual((r["url"], r["prefilled"]), (stored, True))

    def test_link_kinds_for_hotels_and_flights(self):
        rid = self.call("/api/search", {"category": "hotel", "params": HOTEL})["id"]
        for title, url in (("Room A", "https://www.booking.com/hotel/us/monteleone.html?checkin=2026-12-07"),
                           ("Room B", "https://www.example-hotel.com/"), ("Room C", None)):
            self.db.execute("INSERT INTO search_results (request_id, ts, source, title, price, url) VALUES (?,?,?,?,?,?)",
                            (rid, colony.now().isoformat(), "t", title, 100.0, url))
        self.db.commit()
        kinds = {x["title"]: x["link_kind"] for x in self.call(f"/api/search/{rid}")["results"]}
        self.assertEqual(kinds, {"Room A": "page", "Room B": "home", "Room C": None})

    def test_no_target_means_watch_not_book(self):
        rid = self.call("/api/search", {"category": "hotel", "params": HOTEL})["id"]
        self.db.execute("INSERT INTO search_results (request_id, ts, source, title, price) VALUES (?,?,?,?,?)",
                        (rid, colony.now().isoformat(), "t", "room", 10.0))
        self.db.commit()
        self.assertIsNone(self.call(f"/api/search/{rid}")["results"][0]["within_target"])

    def test_search_target_total(self):
        self.assertEqual(colony.search_target_total("flight", {"target": "200", "unit": "day"}), 200.0)  # flights are always totals
        self.assertEqual(colony.search_target_total("hotel", {**HOTEL, "target": "150", "unit": "day"}), 300.0)
        self.assertEqual(colony.search_target_total("hotel", {**HOTEL, "target": "400", "unit": "total"}), 400.0)
        self.assertIsNone(colony.search_target_total("car", {"target": "50", "unit": "day"}))  # no dates
        self.assertIsNone(colony.search_target_total("car", {"target": ""}))
        self.assertIsNone(colony.search_target_total("car", {}))

    def test_past_dates_are_refused_by_the_api(self):
        for path, body in (("/api/search", {"category": "car", "params": {"pickup": "ORD", "from": "2020-01-01", "to": "2020-01-03"}}),
                           ("/api/watch", {"category": "car", "label": "x", "params": {"from": "2020-01-01", "to": "2020-01-03"}, "target": 50})):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.call(path, body)
            self.assertEqual(cm.exception.code, 400)

    def test_identical_watch_is_not_created_twice(self):
        body = {"category": "flight", "label": "x", "params": {"from": "PNS", "to": "ORD", "depart": "2026-12-27"}, "target": 300, "target_unit": "total"}
        first = self.call("/api/watch", body)
        again = [self.call("/api/watch", body) for _ in range(8)]  # a held-down key
        self.assertNotIn("duplicate", first)
        self.assertTrue(all(a.get("duplicate") and a["id"] == first["id"] for a in again))
        self.assertEqual(len(self.call("/api/state")["watches"]), 1)
        # a different target IS a different watch
        self.assertNotIn("duplicate", self.call("/api/watch", {**body, "target": 250}))
        self.assertEqual(len(self.call("/api/state")["watches"]), 2)

    def test_changing_a_watch_target_resets_loosening(self):
        wid = self.call("/api/watch", {"category": "flight", "label": "x", "params": {"from": "PNS,MOB", "to": "ORD,MDW", "depart": "2026-12-27"}, "target": 300, "target_unit": "total"})["id"]
        for _ in range(3):
            self.call(f"/api/watch/{wid}/loosen", {})
        self.assertEqual(self.call(f"/api/watch/{wid}/target", {"target": 400})["effective_target"], 400.0)

    def test_scout_api_status_and_extend(self):
        self.assertTrue(self.call("/api/scout")["expired"])
        self.assertFalse(self.call("/api/scout/extend", {})["expired"])
        self.assertFalse(self.call("/api/scout")["expired"])

    def test_profile_only_accepts_known_keys(self):
        p = self.call("/api/profile", {"phone": "5551234567", "evil_key": "x"})
        self.assertEqual(p["phone"], "5551234567")
        self.assertNotIn("evil_key", p)


if __name__ == "__main__":
    unittest.main()
