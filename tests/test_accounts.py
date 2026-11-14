"""Admin account, team and leaving a class."""
import unittest

from tests.test_tenants import TenantBase


class AccountTests(TenantBase):
    def login_admin(self, client, email="admin@local", secret="secret"):
        return self.login(client, email, secret, role="teacher")

    def test_admin_can_change_own_password(self):
        client = self.app.test_client()
        self.login_admin(client)
        token = self.csrf(client, "/admin/settings")
        client.post("/admin/account/password", data={"new_password": "brandnew1", "csrf": token})

        fresh = self.app.test_client()
        self.assertEqual(self.login_admin(fresh, secret="brandnew1").status_code, 302)

    def test_short_password_is_refused(self):
        client = self.app.test_client()
        self.login_admin(client)
        token = self.csrf(client, "/admin/settings")
        page = client.post("/admin/account/password", data={"new_password": "123", "csrf": token},
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("at least 6", page)

    def test_colleague_can_be_added_and_removed(self):
        group = self.registry.first_group()
        client = self.app.test_client()
        self.login_admin(client)
        token = self.csrf(client, "/admin/settings")
        client.post("/admin/team", data={"email": "mate@x.com", "password": "matepass", "csrf": token})
        self.assertEqual(len(self.registry.admins_of(group["id"])), 2)

        mate = self.app.test_client()
        self.assertEqual(self.login(mate, "mate@x.com", "matepass", role="teacher").status_code, 302)
        self.assertEqual(mate.get("/admin").status_code, 200)

        added = [a for a in self.registry.admins_of(group["id"]) if a["email"] == "mate@x.com"][0]
        token = self.csrf(client, "/admin/settings")
        client.post("/admin/team/{}/remove".format(added["id"]), data={"csrf": token})
        self.assertEqual(len(self.registry.admins_of(group["id"])), 1)

    def test_cannot_remove_yourself(self):
        client = self.app.test_client()
        self.login_admin(client)
        me = self.registry.admin_by_email("admin@local")
        token = self.csrf(client, "/admin/settings")
        page = client.post("/admin/team/{}/remove".format(me["id"]), data={"csrf": token},
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("cannot remove your own", page)
        self.assertEqual(len(self.registry.admins_of(self.registry.first_group()["id"])), 1)

    def test_student_can_leave_a_class(self):
        group = self.registry.first_group()
        client = self.app.test_client()
        self.register(client, "a@x.com")
        self.login(client, "a@x.com")
        self.join(client, group["join_code"])
        user = self.registry.user_by_email("a@x.com")

        token = self.csrf(client, "/join")
        client.post("/leave/{}".format(group["id"]), data={"csrf": token})
        self.assertIsNone(self.registry.membership(user["id"], group["id"]))
        self.assertTrue(client.get("/me").headers["Location"].endswith("/login"))

    def test_leaving_one_class_keeps_the_other(self):
        first = self.registry.first_group()
        client = self.app.test_client()
        token = self.csrf(client, "/org/new")
        client.post("/org/new", data={"org_name": "Sea Studio", "email": "sea@x.com",
                                      "password": "pass1234", "csrf": token})
        second = [g for g in self.registry.groups() if g["name"] == "Sea Studio"][0]

        student = self.app.test_client()
        self.register(student, "a@x.com")
        self.login(student, "a@x.com")
        self.join(student, first["join_code"])
        self.join(student, second["join_code"])

        token = self.csrf(student, "/join")
        student.post("/leave/{}".format(first["id"]), data={"csrf": token})
        page = student.get("/me").get_data(as_text=True)
        self.assertIn("Sea Studio", page)


if __name__ == "__main__":
    unittest.main()
