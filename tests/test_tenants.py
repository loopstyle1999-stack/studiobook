"""Multi-institution behaviour: accounts, class codes, isolation."""
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from studiobook.app import create_app
from studiobook.core import Studio
from studiobook.db import connect


class TenantBase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = os.path.join(self.folder, "studio.db")
        self.app = create_app(self.db, admin_password="secret")
        self.day = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        self.registry = self.app.config["REGISTRY"]

    def csrf(self, client, url):
        html = client.get(url).get_data(as_text=True)
        found = re.search(r'name="csrf"\s+value="([^"]+)"', html)
        return found.group(1) if found else ""

    def register(self, client, email, password="pass1234", name="A"):
        token = self.csrf(client, "/register")
        return client.post("/register", data={"name": name, "email": email,
                                              "password": password, "csrf": token})

    def login(self, client, email, password="pass1234", role="student"):
        token = self.csrf(client, "/login")
        return client.post("/login", data={"role": role, "email": email,
                                           "password": password, "csrf": token})

    def join(self, client, code):
        token = self.csrf(client, "/join")
        return client.post("/join", data={"code": code, "csrf": token})

    def tenant(self, group):
        return Studio(connect(self.registry.tenant_path(group)))


class FirstRunTests(TenantBase):
    def test_existing_database_becomes_first_institution(self):
        group = self.registry.first_group()
        self.assertIsNotNone(group)
        self.assertEqual(len(group["join_code"]), 6)
        self.assertTrue(os.path.exists(self.registry.tenant_path(group)))
        self.assertEqual([a["email"] for a in self.registry.admins_of(group["id"])], ["admin@local"])

    def test_admin_logs_in_with_email_and_password(self):
        client = self.app.test_client()
        self.assertEqual(self.login(client, "admin@local", "secret", role="teacher").status_code, 302)
        page = client.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.registry.first_group()["join_code"], page.get_data(as_text=True))

    def test_wrong_admin_password_is_refused(self):
        client = self.app.test_client()
        page = self.login(client, "admin@local", "nope", role="teacher").get_data(as_text=True)
        self.assertIn("Wrong e-mail or password.", page)


class JoinTests(TenantBase):
    def test_login_without_a_class_goes_to_join(self):
        client = self.app.test_client()
        self.register(client, "a@x.com")
        response = self.login(client, "a@x.com")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/join"))

    def test_join_code_opens_the_schedule(self):
        group = self.registry.first_group()
        self.tenant(group).add_class("Yoga", f"{self.day} 09:00", 5)
        client = self.app.test_client()
        self.register(client, "a@x.com")
        self.login(client, "a@x.com")
        self.assertEqual(self.join(client, group["join_code"].lower()).status_code, 302)
        page = client.get(f"/me?day={self.day}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Yoga", page.get_data(as_text=True))

    def test_wrong_code_is_rejected(self):
        client = self.app.test_client()
        self.register(client, "a@x.com")
        self.login(client, "a@x.com")
        page = self.join(client, "ZZZZZZ").get_data(as_text=True)
        self.assertIn("does not match any class", page)

    def test_existing_member_row_is_reused(self):
        group = self.registry.first_group()
        tenant = self.tenant(group)
        member = tenant.add_member("Old", "old@x.com")[0]
        client = self.app.test_client()
        self.register(client, "old@x.com", name="Old")
        self.login(client, "old@x.com")
        self.join(client, group["join_code"])
        rows = tenant.db.execute("SELECT COUNT(*) AS n FROM members WHERE email = 'old@x.com'").fetchone()
        self.assertEqual(rows["n"], 1, "same e-mail must not create a second member row")
        self.assertEqual(self.registry.groups_for(self.registry.user_by_email("old@x.com")["id"])[0]["member_id"],
                         member["id"])


class IsolationTests(TenantBase):
    def new_org(self, name, email):
        client = self.app.test_client()
        token = self.csrf(client, "/org/new")
        client.post("/org/new", data={"org_name": name, "email": email,
                                      "password": "pass1234", "csrf": token})
        return client, [g for g in self.registry.groups() if g["name"] == name][0]

    def test_second_institution_starts_empty(self):
        client, group = self.new_org("Sea Studio", "sea@x.com")
        page = client.get("/admin").get_data(as_text=True)
        self.assertEqual(page.count("Yoga"), 0)
        self.assertNotIn(self.registry.first_group()["join_code"], page)

    def test_classes_do_not_leak_between_institutions(self):
        first = self.registry.first_group()
        self.tenant(first).add_class("Yoga", f"{self.day} 09:00", 5)
        _, second = self.new_org("Sea Studio", "sea@x.com")
        titles = [c["title"] for c in self.tenant(second).list_classes()]
        self.assertEqual(titles, [])

    def test_student_can_join_two_classes_and_switch(self):
        first = self.registry.first_group()
        self.tenant(first).add_class("Yoga", f"{self.day} 09:00", 5)
        _, second = self.new_org("Sea Studio", "sea@x.com")
        self.tenant(second).add_class("Ballet", f"{self.day} 09:00", 5)

        client = self.app.test_client()
        self.register(client, "a@x.com")
        self.login(client, "a@x.com")
        self.join(client, first["join_code"])
        self.join(client, second["join_code"])

        joined = self.registry.groups_for(self.registry.user_by_email("a@x.com")["id"])
        self.assertEqual([g["name"] for g in joined], [first["name"], "Sea Studio"])

        client.get(f"/switch/{second['id']}")
        page = client.get(f"/me?day={self.day}").get_data(as_text=True)
        self.assertIn("Ballet", page)
        self.assertNotIn("Yoga", page)


if __name__ == "__main__":
    unittest.main()
