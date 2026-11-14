"""Institution registry: accounts, institutions ("groups") and memberships.

Each institution owns its own SQLite database, so the scheduling code in
core.py never has to think about tenants - one Studio instance is one
institution. This module only keeps the shared directory: who signed up,
which institutions exist, who may administer them, and who joined what.
"""
import os
import secrets
import sqlite3
import threading
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

HASH_METHOD = "pbkdf2:sha256:100000"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    join_code TEXT NOT NULL UNIQUE,
    db_file TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS memberships (
    user_id INTEGER NOT NULL REFERENCES users(id),
    group_id INTEGER NOT NULL REFERENCES groups(id),
    member_id INTEGER NOT NULL,
    joined_at TEXT NOT NULL DEFAULT '',
    UNIQUE (user_id, group_id)
);
"""


def new_code(length=6):
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


class Registry:
    def __init__(self, path):
        self.path = path
        self.root = os.path.dirname(os.path.abspath(path))
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- groups
    def groups(self):
        return self.db.execute("SELECT * FROM groups ORDER BY id").fetchall()

    def group_by_id(self, group_id):
        return self.db.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()

    def group_by_code(self, code):
        cleaned = (code or "").strip().upper().replace(" ", "")
        if not cleaned:
            return None
        return self.db.execute("SELECT * FROM groups WHERE join_code = ?", (cleaned,)).fetchone()

    def first_group(self):
        return self.db.execute("SELECT * FROM groups ORDER BY id LIMIT 1").fetchone()

    def tenant_path(self, group):
        return os.path.join(self.root, group["db_file"])

    def create_group(self, name, db_file=None):
        name = (name or "").strip()[:60]
        if not name:
            return None
        if db_file is None:
            os.makedirs(os.path.join(self.root, "tenants"), exist_ok=True)
            db_file = os.path.join("tenants", f"tenant-{secrets.token_hex(6)}.db")
        code = new_code()
        while self.db.execute("SELECT 1 FROM groups WHERE join_code = ?", (code,)).fetchone():
            code = new_code()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO groups (name, join_code, db_file, created_at) VALUES (?, ?, ?, ?)",
                (name, code, db_file, datetime.now().strftime("%Y-%m-%d %H:%M")),
            )
            self.db.commit()
        return self.group_by_id(cur.lastrowid)

    def rename_group(self, group_id, name):
        with self.lock:
            self.db.execute("UPDATE groups SET name = ? WHERE id = ?", ((name or "").strip()[:60], group_id))
            self.db.commit()

    def rotate_code(self, group_id):
        code = new_code()
        while self.db.execute("SELECT 1 FROM groups WHERE join_code = ?", (code,)).fetchone():
            code = new_code()
        with self.lock:
            self.db.execute("UPDATE groups SET join_code = ? WHERE id = ?", (code, group_id))
            self.db.commit()
        return code

    # ----------------------------------------------------------------- users
    def user_by_email(self, email):
        return self.db.execute("SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)).fetchone()

    def create_user(self, name, email, password):
        email = (email or "").strip().lower()
        if "@" not in email:
            return None, "e_email"
        if len(password or "") < 6:
            return None, "e_password"
        if self.user_by_email(email):
            return None, "e_email_taken"
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                ((name or "").strip()[:60] or email.split("@")[0], email,
                 generate_password_hash(password, method=HASH_METHOD),
                 datetime.now().strftime("%Y-%m-%d %H:%M")),
            )
            self.db.commit()
        return self.db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone(), None

    def verify_user(self, email, password):
        user = self.user_by_email(email)
        if user and user["password_hash"] and check_password_hash(user["password_hash"], password or ""):
            return user
        return None

    def set_user_password(self, user_id, password):
        if len(password or "") < 6:
            return False
        with self.lock:
            self.db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                            (generate_password_hash(password, method=HASH_METHOD), user_id))
            self.db.commit()
        return True

    # ---------------------------------------------------------------- admins
    def admin_by_email(self, email):
        return self.db.execute("SELECT * FROM admins WHERE email = ?", ((email or "").strip().lower(),)).fetchone()

    def create_admin(self, group_id, email, password):
        email = (email or "").strip().lower()
        if "@" not in email:
            return None, "e_email"
        if len(password or "") < 6:
            return None, "e_password"
        if self.admin_by_email(email):
            return None, "e_email_taken"
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO admins (group_id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (group_id, email, generate_password_hash(password, method=HASH_METHOD),
                 datetime.now().strftime("%Y-%m-%d %H:%M")),
            )
            self.db.commit()
        return self.db.execute("SELECT * FROM admins WHERE id = ?", (cur.lastrowid,)).fetchone(), None

    def verify_admin(self, email, password):
        admin = self.admin_by_email(email)
        if admin and check_password_hash(admin["password_hash"], password or ""):
            return admin
        return None

    def admin_by_id(self, admin_id):
        return self.db.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()

    def set_admin_email(self, admin_id, email):
        email = (email or "").strip().lower()
        if "@" not in email:
            return "e_email"
        taken = self.admin_by_email(email)
        if taken and taken["id"] != admin_id:
            return "e_email_taken"
        with self.lock:
            self.db.execute("UPDATE admins SET email = ? WHERE id = ?", (email, admin_id))
            self.db.commit()
        return None

    def set_admin_password(self, admin_id, password):
        if len(password or "") < 6:
            return False
        with self.lock:
            self.db.execute("UPDATE admins SET password_hash = ? WHERE id = ?",
                            (generate_password_hash(password, method=HASH_METHOD), admin_id))
            self.db.commit()
        return True

    def remove_admin(self, admin_id):
        with self.lock:
            self.db.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
            self.db.commit()

    def admins_of(self, group_id):
        return self.db.execute("SELECT * FROM admins WHERE group_id = ? ORDER BY id", (group_id,)).fetchall()

    def admin_count(self):
        return self.db.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]

    # ----------------------------------------------------------- memberships
    def membership(self, user_id, group_id):
        return self.db.execute(
            "SELECT * FROM memberships WHERE user_id = ? AND group_id = ?", (user_id, group_id)
        ).fetchone()

    def user_for_member(self, group_id, member_id):
        return self.db.execute(
            "SELECT m.*, u.email, u.name AS user_name FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.group_id = ? AND m.member_id = ?",
            (group_id, member_id),
        ).fetchone()

    def groups_for(self, user_id):
        return self.db.execute(
            "SELECT g.*, m.member_id, m.joined_at FROM memberships m JOIN groups g ON g.id = m.group_id "
            "WHERE m.user_id = ? ORDER BY m.joined_at, g.id",
            (user_id,),
        ).fetchall()

    def add_membership(self, user_id, group_id, member_id):
        with self.lock:
            self.db.execute(
                "INSERT INTO memberships (user_id, group_id, member_id, joined_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, group_id) DO UPDATE SET member_id = excluded.member_id",
                (user_id, group_id, member_id, datetime.now().strftime("%Y-%m-%d %H:%M")),
            )
            self.db.commit()
        return self.membership(user_id, group_id)

    def leave(self, user_id, group_id):
        with self.lock:
            self.db.execute("DELETE FROM memberships WHERE user_id = ? AND group_id = ?", (user_id, group_id))
            self.db.commit()

    def member_count(self, group_id):
        return self.db.execute("SELECT COUNT(*) AS n FROM memberships WHERE group_id = ?", (group_id,)).fetchone()["n"]


def registry_path(db_path):
    """Registry lives next to the main database file."""
    base = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(base, "registry.db")


def seed_from_single_tenant(registry, db_path, admin_password, studio_name):
    """First run: turn the existing single-studio database into the first institution."""
    if registry.first_group() is not None:
        return registry.first_group()
    db_file = os.path.basename(os.path.abspath(db_path))
    group = registry.create_group(studio_name or "Studio", db_file=db_file)
    if admin_password:
        registry.create_admin(group["id"], "admin@local", admin_password)
    return group
