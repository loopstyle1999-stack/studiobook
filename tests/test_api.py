import base64
import unittest

from studiobook.app import create_app
from studiobook.i18n import pick_language


class StudioBookTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(":memory:", admin_password="secret").test_client()
        token = base64.b64encode(b"admin:secret").decode()
        self.auth = {"Authorization": "Basic " + token}

    def member(self, email="a@x.com", credits=1):
        return self.client.post("/api/members", json={"name": "A", "email": email, "credits": credits}, headers=self.auth).json["id"]

    def klass(self, capacity=1):
        return self.client.post(
            "/api/classes", json={"title": "Yoga", "starts_at": "2026-12-01T09:00", "capacity": capacity},
            headers=self.auth,
        ).json["id"]

    def test_booking_uses_and_refunds_credit(self):
        m, c = self.member(credits=1), self.klass()
        r = self.client.post("/api/bookings", json={"member_id": m, "class_id": c})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.client.delete(f"/api/bookings/{r.json['id']}", headers=self.auth).status_code, 200)
        # credit refunded: adding 1 more credit should give 2 in total
        self.assertEqual(self.client.post(f"/api/members/{m}/credits", json={"amount": 1}, headers=self.auth).json["credits"], 2)

    def test_invalid_credit_amount(self):
        m = self.member()
        self.assertEqual(self.client.post(f"/api/members/{m}/credits", json={"amount": 0}, headers=self.auth).status_code, 400)

    def test_no_credits(self):
        m, c = self.member(credits=0), self.klass()
        self.assertEqual(self.client.post("/api/bookings", json={"member_id": m, "class_id": c}).status_code, 402)

    def test_capacity_and_duplicates(self):
        m1, m2, c = self.member("a@x.com", 5), self.member("b@x.com", 5), self.klass(capacity=1)
        self.assertEqual(self.client.post("/api/bookings", json={"member_id": m1, "class_id": c}).status_code, 201)
        self.assertEqual(self.client.post("/api/bookings", json={"member_id": m1, "class_id": c}).status_code, 409)
        self.assertEqual(self.client.post("/api/bookings", json={"member_id": m2, "class_id": c}).status_code, 409)

    def test_duplicate_email(self):
        self.member("a@x.com")
        r = self.client.post("/api/members", json={"name": "B", "email": "a@x.com"}, headers=self.auth)
        self.assertEqual(r.status_code, 409)

    def test_homepage_lists_classes(self):
        self.klass(capacity=3)
        self.assertIn(b"3 / 3 seats left", self.client.get("/").data)

    def test_admin_routes_require_login(self):
        m, c = self.member(), self.klass()
        for method, url, payload in [
            ("post", "/api/members", {"name": "B", "email": "b@x.com"}),
            ("post", "/api/classes", {"title": "T", "starts_at": "x", "capacity": 1}),
            ("post", f"/api/members/{m}/credits", {"amount": 1}),
            ("delete", "/api/bookings/1", None),
        ]:
            r = getattr(self.client, method)(url, json=payload)
            self.assertEqual(r.status_code, 401, url)
            self.assertIn("Basic", r.headers["WWW-Authenticate"])

    def test_wrong_password_rejected(self):
        token = base64.b64encode(b"admin:nope").decode()
        r = self.client.post("/api/classes", json={}, headers={"Authorization": "Basic " + token})
        self.assertEqual(r.status_code, 401)

    def test_public_routes_stay_open(self):
        m, c = self.member(), self.klass()
        self.assertEqual(self.client.get("/api/classes").status_code, 200)
        self.assertEqual(self.client.post("/api/bookings", json={"member_id": m, "class_id": c}).status_code, 201)

    def test_admin_disabled_without_password(self):
        client = create_app(":memory:", admin_password="").test_client()
        self.assertEqual(client.post("/api/classes", json={}).status_code, 503)

    # --- waitlist ---
    def book(self, m, c):
        return self.client.post("/api/bookings", json={"member_id": m, "class_id": c})

    def join(self, m, c):
        return self.client.post("/api/waitlist", json={"member_id": m, "class_id": c})

    def credits_of(self, m):
        return self.client.post(f"/api/members/{m}/credits", json={"amount": 1}, headers=self.auth).json["credits"] - 1

    def test_waitlist_only_when_full(self):
        m, c = self.member(), self.klass(capacity=1)
        self.assertEqual(self.join(m, c).status_code, 409)  # still free seats

    def test_join_waitlist_positions_and_duplicates(self):
        m1, m2, m3 = self.member("a@x.com"), self.member("b@x.com"), self.member("c@x.com")
        c = self.klass(capacity=1)
        self.assertEqual(self.book(m1, c).status_code, 201)
        self.assertEqual(self.join(m1, c).status_code, 409)  # already booked
        self.assertEqual(self.join(m2, c).json["position"], 1)
        self.assertEqual(self.join(m3, c).json["position"], 2)
        self.assertEqual(self.join(m2, c).status_code, 409)  # already waiting

    def test_cancel_promotes_first_waiting_member(self):
        m1, m2, m3 = self.member("a@x.com"), self.member("b@x.com"), self.member("c@x.com")
        c = self.klass(capacity=1)
        booking = self.book(m1, c).json["id"]
        self.join(m2, c)
        self.join(m3, c)
        r = self.client.delete(f"/api/bookings/{booking}", headers=self.auth)
        self.assertEqual(r.json["promoted_member_ids"], [m2])
        self.assertEqual(self.credits_of(m2), 0)  # paid with a credit when promoted
        self.assertEqual(self.credits_of(m1), 1)  # refunded
        self.assertEqual(self.book(m3, c).status_code, 409)  # seat is taken again
        waiting = self.client.get(f"/api/classes/{c}/waitlist", headers=self.auth).json
        self.assertEqual([w["member_id"] for w in waiting], [m3])

    def test_promotion_skips_members_without_credits(self):
        m1, m2, m3 = self.member("a@x.com"), self.member("b@x.com", 0), self.member("c@x.com")
        c = self.klass(capacity=1)
        booking = self.book(m1, c).json["id"]
        self.join(m2, c)
        self.join(m3, c)
        r = self.client.delete(f"/api/bookings/{booking}", headers=self.auth)
        self.assertEqual(r.json["promoted_member_ids"], [m3])
        waiting = self.client.get(f"/api/classes/{c}/waitlist", headers=self.auth).json
        self.assertEqual([w["member_id"] for w in waiting], [m2])  # keeps its place

    def test_waitlist_admin_routes_and_size(self):
        m1, m2 = self.member("a@x.com"), self.member("b@x.com")
        c = self.klass(capacity=1)
        self.book(m1, c)
        entry = self.join(m2, c).json["id"]
        self.assertEqual(self.client.get(f"/api/classes/{c}/waitlist").status_code, 401)
        self.assertEqual(self.client.delete(f"/api/waitlist/{entry}").status_code, 401)
        self.assertEqual(self.client.get("/api/classes").json[0]["waitlist_size"], 1)
        self.assertIn(b"1 on waitlist", self.client.get("/").data)
        self.assertEqual(self.client.delete(f"/api/waitlist/{entry}", headers=self.auth).status_code, 204)
        self.assertEqual(self.client.delete(f"/api/waitlist/{entry}", headers=self.auth).status_code, 404)

    # --- languages ---
    def page(self, url="/", accept=None):
        headers = {"Accept-Language": accept} if accept else {}
        return self.client.get(url, headers=headers).data.decode("utf-8")

    def test_page_language_switch(self):
        self.klass(capacity=3)
        self.assertIn("3 / 3 seats left", self.page())
        self.assertIn("\u5269\u9918 3 / 3", self.page("/?lang=zh-Hant"))
        self.assertIn("\u5269\u4f59 3 / 3", self.page("/?lang=zh-Hans"))
        self.assertIn('lang="zh-Hant"', self.page("/?lang=zh-Hant"))

    def test_page_follows_browser_language(self):
        self.klass(capacity=3)
        self.assertIn("\u5269\u9918 3 / 3", self.page(accept="zh-TW,zh;q=0.9,en;q=0.8"))
        self.assertIn("\u5269\u4f59 3 / 3", self.page(accept="zh-CN"))
        self.assertIn("3 / 3 seats left", self.page(accept="fr-FR"))

    def test_pick_language(self):
        self.assertEqual(pick_language("zh-Hans", "en"), "zh-Hans")  # explicit choice wins
        self.assertEqual(pick_language("xx", "zh-HK"), "zh-Hant")  # unknown choice falls back
        self.assertEqual(pick_language(None, "en;q=0.5,zh-CN;q=0.9"), "zh-Hans")  # q-values respected
        self.assertEqual(pick_language(None, None), "en")
        self.assertEqual(pick_language(None, "de,fr;q=0.8"), "en")

    def test_waitlist_text_is_translated(self):
        m1, m2 = self.member("a@x.com"), self.member("b@x.com")
        c = self.klass(capacity=1)
        self.book(m1, c)
        self.join(m2, c)
        self.assertIn("1 on waitlist", self.page())
        self.assertIn("\u5019\u88dc 1 \u4eba", self.page("/?lang=zh-Hant"))


if __name__ == "__main__":
    unittest.main()
