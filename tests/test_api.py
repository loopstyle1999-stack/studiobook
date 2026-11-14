import base64
import os
import pathlib
import re
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime

import studiobook
from studiobook.app import create_app
from studiobook.i18n import STRINGS, pick_language

NOW = datetime(2026, 11, 20, 8, 0)
CLASS_TIME = "2026-12-01T09:00"


class Base(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]
        self.app = create_app(":memory:", admin_password="secret", now=lambda: self.now[0])
        self.client = self.app.test_client()
        self.studio = self.app.config["STUDIO"]
        token = base64.b64encode(b"admin:secret").decode()
        self.auth = {"Authorization": "Basic " + token}

    # --- API helpers (HTTP Basic) ---
    def member(self, email="a@x.com", credits=1, name="A"):
        r = self.client.post("/api/members", json={"name": name, "email": email, "credits": credits}, headers=self.auth)
        return r.json["id"]

    def klass(self, capacity=1, starts_at=CLASS_TIME):
        r = self.client.post("/api/classes", json={"title": "Yoga", "starts_at": starts_at, "capacity": capacity},
                             headers=self.auth)
        return r.json["id"]

    def book(self, m, c):
        return self.client.post("/api/bookings", json={"member_id": m, "class_id": c}, headers=self.auth)

    def join(self, m, c):
        return self.client.post("/api/waitlist", json={"member_id": m, "class_id": c}, headers=self.auth)

    def cancel(self, booking_id):
        return self.client.delete(f"/api/bookings/{booking_id}", headers=self.auth)

    def credits(self, m):
        return self.studio.member(m)["credits"]

    # --- browser helpers (session cookie + CSRF token) ---
    def csrf(self, client=None):
        client = client or self.client
        client.get("/admin/login")
        with client.session_transaction() as s:
            return s["csrf"]

    def login(self, client=None, password="secret"):
        client = client or self.client
        return client.post("/admin/login", data={"password": password, "csrf": self.csrf(client)})

    def form(self, url, client=None, **data):
        client = client or self.client
        data["csrf"] = self.csrf(client)
        return client.post(url, data=data, follow_redirects=True).get_data(as_text=True)

    def page(self, url, client=None, accept=None):
        headers = {"Accept-Language": accept} if accept else {}
        return (client or self.client).get(url, headers=headers).get_data(as_text=True)


class ApiTests(Base):
    def test_booking_uses_and_refunds_credit(self):
        m, c = self.member(credits=1), self.klass()
        r = self.book(m, c)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.credits(m), 0)
        r = self.cancel(r.json["id"])
        self.assertEqual((r.status_code, r.json["refunded"]), (200, True))
        self.assertEqual(self.credits(m), 1)

    def test_validation(self):
        m = self.member()
        self.assertEqual(self.client.post(f"/api/members/{m}/credits", json={"amount": 0}, headers=self.auth).status_code, 400)
        self.assertEqual(self.client.post("/api/members", json={"name": "B", "email": "nope"}, headers=self.auth).status_code, 400)
        self.assertEqual(self.client.post("/api/classes", json={"title": "T", "starts_at": "x", "capacity": 1}, headers=self.auth).status_code, 400)

    def test_no_credits(self):
        m, c = self.member(credits=0), self.klass()
        self.assertEqual(self.book(m, c).status_code, 402)

    def test_capacity_and_duplicates(self):
        m1, m2, c = self.member("a@x.com", 5), self.member("b@x.com", 5), self.klass(capacity=1)
        self.assertEqual(self.book(m1, c).status_code, 201)
        self.assertEqual(self.book(m1, c).status_code, 409)
        self.assertEqual(self.book(m2, c).status_code, 409)

    def test_duplicate_email_is_case_insensitive(self):
        self.member("a@x.com")
        r = self.client.post("/api/members", json={"name": "B", "email": "A@X.com"}, headers=self.auth)
        self.assertEqual(r.status_code, 409)

    def test_class_in_the_past_cannot_be_booked(self):
        m = self.member()
        c = self.klass(starts_at="2026-11-20T07:00")
        self.assertEqual(self.book(m, c).status_code, 409)
        self.assertEqual(self.credits(m), 1)

    def test_admin_routes_require_basic_auth(self):
        m, c = self.member(), self.klass()
        for method, url, payload in [
            ("post", "/api/members", {"name": "B", "email": "b@x.com"}),
            ("post", "/api/classes", {"title": "T", "starts_at": CLASS_TIME, "capacity": 1}),
            ("post", f"/api/members/{m}/credits", {"amount": 1}),
            ("post", "/api/bookings", {"member_id": m, "class_id": c}),
            ("post", "/api/waitlist", {"member_id": m, "class_id": c}),
            ("get", f"/api/classes/{c}/waitlist", None),
            ("delete", "/api/bookings/1", None),
            ("delete", "/api/waitlist/1", None),
        ]:
            r = getattr(self.client, method)(url, json=payload)
            self.assertEqual(r.status_code, 401, url)
            self.assertIn("Basic", r.headers["WWW-Authenticate"])
        wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
        self.assertEqual(self.client.post("/api/classes", json={}, headers=wrong).status_code, 401)
        self.assertEqual(self.client.get("/api/classes").status_code, 200)  # the only open route

    def test_admin_disabled_without_password(self):
        client = create_app(":memory:", admin_password="", now=lambda: NOW).test_client()
        self.assertEqual(client.post("/api/classes", json={}).status_code, 503)

    def test_waitlist_rules_and_promotion(self):
        m1, m2, m3 = self.member("a@x.com"), self.member("b@x.com", 0), self.member("c@x.com")
        c = self.klass(capacity=1)
        self.assertEqual(self.join(m1, c).status_code, 409)  # free seats: book directly
        booking = self.book(m1, c).json["id"]
        self.assertEqual(self.join(m1, c).status_code, 409)  # already booked
        self.assertEqual(self.join(m2, c).json["position"], 1)
        self.assertEqual(self.join(m3, c).json["position"], 2)
        self.assertEqual(self.join(m2, c).status_code, 409)  # already waiting
        r = self.cancel(booking)
        self.assertEqual(r.json["promoted_member_ids"], [m3])  # m2 has no credits and keeps its place
        waiting = self.client.get(f"/api/classes/{c}/waitlist", headers=self.auth).json
        self.assertEqual([w["member_id"] for w in waiting], [m2])
        self.assertEqual(self.client.get("/api/classes").json[0]["waitlist_size"], 1)
        self.assertEqual(self.client.delete(f"/api/waitlist/{waiting[0]['id']}", headers=self.auth).status_code, 204)
        self.assertEqual(self.client.delete(f"/api/waitlist/{waiting[0]['id']}", headers=self.auth).status_code, 404)


class RuleTests(Base):
    def test_cancellation_window(self):
        m = self.member(credits=3)
        c = self.klass(starts_at="2026-11-20T12:00")  # 4 hours from "now"
        self.studio.save_settings("", 6)  # cancel at least 6 hours ahead -> already too late
        booking = self.book(m, c).json["id"]
        payload, status = self.studio.cancel(booking, member_id=m)
        self.assertEqual((status, payload["code"]), (409, "e_late"))
        payload, status = self.studio.cancel(booking)  # admin: allowed, credit kept
        self.assertEqual((status, payload["refunded"]), (200, False))
        self.assertEqual(self.credits(m), 2)

    def test_admin_can_force_refund_and_members_cancel_in_time(self):
        m = self.member(credits=3)
        c1, c2 = self.klass(starts_at="2026-11-20T12:00"), self.klass(starts_at="2026-11-25T12:00")
        self.studio.save_settings("", 6)
        late = self.book(m, c1).json["id"]
        ok = self.book(m, c2).json["id"]
        self.assertTrue(self.studio.cancel(late, refund=True)[0]["refunded"])
        self.assertTrue(self.studio.cancel(ok, member_id=m)[0]["refunded"])
        self.assertEqual(self.credits(m), 3)

    def test_cancel_by_another_member_is_refused(self):
        m1, m2, c = self.member("a@x.com"), self.member("b@x.com"), self.klass()
        booking = self.book(m1, c).json["id"]
        self.assertEqual(self.studio.cancel(booking, member_id=m2)[1], 404)

    def test_check_in_and_no_show(self):
        m, c = self.member(), self.klass()
        booking = self.book(m, c).json["id"]
        self.assertEqual(self.studio.mark(booking, "attended")[1], 200)
        self.assertEqual(self.studio.class_detail(c)["class"]["attended"], 1)
        self.assertEqual(self.studio.mark(booking, "no_show")[1], 200)
        self.assertEqual(self.studio.member_detail(m)["stats"]["no_show"], 1)
        self.assertEqual(self.studio.mark(booking, "bogus")[1], 400)

    def test_weekly_series(self):
        # 2026-12-02 is a Wednesday: Wed + Mon for 2 weeks = Dec 2, 7, 9, 14
        payload, status = self.studio.add_series("Pilates", "2026-12-02T09:00", 8, 60, "Joy", [0, 2], 2)
        self.assertEqual((status, payload["created"]), (201, 4))
        starts = [c["starts_at"] for c in self.studio.list_classes()]
        self.assertEqual(starts, ["2026-12-02 09:00", "2026-12-07 09:00", "2026-12-09 09:00", "2026-12-14 09:00"])
        self.assertEqual(self.studio.add_series("P", "2026-12-02T09:00", 8, 60, "", [], 2)[1], 400)
        self.assertEqual(self.studio.add_series("P", "2026-12-02T09:00", 8, 60, "", [1], 99)[1], 400)

    def test_cancel_whole_class_refunds_everyone(self):
        m1, m2, c = self.member("a@x.com"), self.member("b@x.com"), self.klass(capacity=1)
        self.book(m1, c)
        self.join(m2, c)
        payload, status = self.studio.cancel_class(c)
        self.assertEqual((status, payload["refunded"]), (200, 1))
        self.assertEqual(self.credits(m1), 1)
        self.assertEqual(self.studio.list_classes(), [])
        self.assertEqual(self.studio.rows("SELECT * FROM waitlist"), [])

    def test_credit_history(self):
        m, c = self.member(credits=2), self.klass()
        self.cancel(self.book(m, c).json["id"])
        self.client.post(f"/api/members/{m}/credits", json={"amount": 5}, headers=self.auth)
        reasons = [(l["reason"], l["delta"]) for l in self.studio.member_detail(m)["log"]]
        self.assertEqual(reasons, [("admin_add", 5), ("cancel_refund", 1), ("book", -1), ("initial", 2)])

    def test_member_login_and_lockout(self):
        created = self.studio.add_member("Amy", "Amy@X.com", 1)[0]
        self.assertEqual(self.studio.login_member("amy@x.com", created["access_code"].lower()), (created["id"], "ok"))
        for _ in range(5):
            self.assertEqual(self.studio.login_member("amy@x.com", "WRONG")[1], "bad")
        self.assertEqual(self.studio.login_member("amy@x.com", created["access_code"])[1], "locked")
        self.assertEqual(self.studio.login_member("nobody@x.com", "WRONG")[1], "bad")

    def test_reset_code_invalidates_old_one(self):
        created = self.studio.add_member("Amy", "amy@x.com", 1)[0]
        new = self.studio.reset_code(created["id"])[0]["access_code"]
        self.assertEqual(self.studio.login_member("amy@x.com", created["access_code"])[1], "bad")
        self.assertEqual(self.studio.login_member("amy@x.com", new)[1], "ok")

    def test_old_database_is_upgraded(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = os.path.join(folder, "old.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE members (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                                  credits INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0));
            CREATE TABLE classes (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, starts_at TEXT NOT NULL,
                                  capacity INTEGER NOT NULL CHECK (capacity > 0));
            CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER NOT NULL,
                                   class_id INTEGER NOT NULL, UNIQUE (member_id, class_id));
            INSERT INTO members (name, email, credits) VALUES ('Old', 'old@x.com', 3);
            INSERT INTO classes (title, starts_at, capacity) VALUES ('Old class', '2026-12-01T09:00', 5);
            INSERT INTO bookings (member_id, class_id) VALUES (1, 1);
        """)
        old.commit()
        old.close()
        studio = create_app(path, admin_password="secret", now=lambda: NOW).config["STUDIO"]
        self.assertEqual(studio.list_classes()[0]["starts_at"], "2026-12-01 09:00")
        self.assertEqual(studio.member_detail(1)["bookings"][0]["status"], "booked")
        self.assertEqual(studio.member(1)["credits"], 3)  # the old balance became a purchase
        code = studio.reset_code(1)[0]["access_code"]
        self.assertEqual(studio.login_member("old@x.com", code), (1, "ok"))


class AdminPageTests(Base):
    def test_pages_need_login(self):
        self.klass()
        for url in ("/admin", "/admin/schedule", "/admin/members", "/admin/settings", "/admin/classes/1", "/admin/members/1"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertTrue(r.headers["Location"].endswith("/admin/login"))

    def test_login_wrong_and_right_password(self):
        self.assertIn("Wrong password.", self.client.post("/admin/login", data={"password": "nope", "csrf": self.csrf()}).get_data(as_text=True))
        self.assertEqual(self.client.get("/admin").status_code, 302)
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_login_disabled_without_password(self):
        client = create_app(":memory:", admin_password="", now=lambda: NOW).test_client()
        page = client.get("/admin/login").get_data(as_text=True)
        self.assertIn("Admin is disabled", page)
        self.assertNotIn('type="password"', page)

    def test_csrf_token_required(self):
        self.login()
        r = self.client.post("/admin/classes", data={"title": "x", "starts_at": CLASS_TIME, "capacity": "1"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/admin/classes", data={"title": "x", "starts_at": CLASS_TIME, "capacity": "1", "csrf": "wrong"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post("/me/logout", data={}).status_code, 400)
        self.assertEqual(self.studio.list_classes(), [])

    def test_browser_session_does_not_unlock_api(self):
        self.login()
        r = self.client.post("/api/classes", json={"title": "T", "starts_at": CLASS_TIME, "capacity": 1})
        self.assertEqual(r.status_code, 401)

    def test_logout(self):
        self.login()
        self.client.post("/admin/logout", data={"csrf": self.csrf()})
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_every_page_renders_in_every_language(self):
        self.login()
        m = self.member("a@x.com", 2)
        m2 = self.member("b@x.com", 2)
        c = self.klass(capacity=1)
        self.book(m, c)
        self.join(m2, c)
        urls = ["/admin", "/admin/today", "/admin/schedule", "/admin/schedule?week=2026-12-01&type=1", "/admin/members",
                f"/admin/members/{m}", "/admin/members?q=a%40x", "/admin/members?filter=low", "/admin/members?filter=expiring",
                "/admin/catalog", "/admin/reports", "/admin/reports?month=2026-12", "/admin/settings", f"/admin/classes/{c}",
                "/", "/me/login", "/offline"]
        for lang in ("en", "zh-Hant", "zh-Hans"):
            for url in urls:
                sep = "&" if "?" in url else "?"
                r = self.client.get(f"{url}{sep}lang={lang}")
                self.assertEqual(r.status_code, 200, (lang, url))
                self.assertIn(f'lang="{lang}"', r.get_data(as_text=True))

    def test_admin_flow_through_forms(self):
        self.login()
        self.assertIn("Class added.", self.form("/admin/classes", title="Yoga", starts_at=CLASS_TIME, capacity="1", duration="45", instructor="Joy"))
        html = self.form("/admin/members", name="Amy", email="amy@x.com", credits="2")
        self.assertIn("Member added. Access code:", html)
        self.form("/admin/members", name="Ben", email="ben@x.com", credits="2")
        self.assertIn("Booked.", self.form("/admin/bookings", class_id="1", member_id="1", do="book"))
        html = self.form("/admin/bookings", class_id="1", member_id="2", do="waitlist")
        self.assertIn("(position 1)", html)
        html = self.form("/admin/bookings/1/cancel", next="/admin/classes/1")
        self.assertIn("Ben moved up from the waitlist", html)
        self.assertEqual(self.studio.class_detail(1)["class"]["booked"], 1)
        self.assertIn("Updated.", self.form("/admin/bookings/2/mark", status="attended", next="/admin/classes/1"))
        self.assertIn("Present", self.page("/admin/classes/1"))
        self.assertIn("Credits added.", self.form("/admin/members/1/credits", amount="3", note="10-class pack"))
        self.assertIn("10-class pack", self.page("/admin/members/1"))
        self.assertIn("Saved.", self.form("/admin/members/1/update", name="Amy Chen", phone="0912", notes="likes mornings"))
        self.assertIn("Amy Chen", self.page("/admin/members"))
        self.assertIn("New access code:", self.form("/admin/members/1/code"))

    def test_admin_errors_are_translated(self):
        self.login()
        self.form("/admin/classes", title="Yoga", starts_at=CLASS_TIME, capacity="1")
        self.form("/admin/members", name="Amy", email="amy@x.com", credits="0")
        self.assertIn("Could not complete: Not enough credits.", self.form("/admin/bookings", class_id="1", member_id="1", do="book"))
        self.assertIn("already registered", self.form("/admin/members", name="Amy2", email="amy@x.com"))
        self.assertIn("Could not complete", self.form("/admin/classes", title="", starts_at="", capacity=""))
        self.assertIn("\u4e0d\u8db3", self.form("/admin/bookings?lang=zh-Hant", class_id="1", member_id="1", do="book"))

    def test_series_form(self):
        self.login()
        html = self.form("/admin/classes", title="Pilates", starts_at="2026-12-02T09:00", capacity="8",
                         repeat="1", weekdays=["0", "2"], weeks="2")
        self.assertIn("Created 4 classes.", html)
        html = self.form("/admin/classes", title="P", starts_at="2026-12-02T09:00", capacity="8", repeat="1", weeks="2")
        self.assertIn("Pick at least one weekday", html)  # repeat ticked but no weekday chosen
        self.assertEqual(len(self.studio.list_classes()), 4)

    def test_settings_and_cancel_class(self):
        self.login()
        self.assertIn("Settings saved.", self.form("/admin/settings", studio_name="Lotus Studio", cutoff="12"))
        self.assertIn("Lotus Studio", self.page("/admin"))
        self.assertEqual(self.studio.cutoff_hours(), 12)
        self.assertIn("Could not complete", self.form("/admin/settings", studio_name="x", cutoff="500"))
        m, c = self.member(), self.klass()
        self.book(m, c)
        self.assertIn("Class cancelled. 1 credit(s) returned.", self.form(f"/admin/classes/{c}/cancel"))
        self.assertEqual(self.credits(m), 1)

    def test_week_calendar_shows_classes(self):
        self.login()
        self.klass(capacity=2)
        self.assertIn("09:00 Yoga", self.page("/admin/schedule?week=2026-12-01"))
        self.assertNotIn("09:00 Yoga", self.page("/admin/schedule?week=2026-12-14"))

    def test_dashboard_numbers(self):
        self.login()
        m = self.member("a@x.com", 0)
        c = self.klass(capacity=2, starts_at="2026-11-20T18:00")
        self.client.post("/api/members/%d/credits" % m, json={"amount": 1}, headers=self.auth)
        self.book(m, c)
        data = self.studio.dashboard()
        self.assertEqual((len(data["today_classes"]), data["today_bookings"], data["week_fill"]), (1, 1, 50))
        self.assertEqual([x["name"] for x in data["low_credit"]], ["A"])


class MemberPortalTests(Base):
    def setUp(self):
        super().setUp()
        self.created = self.studio.add_member("Amy", "amy@x.com", 3)[0]
        self.class_id = self.klass(capacity=1)
        self.me = self.app.test_client()

    def member_login(self, code=None, email="amy@x.com", client=None):
        client = client or self.me
        return client.post("/me/login", data={"email": email, "code": code or self.created["access_code"],
                                              "csrf": self.csrf(client)})

    def test_login_book_cancel(self):
        self.assertEqual(self.me.get("/me").status_code, 302)
        self.assertEqual(self.member_login().status_code, 302)
        home = self.page("/me", client=self.me)
        self.assertIn("Hi Amy", home)
        self.assertIn("3 credits", home)
        self.assertIn("Booked.", self.form("/me/book", client=self.me, class_id=str(self.class_id), do="book"))
        self.assertEqual(self.credits(self.created["id"]), 2)
        booking = self.studio.member_detail(self.created["id"])["bookings"][0]["id"]
        self.assertIn("Booking cancelled.", self.form(f"/me/cancel/{booking}", client=self.me))
        self.assertEqual(self.credits(self.created["id"]), 3)

    def test_wrong_login_and_lockout(self):
        r = self.member_login(code="WRONGCODE")
        self.assertIn("Wrong e-mail or password.", r.get_data(as_text=True))
        for _ in range(5):
            self.member_login(code="WRONGCODE")
        self.assertIn("Too many wrong attempts", self.member_login().get_data(as_text=True))

    def test_waitlist_and_late_cancel_from_member_page(self):
        other = self.studio.add_member("Ben", "ben@x.com", 3)[0]
        self.book(other["id"], self.class_id)
        self.member_login()
        self.assertIn("(position 1)", self.form("/me/book", client=self.me, class_id=str(self.class_id), do="waitlist"))
        entry = self.studio.waitlist_for_class(self.class_id)[0]["id"]
        self.assertIn("Removed.", self.form(f"/me/leave/{entry}", client=self.me))
        # a late cancellation is refused for members
        late = self.klass(capacity=2, starts_at="2026-11-20T09:00")
        self.studio.save_settings("", 3)
        booking = self.book(self.created["id"], late).json["id"]
        self.assertIn("Too late to cancel", self.form(f"/me/cancel/{booking}", client=self.me))
        self.assertEqual(self.credits(self.created["id"]), 2)

    def test_member_cannot_touch_other_members_data(self):
        other = self.studio.add_member("Ben", "ben@x.com", 3)[0]
        booking = self.book(other["id"], self.class_id).json["id"]
        self.member_login()
        self.assertIn("Could not complete", self.form(f"/me/cancel/{booking}", client=self.me))
        self.assertEqual(self.studio.class_detail(self.class_id)["class"]["booked"], 1)

    def test_member_session_is_not_admin(self):
        self.member_login()
        self.assertEqual(self.me.get("/admin").status_code, 302)
        self.assertEqual(self.me.post("/api/classes", json={}).status_code, 401)

    def test_member_logout_and_public_page(self):
        self.member_login()
        self.me.post("/me/logout", data={"csrf": self.csrf(self.me)})
        self.assertEqual(self.me.get("/me").status_code, 302)
        self.assertIn("Yoga", self.page("/"))
        self.assertIn("\u5269\u9918 1 / 1", self.page("/?lang=zh-Hant"))


class LanguageTests(Base):
    def test_pick_language(self):
        self.assertEqual(pick_language("zh-Hans", "en"), "zh-Hans")
        self.assertEqual(pick_language("xx", "zh-HK"), "zh-Hant")
        self.assertEqual(pick_language(None, "en;q=0.5,zh-CN;q=0.9"), "zh-Hans")
        self.assertEqual(pick_language(None, None), "en")
        self.assertEqual(pick_language(None, "de,fr;q=0.8"), "en")

    def test_choice_is_remembered_in_session(self):
        self.assertIn('lang="zh-Hant"', self.page("/admin/login?lang=zh-Hant"))
        self.assertIn('lang="zh-Hant"', self.page("/"))  # remembered for the next page
        self.assertIn('lang="en"', self.page("/?lang=en"))

    def test_follows_browser_language(self):
        self.assertIn('lang="zh-Hant"', self.page("/", accept="zh-TW,zh;q=0.9,en;q=0.8"))
        self.assertIn('lang="zh-Hans"', self.page("/", accept="zh-CN"))
        self.assertIn('lang="en"', self.page("/", accept="fr-FR"))

    def test_translations_are_complete(self):
        keys = set(STRINGS["en"])
        for lang in STRINGS:
            self.assertEqual(set(STRINGS[lang]), keys, lang)
        used = {f"wd{i}" for i in range(7)}
        used |= {f"{p}{s}" for p in ("s_", "mark_") for s in ("booked", "attended", "no_show")}
        used |= {f"r_{r}" for r in ("initial", "admin_add", "book", "cancel_refund", "late_cancel", "class_cancelled",
                                    "promoted", "purchase", "sale_voided")}
        used |= {f"f_{k}" for k in ("all", "low", "expiring", "inactive", "new")}
        used |= {f"pay_{k}" for k in ("cash", "transfer", "card", "other", "adjust")}
        used |= {f"kind_{k}" for k in ("pack", "unlimited", "dropin")}
        used |= {"class_added", "series_added", "class_cancelled", "booked_ok", "waitlisted_ok", "cancelled_ok",
                 "cancelled_no_refund", "promoted_ok", "marked_ok", "removed_ok", "member_added", "member_saved", "credits_added",
                 "sold_ok", "voided_ok", "code_reset", "plan_saved", "type_saved", "instructor_added", "saved_ok",
                 "settings_saved", "password_saved", "bad_login", "bad_password", "admin_disabled", "error"}
        root = pathlib.Path(studiobook.__file__).parent
        for path in list((root / "templates").glob("*.html")) + list(root.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if path.name != "i18n.py":
                used |= set(re.findall(r"\bt\('([a-z_0-9]+)'", text))
                used |= set(re.findall(r'"(e_[a-z_]+)"', text))
        used -= {"mark_", "r_", "s_", "pay_", "f_", "kind_"}
        self.assertEqual(sorted(k for k in used if k not in keys), [])


class PlanTests(Base):
    def plan(self, name="10-class pack", kind="pack", credits=10, days=90, price=3000):
        return self.studio.save_plan(None, name, kind, credits, days, price)[0]["id"]

    def test_pack_is_sold_with_expiry_and_revenue(self):
        m, plan = self.member(credits=0), self.plan()
        payload, status = self.studio.sell_plan(m, plan, "cash")
        self.assertEqual((status, payload["expires_on"]), (201, "2027-02-18"))  # 90 days after 2026-11-20
        self.assertEqual(self.credits(m), 10)
        report = self.studio.report("2026-11")
        self.assertEqual((report["revenue"], report["sales_count"]), (3000, 1))
        self.assertEqual(self.studio.dashboard()["revenue_month"], 3000)

    def test_expired_pack_cannot_pay_for_a_later_class(self):
        m, plan = self.member(credits=0), self.plan(days=10)  # valid until 2026-11-30
        self.studio.sell_plan(m, plan, "cash")
        self.assertEqual(self.book(m, self.klass(starts_at="2026-11-25T09:00")).status_code, 201)
        self.assertEqual(self.book(m, self.klass(starts_at="2026-12-05T09:00")).status_code, 402)  # pack ends before that class

    def test_expiring_pack_is_used_first_and_refund_goes_back_to_it(self):
        m = self.member(credits=0)
        short, long_ = self.plan("short", days=15), self.plan("long", days=100)
        self.studio.sell_plan(m, long_, "cash")
        self.studio.sell_plan(m, short, "cash")
        booking = self.book(m, self.klass(starts_at="2026-11-25T09:00")).json["id"]
        left = {p["name"]: p["credits_left"] for p in self.studio.list_purchases(m)}
        self.assertEqual(left, {"short": 9, "long": 10})
        self.cancel(booking)
        left = {p["name"]: p["credits_left"] for p in self.studio.list_purchases(m)}
        self.assertEqual(left, {"short": 10, "long": 10})

    def test_unlimited_membership_does_not_use_credits(self):
        m, month = self.member(credits=2), self.plan("Monthly", "unlimited", 0, 30, 2500)
        self.studio.sell_plan(m, month, "transfer")
        c1, c2 = self.klass(starts_at="2026-11-25T09:00"), self.klass(starts_at="2026-11-26T09:00")
        b1 = self.book(m, c1).json["id"]
        self.book(m, c2)
        member = self.studio.member(m)
        self.assertEqual((member["credits"], member["unlimited"]), (2, True))
        self.assertFalse(self.studio.member_detail(m)["log"][0]["delta"])  # memberships cost nothing per class
        self.assertEqual(self.cancel(b1).json["refunded"], True)
        self.assertEqual(self.credits(m), 2)
        self.now[0] = datetime(2026, 12, 25, 8, 0)  # the membership has ended, the 2 loose credits remain
        self.assertFalse(self.studio.member(m)["unlimited"])

    def test_dropin_and_validation(self):
        m = self.member(credits=0)
        drop = self.plan("Drop-in", "dropin", 5, 7, 400)
        self.assertEqual(self.studio.list_plans()[0]["credits"], 1)  # always one class
        self.assertEqual(self.studio.save_plan(None, "", "pack", 5, 0, 10)[1], 400)
        self.assertEqual(self.studio.save_plan(None, "Month", "unlimited", 0, 0, 10)[1], 400)  # needs a length
        self.assertEqual(self.studio.sell_plan(m, drop, "bitcoin")[1], 400)
        self.assertEqual(self.studio.sell_plan(m, 999, "cash")[1], 404)

    def test_void_sale_removes_credits_and_revenue(self):
        m, plan = self.member(credits=0), self.plan()
        sale = self.studio.sell_plan(m, plan, "cash")[0]["id"]
        self.assertEqual(self.studio.void_purchase(sale)[1], 200)
        self.assertEqual((self.credits(m), self.studio.report("2026-11")["revenue"]), (0, 0))
        self.assertEqual(self.studio.void_purchase(sale)[1], 404)

    def test_manual_credits_are_not_revenue(self):
        m = self.member(credits=3)
        self.client.post(f"/api/members/{m}/credits", json={"amount": 2}, headers=self.auth)
        self.assertEqual(self.studio.report("2026-11")["sales_count"], 0)

    def test_sell_through_api_and_admin_form(self):
        m, plan = self.member(credits=0), self.plan()
        r = self.client.post(f"/api/members/{m}/purchases", json={"plan_id": plan, "method": "card", "price": 2800}, headers=self.auth)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(self.client.get("/api/plans", headers=self.auth).json), 1)
        self.login()
        self.assertIn("Sold: 10-class pack.", self.form(f"/admin/members/{m}/sell", plan_id=str(plan), method="cash", price="", next=f"/admin/members/{m}"))
        self.assertEqual(self.credits(m), 20)
        self.assertIn("Sale cancelled.", self.form("/admin/purchases/2/void", next=f"/admin/members/{m}"))
        self.assertEqual(self.credits(m), 10)

    def test_catalog_forms(self):
        self.login()
        self.assertIn("Plan saved.", self.form("/admin/plans", name="Pack 5", kind="pack", credits="5", valid_days="60", price="1800", active="1"))
        self.assertIn("Class type saved.", self.form("/admin/types", name="Yoga", color="#ff8800", duration="75", capacity="12", active="1"))
        self.assertIn("Instructor added.", self.form("/admin/instructors", name="Joy"))
        self.assertIn("already exists", self.form("/admin/instructors", name="Joy"))
        self.assertIn("A class type needs", self.form("/admin/types", name="Bad", color="red", duration="60", capacity="5"))
        self.assertIn("Pack 5", self.page("/admin/catalog"))
        self.form("/admin/instructors/1/toggle")
        self.assertEqual(self.studio.list_instructors(active_only=True), [])

    def test_class_type_gives_defaults_and_colour(self):
        ty = self.studio.save_type(None, "Pilates", "#ff8800", 45, 6)[0]["id"]
        created = self.studio.add_class("", "2026-12-01T10:00", None, None, "Joy", ty)[0]
        self.assertEqual((created["title"], created["capacity"], created["duration_min"]), ("Pilates", 6, 45))
        listed = self.studio.list_classes()[0]
        self.assertEqual((listed["color"], listed["type_name"]), ("#ff8800", "Pilates"))
        self.assertEqual(self.studio.list_classes(type_id=ty + 1), [])

    def test_member_filters(self):
        low = self.member("low@x.com", credits=1, name="Low")
        rich = self.member("rich@x.com", credits=9, name="Rich")
        new = lambda flt: [m["name"] for m in self.studio.list_members(flt=flt)]
        self.assertEqual(new("low"), ["Low"])
        self.assertEqual(sorted(new("new")), ["Low", "Rich"])
        self.now[0] = datetime(2027, 3, 1, 8, 0)
        self.assertEqual(sorted(new("inactive")), ["Low", "Rich"])
        self.assertEqual(self.studio.list_members("ric")[0]["name"], "Rich")

    def test_reports_cover_attendance(self):
        ty = self.studio.save_type(None, "Yoga", "#0f766e", 60, 2)[0]["id"]
        m1, m2 = self.member("a@x.com", 5, "Amy"), self.member("b@x.com", 5, "Ben")
        c = self.studio.add_class("", "2026-11-25 09:00", None, None, "Joy", ty)[0]["id"]
        b1, b2 = self.book(m1, c).json["id"], self.book(m2, c).json["id"]
        self.studio.mark(b1, "attended")
        self.studio.mark(b2, "no_show")
        r = self.studio.report("2026-11")
        self.assertEqual((r["classes"], r["booked"], r["fill"], r["no_show_rate"], r["new_members"]), (1, 2, 100, 50, 2))
        self.assertEqual(r["by_type"][0]["name"], "Yoga")
        self.assertEqual(r["by_instructor"][0]["name"], "Joy")
        self.assertEqual(r["weekdays"][2], 2)  # 2026-11-25 is a Wednesday
        self.assertEqual([m["name"] for m in r["top_members"]], ["Amy"])
        self.assertEqual((r["prev"], r["next"]), ("2026-10", "2026-12"))
        self.assertEqual(self.studio.report("garbage")["month"], "2026-11")

    def test_csv_exports(self):
        m = self.member("a@x.com", 0, "Amy \u738b")
        self.studio.sell_plan(m, self.plan(), "cash")
        self.login()
        for url, needle in (("/admin/export/members.csv", "Amy \u738b"), ("/admin/export/sales.csv?month=2026-11", "10-class pack"),
                            ("/admin/export/bookings.csv?month=2026-11", "class_start")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertTrue(r.get_data(as_text=True).startswith("\ufeff"), url)  # Excel needs the BOM for Chinese text
            self.assertIn(needle, r.get_data(as_text=True))
        self.assertEqual(self.app.test_client().get("/admin/export/members.csv").status_code, 302)

    def test_booking_window_for_members_only(self):
        m = self.member(credits=3)
        far = self.klass(starts_at="2027-02-01T09:00")
        self.assertEqual(self.studio.book(m, far, member_self=True)[0]["code"], "e_window")
        self.assertEqual(self.book(m, far).status_code, 201)  # admins may book further ahead
        self.studio.save_settings("", 0, 90)
        other = self.klass(starts_at="2027-01-20T09:00")
        self.assertEqual(self.studio.book(m, other, member_self=True)[1], 201)
        self.assertEqual(self.studio.save_settings("", 0, 0)[1], 400)


class CalendarAndAppTests(Base):
    def test_overlapping_classes_get_side_by_side_lanes(self):
        from datetime import date
        from studiobook.web import layout_week
        self.klass(capacity=3, starts_at="2026-11-23T09:00")
        self.klass(capacity=3, starts_at="2026-11-23T09:30")
        self.klass(capacity=3, starts_at="2026-11-23T12:00")
        days = self.studio.week_schedule(date(2026, 11, 23))
        grid = layout_week(days)
        monday = days[0]["classes"]
        self.assertEqual([(c["lane"], c["lanes"]) for c in monday], [(0, 2), (1, 2), (0, 1)])
        self.assertEqual((grid["lo"], grid["hi"]), (7, 21))
        self.assertEqual(monday[0]["top"], 120)  # 09:00 is 2 hours after the grid starts at 07:00
        self.assertEqual(monday[0]["height"], 60)

    def test_grid_grows_for_early_and_late_classes(self):
        from datetime import date
        from studiobook.web import layout_week
        self.klass(capacity=3, starts_at="2026-11-23T05:30")
        self.klass(capacity=3, starts_at="2026-11-23T21:30")
        grid = layout_week(self.studio.week_schedule(date(2026, 11, 23)))
        self.assertEqual((grid["lo"], grid["hi"]), (5, 23))

    def test_schedule_page_filters(self):
        ty = self.studio.save_type(None, "Yoga", "#0f766e", 60, 4)[0]["id"]
        self.studio.add_class("Morning flow", "2026-11-23 09:00", 4, 60, "Joy", ty)
        self.studio.add_class("Spin", "2026-11-23 18:00", 4, 60, "Max", None)
        self.login()
        page = self.page("/admin/schedule?week=2026-11-23")
        self.assertIn("Morning flow", page)
        self.assertIn("Spin", page)
        filtered = self.page(f"/admin/schedule?week=2026-11-23&type={ty}")
        self.assertIn("Morning flow", filtered)
        self.assertNotIn("Spin", filtered)
        self.assertNotIn("Morning flow", self.page("/admin/schedule?week=2026-11-23&instructor=Max"))

    def test_front_desk_checks_people_in(self):
        m, c = self.member(), self.klass(starts_at="2026-11-20T18:00")
        booking = self.book(m, c).json["id"]
        self.login()
        self.assertIn("Present", self.page("/admin/today"))
        self.form(f"/admin/bookings/{booking}/mark", status="attended", next="/admin/today")
        self.assertEqual(self.studio.class_detail(c)["bookings"][0]["status"], "attended")

    def test_installable_web_app_files(self):
        manifest = self.client.get("/manifest.webmanifest")
        self.assertEqual(manifest.mimetype, "application/manifest+json")
        data = manifest.get_json()
        self.assertEqual((data["start_url"], data["display"]), ("/me", "standalone"))
        for icon in data["icons"]:
            response = self.client.get(icon["src"])
            self.assertEqual(response.status_code, 200)
            response.close()
        worker = self.client.get("/sw.js")
        self.assertEqual((worker.status_code, worker.mimetype), (200, "text/javascript"))
        self.assertIn("/offline", worker.get_data(as_text=True))
        worker.close()
        self.assertIn('rel="manifest"', self.page("/me/login"))


class MemberAppTests(Base):
    def setUp(self):
        super().setUp()
        self.ty = self.studio.save_type(None, "Yoga", "#0f766e", 60, 5)[0]["id"]
        self.created = self.studio.add_member("Amy", "amy@x.com", 3)[0]
        self.studio.add_class("", "2026-11-21 09:00", None, None, "Joy", self.ty)
        self.studio.add_class("Spin", "2026-11-21 18:00", 5, 45, "Max", None)
        self.me = self.app.test_client()
        self.me.post("/me/login", data={"email": "amy@x.com", "code": self.created["access_code"], "csrf": self.csrf(self.me)})

    def test_schedule_tab_has_calendar_and_type_filter(self):
        page = self.page("/me?day=2026-11-21", client=self.me)
        self.assertIn("Yoga", page)
        self.assertIn("Spin", page)
        self.assertIn("class=\"cal\"", page)
        only = self.page(f"/me?day=2026-11-21&type={self.ty}", client=self.me)
        self.assertIn("Joy", only)
        self.assertNotIn("Spin", only)
        self.assertIn("No classes this day.", self.page("/me?day=2026-11-22", client=self.me))

    def test_book_from_schedule_and_see_it_under_my_bookings(self):
        self.assertIn("Booked.", self.form("/me/book", client=self.me, class_id="1", do="book", next="/me?day=2026-11-21"))
        bookings = self.page("/me/bookings", client=self.me)
        self.assertIn("Yoga", bookings)
        account = self.page("/me/account", client=self.me)
        self.assertIn("2 credits", account)

    def test_account_shows_plans_and_password_change_works_for_login(self):
        self.studio.sell_plan(self.created["id"], self.studio.save_plan(None, "Monthly", "unlimited", 0, 30, 2500)[0]["id"], "cash")
        account = self.page("/me/account", client=self.me)
        self.assertIn("Monthly", account)
        self.assertIn("Valid until 2026-12-20", account)
        self.assertIn("Password saved.", self.form("/me/password", client=self.me, password="my-secret-pw"))
        self.assertIn("at least 6 characters", self.form("/me/password", client=self.me, password="123"))
        fresh = self.app.test_client()
        r = fresh.post("/me/login", data={"email": "amy@x.com", "code": "my-secret-pw", "csrf": self.csrf(fresh)})
        self.assertEqual(r.status_code, 302)
        r = fresh.post("/me/login", data={"email": "amy@x.com", "code": self.created["access_code"], "csrf": self.csrf(fresh)})
        self.assertEqual(r.status_code, 200)  # the old code no longer works

    def test_profile_edit_keeps_private_notes(self):
        self.studio.update_member(self.created["id"], "Amy", "", "private note")
        self.assertIn("Saved.", self.form("/me/profile", client=self.me, name="Amy C", phone="0912"))
        member = self.studio.member(self.created["id"])
        self.assertEqual((member["name"], member["phone"], member["notes"]), ("Amy C", "0912", "private note"))


if __name__ == "__main__":
    unittest.main()
