import base64
import unittest

from studiobook.app import create_app


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
        self.assertEqual(self.client.delete(f"/api/bookings/{r.json['id']}", headers=self.auth).status_code, 204)
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


if __name__ == "__main__":
    unittest.main()