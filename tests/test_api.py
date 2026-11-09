import unittest

from studiobook.app import create_app


class StudioBookTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(":memory:").test_client()

    def member(self, email="a@x.com", credits=1):
        return self.client.post("/api/members", json={"name": "A", "email": email, "credits": credits}).json["id"]

    def klass(self, capacity=1):
        return self.client.post(
            "/api/classes", json={"title": "Yoga", "starts_at": "2026-12-01T09:00", "capacity": capacity}
        ).json["id"]

    def test_booking_uses_and_refunds_credit(self):
        m, c = self.member(credits=1), self.klass()
        r = self.client.post("/api/bookings", json={"member_id": m, "class_id": c})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.client.delete(f"/api/bookings/{r.json['id']}").status_code, 204)
        # credit refunded: adding 1 more credit should give 2 in total
        self.assertEqual(self.client.post(f"/api/members/{m}/credits", json={"amount": 1}).json["credits"], 2)

    def test_invalid_credit_amount(self):
        m = self.member()
        self.assertEqual(self.client.post(f"/api/members/{m}/credits", json={"amount": 0}).status_code, 400)

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
        r = self.client.post("/api/members", json={"name": "B", "email": "a@x.com"})
        self.assertEqual(r.status_code, 409)

    def test_homepage_lists_classes(self):
        self.klass(capacity=3)
        self.assertIn(b"3 / 3 seats left", self.client.get("/").data)


if __name__ == "__main__":
    unittest.main()
