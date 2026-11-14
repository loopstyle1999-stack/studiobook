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


class StudentAccountTests(TenantBase):
    def test_student_password_change_takes_effect(self):
        group = self.registry.first_group()
        client = self.app.test_client()
        self.register(client, "a@x.com")
        self.login(client, "a@x.com")
        self.join(client, group["join_code"])

        token = self.csrf(client, "/me/account")
        client.post("/me/password", data={"password": "newpass99", "csrf": token})

        fresh = self.app.test_client()
        self.assertEqual(self.login(fresh, "a@x.com", "newpass99").status_code, 302)
        stale = self.app.test_client()
        self.assertIn("Wrong e-mail or password",
                      self.login(stale, "a@x.com", "pass1234").get_data(as_text=True))

    def test_logout_clears_the_whole_session(self):
        group = self.registry.first_group()
        client = self.app.test_client()
        self.register(client, "a@x.com")
        self.login(client, "a@x.com")
        self.join(client, group["join_code"])

        token = self.csrf(client, "/me/account")
        client.post("/me/logout", data={"csrf": token})
        self.assertTrue(client.get("/me").headers["Location"].endswith("/login"))
        self.assertTrue(client.get("/join").headers["Location"].endswith("/login"))

    def test_sign_in_key_survives_a_restart(self):
        from studiobook.app import create_app
        first = create_app(self.db, admin_password="secret")
        second = create_app(self.db, admin_password="secret")
        self.assertEqual(first.secret_key, second.secret_key)

    def test_repeated_wrong_passwords_are_throttled(self):
        client = self.app.test_client()
        for _ in range(5):
            self.login(client, "a@x.com", "nope")
        page = self.login(client, "a@x.com", "nope").get_data(as_text=True)
        self.assertIn("Too many wrong attempts", page)

    def test_teacher_can_reset_a_student_password(self):
        group = self.registry.first_group()
        student = self.app.test_client()
        self.register(student, "a@x.com")
        self.login(student, "a@x.com")
        self.join(student, group["join_code"])
        member_id = self.registry.groups_for(self.registry.user_by_email("a@x.com")["id"])[0]["member_id"]

        teacher = self.app.test_client()
        self.login(teacher, "admin@local", "secret", role="teacher")
        token = self.csrf(teacher, "/admin/members/%d" % member_id)
        teacher.post("/admin/members/%d/password" % member_id,
                     data={"password": "resetpass1", "csrf": token})

        fresh = self.app.test_client()
        self.assertEqual(self.login(fresh, "a@x.com", "resetpass1").status_code, 302)

    def test_reset_is_scoped_to_the_own_institution(self):
        teacher = self.app.test_client()
        self.login(teacher, "admin@local", "secret", role="teacher")
        other = self.app.test_client()
        token = self.csrf(other, "/org/new")
        other.post("/org/new", data={"org_name": "Sea Studio", "email": "sea@x.com",
                                     "password": "pass1234", "csrf": token})
        outsider = self.app.test_client()
        self.login(outsider, "sea@x.com", "pass1234", role="teacher")
        token = self.csrf(outsider, "/admin/members/1")
        outsider.post("/admin/members/1/password", data={"password": "hacked123", "csrf": token})
        # institution #1's member #1 must keep working with its own account
        page = outsider.get("/admin/members/1")
        self.assertEqual(page.status_code, 404)
