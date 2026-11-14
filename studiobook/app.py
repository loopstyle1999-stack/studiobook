"""studiobook: a self-hosted booking and membership app for small studios.

Two modes:

* multi-tenant (normal): a registry database keeps accounts, institutions and
  memberships; every institution has its own SQLite database, so all the
  scheduling code stays single-tenant.
* single-tenant (``db_path=":memory:"``): used by the test-suite, one studio,
  password-only admin login.
"""
import hmac
import json
import os
import secrets
import time
from datetime import datetime

from flask import (Flask, Response, flash, has_request_context, redirect, render_template,
                   request, send_from_directory, session, url_for)

from . import admin, api, portal
from .core import Studio
from .db import connect
from .i18n import LABELS, translate
from .registry import Registry, registry_path, seed_from_single_tenant
from .web import current_lang


class StudioProxy:
    """Behaves like a Studio, but resolves to the signed-in institution on each access."""

    def __init__(self, resolver):
        self._resolver = resolver

    def __getattr__(self, name):
        return getattr(self._resolver(), name)


def _password_file(db_path: str = None) -> str:
    """Password file sitting next to the database, used when no env var is set."""
    base = os.path.dirname(os.path.abspath(db_path or os.environ.get("STUDIOBOOK_DB", "studiobook.db")))
    return os.path.join(base, "admin-password.txt")


def _read_password_file(db_path: str = None):
    path = _password_file(db_path)
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        return value or None
    except OSError:
        return None


def _secret_key(db_path=None):
    """Keep one signing key across restarts, otherwise every restart logs everyone out."""
    env = os.environ.get("STUDIOBOOK_SECRET")
    if env:
        return env
    if not db_path or db_path == ":memory:":
        return secrets.token_hex(32)
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), ".secret-key")
    try:
        with open(path, encoding="utf-8") as handle:
            stored = handle.read().strip()
        if stored:
            return stored
    except OSError:
        pass
    value = secrets.token_hex(32)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)
    except OSError:
        pass
    return value


def create_app(db_path: str = None, admin_password: str = None, now=None) -> Flask:
    app = Flask(__name__)
    db_path = db_path or os.environ.get("STUDIOBOOK_DB", "studiobook.db")
    if admin_password is None:
        admin_password = os.environ.get("STUDIOBOOK_ADMIN_PASSWORD") or None
        if not admin_password and db_path != ":memory:":
            admin_password = _read_password_file(db_path)
    app.secret_key = _secret_key(db_path)
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

    multi = db_path != ":memory:"
    registry = None
    studios = {}

    if multi:
        registry = Registry(registry_path(db_path))
        # first run: adopt the existing single-studio database as institution #1
        if registry.first_group() is None:
            name = "Studio"
            try:
                probe = Studio(connect(db_path), now=now)
                name = probe.studio_name() or name
            except Exception:
                pass
            seed_from_single_tenant(registry, db_path, admin_password, name)
        app.config["REGISTRY"] = registry

        def studio_for(group_id):
            group = registry.group_by_id(group_id) or registry.first_group()
            if group is None:
                return None
            if group["id"] not in studios:
                studios[group["id"]] = Studio(connect(registry.tenant_path(group)), now=now)
            return studios[group["id"]]

        def current_group():
            gid = session.get("group_id") or session.get("admin_group")
            group = registry.group_by_id(gid) if gid else None
            return group or registry.first_group()

        def resolve_studio():
            """Outside a request (scripts, tests) fall back to the first institution."""
            if not has_request_context():
                return studio_for(registry.first_group()["id"])
            return studio_for(current_group()["id"])

        studio = StudioProxy(resolve_studio)
    else:
        studio = Studio(connect(db_path), now=now)
        registry = None

    app.config["STUDIO"] = studio

    attempts = {}

    def too_many(key):
        now_ts = time.time()
        recent = [t for t in attempts.get(key, []) if now_ts - t < 600]
        attempts[key] = recent
        return len(recent) >= 5

    def note_failure(key):
        attempts.setdefault(key, []).append(time.time())

    def password_ok(candidate):
        return bool(admin_password) and hmac.compare_digest(
            candidate.encode("utf-8"), admin_password.encode("utf-8")
        )

    def signed_in_user():
        if not multi or "user_id" not in session:
            return None
        return registry.db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    @app.context_processor
    def template_context():
        lang = current_lang()
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        group = current_group() if multi else None
        user = signed_in_user()
        signed = bool(session.get("group_id") or session.get("admin_group") or not multi)
        return dict(
            lang=lang,
            languages=LABELS,
            csrf=session["csrf"],
            studio_name=studio.studio_name(),
            brand=studio.studio_name() if signed else os.environ.get("STUDIOBOOK_BRAND", "跃动"),
            currency=studio.currency(),
            multi_tenant=multi,
            my_groups=registry.groups_for(user["id"]) if (user and registry) else [],
            team_admins=registry.admins_of(group["id"]) if (registry and group and session.get("admin")) else [],
            me_admin_id=session.get("admin_id"),
            current_group=group,
            join_code=group["join_code"] if group else "",
            t=lambda key, **values: translate(lang, key, **values),
        )

    @app.template_global()
    def lang_url(code):
        args = request.args.to_dict()
        args["lang"] = code
        return url_for(request.endpoint, **(request.view_args or {}), **args)

    @app.template_global()
    def hhmm(value):
        return value[11:16]

    @app.template_global()
    def day_label(value):
        if isinstance(value, str):
            value = datetime.strptime(value[:10], "%Y-%m-%d").date()
        return f"{translate(current_lang(), 'wd%d' % value.weekday())} {value.month}/{value.day}"

    @app.template_global()
    def money(value):
        return f"{studio.currency()}{value:,}"

    @app.template_global()
    def initials(name):
        parts = (name or "?").split()
        return ((parts[0][0] + parts[-1][0]) if len(parts) > 1 else parts[0][:2]).upper()

    @app.template_global()
    def hue(name):
        return sum(ord(ch) for ch in (name or "")) * 37 % 360

    @app.before_request
    def csrf_protect():
        guarded = ("/admin", "/me", "/login", "/join", "/register", "/org")
        if request.method == "POST" and request.path.startswith(guarded):
            token = session.get("csrf")
            if not token or not hmac.compare_digest(token, request.form.get("csrf", "")):
                return "Invalid or missing CSRF token. Go back, reload the page and try again.", 400

    @app.before_request
    def one_login_page():
        """In multi-tenant mode the old per-role login URLs just point at /login."""
        if not multi:
            return None
        if request.method != "GET":
            return None
        if request.path == "/me/login":
            return redirect(url_for("login", role="student"))
        if request.path == "/admin/login":
            return redirect(url_for("login", role="teacher"))
        return None

    @app.get("/")
    def index():
        if multi:
            return redirect(url_for("login"))
        now = studio.now()
        upcoming = studio.list_classes(now.strftime("%Y-%m-%d %H:%M"))
        days = {}
        for c in upcoming[:80]:
            days.setdefault(c["starts_at"][:10], []).append(c)
        return render_template("index.html", days=days)

    # ------------------------------------------------------------- sign in
    def sign_in_student():
        email = request.form.get("email", "")
        secret = request.form.get("password") or request.form.get("code") or ""
        if not multi:
            member_id, outcome = studio.login_member(email, secret)
            if outcome == "ok":
                session["member_id"] = member_id
                session.pop("admin", None)
                return None
            return "e_locked" if outcome == "locked" else "bad_login"
        email = (email or "").strip().lower()
        if too_many("u:" + email):
            return "e_locked"
        user = registry.verify_user(email, secret)
        if user is None:
            note_failure("u:" + email)
            return "bad_login"
        attempts.pop("u:" + email, None)
        session["user_id"] = user["id"]
        session.pop("admin", None)
        joined = registry.groups_for(user["id"])
        if not joined:
            return "go_join"
        first = joined[0]
        session["group_id"] = first["id"]
        session["member_id"] = first["member_id"]
        return "go_me"

    def sign_in_teacher():
        if not multi:
            if not admin_password:
                return "admin_disabled"
            if password_ok(request.form.get("password", "")):
                session["admin"] = True
                session.pop("member_id", None)
                return "go_admin"
            return "bad_password"
        email = (request.form.get("email") or "").strip().lower()
        if too_many("a:" + email):
            return "e_locked"
        admin_row = registry.verify_admin(email, request.form.get("password", ""))
        if admin_row is None:
            note_failure("a:" + email)
            return "bad_login"
        attempts.pop("a:" + email, None)
        session["admin"] = True
        session["admin_id"] = admin_row["id"]
        session["admin_group"] = admin_row["group_id"]
        session.pop("member_id", None)
        session.pop("group_id", None)
        return "go_admin"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        role = (request.values.get("role") or "student").strip()
        if role not in ("student", "teacher"):
            role = "student"
        error = None
        if request.method == "POST":
            outcome = sign_in_teacher() if role == "teacher" else sign_in_student()
            if outcome == "go_admin":
                return redirect(url_for("admin_dashboard"))
            if outcome == "go_me":
                return redirect(url_for("member_home"))
            if outcome == "go_join":
                return redirect(url_for("join_group"))
            error = outcome
        return render_template(
            "login.html", role=role, error=error,
            admin_ready=bool(admin_password) if not multi else True,
            has_admin=bool(registry.admin_count()) if multi else bool(admin_password),
        )

    # ----------------------------------------------------------- sign up
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if not multi:
            return redirect(url_for("login"))
        error = None
        if request.method == "POST":
            user, error = registry.create_user(
                request.form.get("name"), request.form.get("email"), request.form.get("password")
            )
            if user:
                session["user_id"] = user["id"]
                return redirect(url_for("join_group"))
        return render_template("register.html", error=error)

    @app.route("/org/new", methods=["GET", "POST"])
    def new_org():
        if not multi:
            return redirect(url_for("login"))
        error = None
        if request.method == "POST":
            name = request.form.get("org_name", "").strip()
            email = request.form.get("email", "")
            password = request.form.get("password", "")
            if not name:
                error = "e_org_name"
            else:
                group = registry.create_group(name)
                created, why = registry.create_admin(group["id"], email, password)
                if created:
                    tenant = studio_for(group["id"])
                    tenant.save_settings(name, tenant.cutoff_hours(), tenant.window_days(), tenant.currency())
                    session["admin"] = True
                    session["admin_group"] = group["id"]
                    return redirect(url_for("admin_dashboard"))
                error = why
        return render_template("new_org.html", error=error)

    # ------------------------------------------------------- join a class
    @app.route("/join", methods=["GET", "POST"])
    def join_group():
        if not multi:
            return redirect(url_for("login"))
        user = signed_in_user()
        if user is None:
            return redirect(url_for("login"))
        error = None
        if request.method == "POST":
            group = registry.group_by_code(request.form.get("code"))
            if group is None:
                error = "e_bad_code"
            else:
                member_id = ensure_member(registry, studio_for, group, user)
                registry.add_membership(user["id"], group["id"], member_id)
                session["group_id"] = group["id"]
                session["member_id"] = member_id
                return redirect(url_for("member_home"))
        return render_template("join.html", error=error, my_groups=registry.groups_for(user["id"]))

    @app.get("/switch/<int:group_id>")
    def switch_group(group_id):
        if not multi:
            return redirect(url_for("member_home"))
        user = signed_in_user()
        if user is None:
            return redirect(url_for("login"))
        membership = registry.membership(user["id"], group_id)
        if membership is None:
            return redirect(url_for("join_group"))
        session["group_id"] = group_id
        session["member_id"] = membership["member_id"]
        return redirect(url_for("member_home"))

    @app.post("/logout")
    def logout():
        for key in ("member_id", "admin", "user_id", "group_id", "admin_group"):
            session.pop(key, None)
        return redirect(url_for("login") if multi else url_for("index"))

    # ------------------------------------------------- admin account and team
    def signed_in_admin():
        if not multi or not session.get("admin") or not session.get("admin_id"):
            return None
        return registry.admin_by_id(session["admin_id"])

    @app.post("/admin/account/password")
    def admin_password_change():
        if not session.get("admin"):
            return redirect(url_for("login", role="teacher"))
        lang = current_lang()
        row = signed_in_admin()
        if row is None:
            flash(translate(lang, "e_password"), "error")
            return redirect(url_for("admin_settings"))
        if not registry.set_admin_password(row["id"], request.form.get("new_password", "")):
            flash(translate(lang, "e_password"), "error")
        else:
            flash(translate(lang, "password_changed"), "ok")
        return redirect(url_for("admin_settings"))

    @app.post("/admin/team")
    def admin_team_add():
        if not session.get("admin"):
            return redirect(url_for("login", role="teacher"))
        if not multi:
            return redirect(url_for("admin_settings"))
        lang = current_lang()
        created, why = registry.create_admin(
            session["admin_group"], request.form.get("email"), request.form.get("password")
        )
        flash(translate(lang, "admin_added" if created else why), "ok" if created else "error")
        return redirect(url_for("admin_settings"))

    @app.post("/admin/team/<int:admin_id>/remove")
    def admin_team_remove(admin_id):
        if not session.get("admin"):
            return redirect(url_for("login", role="teacher"))
        if not multi:
            return redirect(url_for("admin_settings"))
        lang = current_lang()
        row = registry.admin_by_id(admin_id)
        if row is None or row["group_id"] != session.get("admin_group"):
            return redirect(url_for("admin_settings"))
        if admin_id == session.get("admin_id"):
            flash(translate(lang, "e_cannot_remove_self"), "error")
        elif len(registry.admins_of(row["group_id"])) <= 1:
            flash(translate(lang, "e_last_admin"), "error")
        else:
            registry.remove_admin(admin_id)
            flash(translate(lang, "admin_removed"), "ok")
        return redirect(url_for("admin_settings"))

    @app.post("/admin/members/<int:member_id>/password")
    def admin_member_password(member_id):
        if not session.get("admin"):
            return redirect(url_for("login", role="teacher"))
        if not multi:
            return redirect(url_for("admin_member", member_id=member_id))
        lang = current_lang()
        link = registry.user_for_member(session["admin_group"], member_id)
        if link is None:
            flash(translate(lang, "e_no_account"), "error")
        elif registry.set_user_password(link["user_id"], request.form.get("password", "")):
            flash(translate(lang, "password_reset_done"), "ok")
        else:
            flash(translate(lang, "e_password"), "error")
        return redirect(url_for("admin_member", member_id=member_id))

    # -------------------------------------------------------- leave a class
    @app.post("/leave/<int:group_id>")
    def leave_group(group_id):
        user = signed_in_user()
        if user is None:
            return redirect(url_for("login"))
        lang = current_lang()
        registry.leave(user["id"], group_id)
        flash(translate(lang, "left_class"), "ok")
        if session.get("group_id") == group_id:
            session.pop("group_id", None)
            session.pop("member_id", None)
            remaining = registry.groups_for(user["id"])
            if remaining:
                session["group_id"] = remaining[0]["id"]
                session["member_id"] = remaining[0]["member_id"]
                return redirect(url_for("member_home"))
        return redirect(url_for("join_group"))

    # ---- installable web app (works on phones: "Add to Home screen") ----
    @app.get("/manifest.webmanifest")
    def manifest():
        body = {
            "name": studio.studio_name(), "short_name": studio.studio_name()[:12], "start_url": "/me",
            "scope": "/", "display": "standalone", "background_color": "#f4f6f7", "theme_color": "#0f766e",
            "icons": [
                {"src": url_for("static", filename="icon-192.png"), "sizes": "192x192", "type": "image/png"},
                {"src": url_for("static", filename="icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        }
        return Response(json.dumps(body), mimetype="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/offline")
    def offline():
        return render_template("offline.html")

    api.register(app, studio, password_ok, bool(admin_password))
    admin.register(app, studio, password_ok, bool(admin_password))
    portal.register(app, studio, set_user_password=registry.set_user_password if registry else None)
    return app


def ensure_member(registry, studio_for, group, user):
    """Find (or create) this person's member row inside the institution's database."""
    tenant = studio_for(group["id"])
    row = tenant.db.execute("SELECT id FROM members WHERE email = ?", (user["email"],)).fetchone()
    if row:
        return row["id"]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = tenant.db.execute(
        "INSERT INTO members (name, email, code_hash, created_at) VALUES (?, ?, '', ?)",
        (user["name"] or user["email"].split("@")[0], user["email"], stamp),
    )
    tenant.db.commit()
    return cur.lastrowid


def main():
    host = os.environ.get("STUDIOBOOK_HOST", "127.0.0.1")
    create_app().run(host=host, port=int(os.environ.get("PORT", 5000)))


if __name__ == "__main__":
    main()
