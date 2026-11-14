"""Business logic for studiobook. No web code in here, so it is easy to test."""
import csv
import io
import re
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

FMT = "%Y-%m-%d %H:%M"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid mix-ups
HASH_METHOD = "pbkdf2:sha256:100000"
STATUSES = ("booked", "attended", "no_show")
KINDS = ("pack", "unlimited", "dropin")
METHODS = ("cash", "transfer", "card", "other")
DEFAULT_COLOR = "#0f766e"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MEMBER_COLS = "id, name, email, credits AS legacy_credits, phone, notes, created_at"
COUNTS = """
    (SELECT COUNT(*) FROM bookings b WHERE b.class_id = c.id) AS booked,
    (SELECT COUNT(*) FROM bookings b WHERE b.class_id = c.id AND b.status = 'attended') AS attended,
    (SELECT COUNT(*) FROM bookings b WHERE b.class_id = c.id AND b.status = 'no_show') AS no_show,
    (SELECT COUNT(*) FROM waitlist w WHERE w.class_id = c.id) AS waitlist_size
"""
CLASS_SELECT = f"SELECT c.*, t.color AS color, t.name AS type_name, {COUNTS} FROM classes c LEFT JOIN class_types t ON t.id = c.type_id"


def parse_dt(value):
    """Accept 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DDTHH:MM'; return a datetime or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip().replace("T", " ")[:16], FMT)
    except ValueError:
        return None


def err(message, status, code="error", **extra):
    return {"error": message, "code": code, **extra}, status


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class Studio:
    def __init__(self, db, now=None):
        self.db = db
        self.now = now or datetime.now
        self.lock = threading.RLock()
        self._failed_logins = {}

    # ---------------------------------------------------------------- helpers
    def rows(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def row(self, sql, params=()):
        found = self.rows(sql, params)
        return found[0] if found else None

    def today(self):
        return self.now().date()

    def _stamp(self):
        return self.now().strftime("%Y-%m-%d %H:%M:%S")

    def _log(self, member_id, delta, reason, note=""):
        self.db.execute(
            "INSERT INTO credit_log (member_id, delta, reason, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (member_id, delta, reason, note, self._stamp()),
        )

    def _taken(self, class_id):
        return self.db.execute("SELECT COUNT(*) FROM bookings WHERE class_id = ?", (class_id,)).fetchone()[0]

    def _started(self, cls):
        start = parse_dt(cls["starts_at"])
        return start is not None and start <= self.now()

    # --------------------------------------------------------------- settings
    def setting(self, key, default=""):
        found = self.row("SELECT value FROM settings WHERE key = ?", (key,))
        return found["value"] if found else default

    def _int_setting(self, key, default):
        try:
            return max(0, int(self.setting(key, str(default))))
        except ValueError:
            return default

    def cutoff_hours(self):
        return self._int_setting("cancel_cutoff_hours", 0)

    def window_days(self):
        return self._int_setting("booking_window_days", 30) or 30

    def studio_name(self):
        return self.setting("studio_name", "") or "studiobook"

    def currency(self):
        return self.setting("currency", "$")

    def save_settings(self, studio_name, cutoff, window_days=None, currency=None):
        window_days = self.window_days() if window_days is None else window_days
        if not is_int(cutoff) or not 0 <= cutoff <= 168:
            return err("cancel cutoff must be between 0 and 168 hours", 400, "e_cutoff")
        if not is_int(window_days) or not 1 <= window_days <= 365:
            return err("booking window must be between 1 and 365 days", 400, "e_window_setting")
        values = (("studio_name", (studio_name or "").strip()[:60]), ("cancel_cutoff_hours", str(cutoff)),
                  ("booking_window_days", str(window_days)),
                  ("currency", (self.currency() if currency is None else currency).strip()[:6]))
        with self.lock:
            for key, value in values:
                self.db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            self.db.commit()
        return {k: v for k, v in values}, 200

    # --------------------------------------------------- credits and purchases
    def balances(self, on=None):
        """member_id -> {credits, unlimited, until, expires}: what each member can use on a given day."""
        on = on or self.today().isoformat()
        out = {}
        with self.lock:
            found = self.db.execute(
                """SELECT member_id, kind, credits_left, expires_on FROM purchases
                   WHERE status = 'active' AND starts_on <= ? AND (expires_on IS NULL OR expires_on >= ?)""",
                (on, on),
            ).fetchall()
        for r in found:
            b = out.setdefault(r["member_id"], {"credits": 0, "unlimited": False, "until": None, "expires": None})
            if r["kind"] == "unlimited":
                b["unlimited"] = True
                if r["expires_on"] and (b["until"] is None or r["expires_on"] > b["until"]):
                    b["until"] = r["expires_on"]
            elif r["credits_left"] > 0:
                b["credits"] += r["credits_left"]
                if r["expires_on"] and (b["expires"] is None or r["expires_on"] < b["expires"]):
                    b["expires"] = r["expires_on"]
        return out

    @staticmethod
    def _with_balance(member, balance):
        member.update(balance or {"credits": 0, "unlimited": False, "until": None, "expires": None})
        return member

    def _pick_purchase(self, member_id, day):
        """The purchase a booking on `day` is paid with: memberships first, then the pack that expires soonest."""
        return self.db.execute(
            """SELECT * FROM purchases WHERE member_id = ? AND status = 'active' AND starts_on <= ?
                 AND (expires_on IS NULL OR expires_on >= ?) AND (kind = 'unlimited' OR credits_left > 0)
               ORDER BY CASE kind WHEN 'unlimited' THEN 0 ELSE 1 END, expires_on IS NULL, expires_on, id""",
            (member_id, day, day),
        ).fetchone()

    def _charge(self, member_id, day):
        """Use one credit (or a membership day). Returns the purchase row, or None if the member has nothing to pay with."""
        purchase = self._pick_purchase(member_id, day)
        if purchase is None:
            return None
        if purchase["kind"] != "unlimited":
            self.db.execute("UPDATE purchases SET credits_left = credits_left - 1 WHERE id = ?", (purchase["id"],))
        return purchase

    def _refund(self, member_id, purchase_id, reason):
        purchase = self.db.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone() if purchase_id else None
        if purchase is not None and purchase["kind"] == "unlimited":
            self._log(member_id, 0, reason, purchase["name"])
        elif purchase is not None:
            self.db.execute("UPDATE purchases SET credits_left = credits_left + 1 WHERE id = ?", (purchase_id,))
            self._log(member_id, 1, reason)
        else:  # booked before purchases existed: hand the credit back as a small pack
            self.db.execute(
                "INSERT INTO purchases (member_id, name, kind, credits_total, credits_left, starts_on, method, created_at) "
                "VALUES (?, 'Refund', 'pack', 1, 1, '2000-01-01', 'adjust', ?)", (member_id, self._stamp()))
            self._log(member_id, 1, reason)

    def _grant(self, member_id, name, kind, credits, price, start, expires, method, note, plan_id=None):
        cur = self.db.execute(
            "INSERT INTO purchases (member_id, plan_id, name, kind, price, credits_total, credits_left, starts_on, "
            "expires_on, method, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (member_id, plan_id, name, kind, price, credits, credits, start, expires, method, note, self._stamp()),
        )
        return cur.lastrowid

    # ------------------------------------------------------------------ plans
    def list_plans(self, active_only=False):
        return self.rows("SELECT * FROM plans" + (" WHERE active = 1" if active_only else "") + " ORDER BY active DESC, price, id")

    def save_plan(self, plan_id, name, kind, credits, valid_days, price, active=True):
        name = name.strip() if isinstance(name, str) else ""
        if kind == "dropin":
            credits = 1
        if kind == "unlimited":
            credits = 0
        if (not name or kind not in KINDS or not is_int(credits) or not is_int(valid_days) or not is_int(price)
                or price < 0 or valid_days < 0 or (kind == "pack" and credits < 1)
                or (kind == "unlimited" and valid_days < 1)):
            return err("plan needs a name, a type, a price and, for memberships, a length in days", 400, "e_plan_invalid")
        with self.lock:
            if plan_id:
                cur = self.db.execute("UPDATE plans SET name=?, kind=?, credits=?, valid_days=?, price=?, active=? WHERE id=?",
                                      (name, kind, credits, valid_days, price, 1 if active else 0, plan_id))
                if cur.rowcount == 0:
                    return err("plan not found", 404, "e_not_found")
            else:
                cur = self.db.execute("INSERT INTO plans (name, kind, credits, valid_days, price, active) VALUES (?, ?, ?, ?, ?, ?)",
                                      (name, kind, credits, valid_days, price, 1 if active else 0))
                plan_id = cur.lastrowid
            self.db.commit()
        return self.row("SELECT * FROM plans WHERE id = ?", (plan_id,)), 200

    def sell_plan(self, member_id, plan_id, method="cash", price=None, note="", start=None):
        plan = self.row("SELECT * FROM plans WHERE id = ?", (plan_id,))
        if plan is None or self.member(member_id) is None:
            return err("member or plan not found", 404, "e_not_found")
        if method not in METHODS or (price is not None and (not is_int(price) or price < 0)):
            return err("choose a payment method and a price of 0 or more", 400, "e_sale_invalid")
        start_day = parse_dt(f"{start} 00:00").date() if start else self.today()
        expires = (start_day + timedelta(days=plan["valid_days"])).isoformat() if plan["valid_days"] else None
        with self.lock:
            purchase_id = self._grant(member_id, plan["name"], plan["kind"], plan["credits"],
                                      plan["price"] if price is None else price, start_day.isoformat(), expires,
                                      method, str(note or "").strip(), plan["id"])
            self._log(member_id, plan["credits"] if plan["kind"] != "unlimited" else 0, "purchase", plan["name"])
            self.db.commit()
        return {"id": purchase_id, "name": plan["name"], "expires_on": expires}, 201

    def void_purchase(self, purchase_id):
        """Cancel a sale (for example a mistake or a refund). The remaining credits are removed."""
        with self.lock:
            found = self.db.execute("SELECT * FROM purchases WHERE id = ? AND status = 'active'", (purchase_id,)).fetchone()
            if found is None:
                return err("sale not found", 404, "e_not_found")
            self.db.execute("UPDATE purchases SET status = 'void', credits_left = 0 WHERE id = ?", (purchase_id,))
            if found["credits_left"]:
                self._log(found["member_id"], -found["credits_left"], "sale_voided", found["name"])
            self.db.commit()
        return {"id": purchase_id}, 200

    def list_purchases(self, member_id):
        return self.rows("SELECT * FROM purchases WHERE member_id = ? ORDER BY id DESC", (member_id,))

    # ---------------------------------------------------------------- members
    def _new_code(self):
        return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))

    def add_member(self, name, email, credits=0, phone="", notes=""):
        name = name.strip() if isinstance(name, str) else name
        email = email.strip().lower() if isinstance(email, str) else email
        if (not isinstance(name, str) or not name or not isinstance(email, str) or "@" not in email
                or not is_int(credits) or credits < 0):
            return err("name, a valid email and a non-negative integer credits are required", 400, "e_member_invalid")
        code = self._new_code()
        with self.lock:
            try:
                cur = self.db.execute(
                    "INSERT INTO members (name, email, credits, phone, notes, code_hash, created_at) "
                    "VALUES (?, ?, 0, ?, ?, ?, ?)",
                    (name, email, str(phone or "").strip(), str(notes or "").strip(),
                     generate_password_hash(code, method=HASH_METHOD), self._stamp()),
                )
            except sqlite3.IntegrityError:
                self.db.rollback()
                return err("email already registered", 409, "e_email_taken")
            if credits:
                self._grant(cur.lastrowid, "Starting credits", "pack", credits, 0, "2000-01-01", None, "adjust", "")
                self._log(cur.lastrowid, credits, "initial")
            self.db.commit()
        return {"id": cur.lastrowid, "name": name, "email": email, "credits": credits, "access_code": code}, 201

    def member(self, member_id):
        found = self.row(f"SELECT {MEMBER_COLS} FROM members WHERE id = ?", (member_id,))
        if found is None:
            return None
        return self._with_balance(found, self.balances().get(member_id))

    def update_member(self, member_id, name, phone, notes):
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            return err("name is required", 400, "e_name")
        with self.lock:
            cur = self.db.execute("UPDATE members SET name = ?, phone = ?, notes = ? WHERE id = ?",
                                  (name, str(phone or "").strip(), str(notes or "").strip(), member_id))
            self.db.commit()
        if cur.rowcount == 0:
            return err("member not found", 404, "e_not_found")
        return self.member(member_id), 200

    def update_profile(self, member_id, name, phone):
        """What a member may change about themselves (not the studio's private notes)."""
        current = self.member(member_id)
        if current is None:
            return err("member not found", 404, "e_not_found")
        return self.update_member(member_id, name, phone, current["notes"])

    def add_credits(self, member_id, amount, note=""):
        """Give a member extra credits by hand (a gift, a correction). Sell a plan to record a payment."""
        if not is_int(amount) or amount <= 0:
            return err("amount must be a positive integer", 400, "e_amount")
        if self.member(member_id) is None:
            return err("member not found", 404, "e_not_found")
        with self.lock:
            self._grant(member_id, "Credits added", "pack", amount, 0, "2000-01-01", None, "adjust", note)
            self._log(member_id, amount, "admin_add", note)
            self.db.commit()
        return self.member(member_id), 200

    def reset_code(self, member_id):
        code = self._new_code()
        with self.lock:
            cur = self.db.execute("UPDATE members SET code_hash = ? WHERE id = ?",
                                  (generate_password_hash(code, method=HASH_METHOD), member_id))
            self.db.commit()
        if cur.rowcount == 0:
            return err("member not found", 404, "e_not_found")
        return {"access_code": code}, 200

    def set_password(self, member_id, new_password):
        if not isinstance(new_password, str) or len(new_password) < 6:
            return err("password must have at least 6 characters", 400, "e_password")
        with self.lock:
            cur = self.db.execute("UPDATE members SET code_hash = ? WHERE id = ?",
                                  (generate_password_hash(new_password, method=HASH_METHOD), member_id))
            self.db.commit()
        if cur.rowcount == 0:
            return err("member not found", 404, "e_not_found")
        return {}, 200

    def login_member(self, email, code):
        """Return (member_id or None, 'ok' | 'bad' | 'locked'). Five wrong tries lock an email for 10 minutes."""
        key = (email or "").strip().lower()
        now = self.now()
        typed = (code or "").strip()
        with self.lock:
            recent = [t for t in self._failed_logins.get(key, []) if now - t < timedelta(minutes=10)]
            if len(recent) >= 5:
                return None, "locked"
            found = self.db.execute("SELECT id, code_hash FROM members WHERE email = ?", (key,)).fetchone()
            if found and found["code_hash"] and (check_password_hash(found["code_hash"], typed)
                                                 or check_password_hash(found["code_hash"], typed.upper())):
                self._failed_logins.pop(key, None)
                return found["id"], "ok"
            recent.append(now)
            self._failed_logins[key] = recent
        return None, "bad"

    def list_members(self, search="", flt="all"):
        like = f"%{search.strip()}%"
        today = self.today()
        now_s = self.now().strftime(FMT)
        found = self.rows(
            """SELECT m.id, m.name, m.email, m.phone, m.created_at,
                      (SELECT COUNT(*) FROM bookings b JOIN classes c ON c.id = b.class_id
                       WHERE b.member_id = m.id AND c.starts_at >= ?) AS upcoming,
                      (SELECT MAX(c.starts_at) FROM bookings b JOIN classes c ON c.id = b.class_id
                       WHERE b.member_id = m.id AND b.status = 'attended') AS last_visit,
                      (SELECT COUNT(*) FROM bookings b WHERE b.member_id = m.id AND b.status = 'attended') AS visits
               FROM members m WHERE m.name LIKE ? OR m.email LIKE ? OR m.phone LIKE ? ORDER BY m.name""",
            (now_s, like, like, like),
        )
        balances = self.balances()
        out = []
        for m in found:
            self._with_balance(m, balances.get(m["id"]))
            recent = m["last_visit"] or m["created_at"][:10] or "0000-00-00"
            if flt == "low" and (m["unlimited"] or m["credits"] > 1):
                continue
            if flt == "expiring" and not ((m["expires"] and m["expires"] <= (today + timedelta(days=14)).isoformat())
                                          or (m["until"] and m["until"] <= (today + timedelta(days=14)).isoformat())):
                continue
            if flt == "inactive" and recent[:10] >= (today - timedelta(days=30)).isoformat():
                continue
            if flt == "new" and m["created_at"][:10] < (today - timedelta(days=30)).isoformat():
                continue
            out.append(m)
        return out

    def member_detail(self, member_id):
        member = self.member(member_id)
        if member is None:
            return None
        bookings = self.rows(
            """SELECT b.id, b.status, c.id AS class_id, c.title, c.starts_at FROM bookings b
               JOIN classes c ON c.id = b.class_id WHERE b.member_id = ? ORDER BY c.starts_at DESC""", (member_id,))
        waiting = self.rows(
            """SELECT w.id, c.id AS class_id, c.title, c.starts_at FROM waitlist w
               JOIN classes c ON c.id = w.class_id WHERE w.member_id = ? ORDER BY c.starts_at""", (member_id,))
        log = self.rows("SELECT * FROM credit_log WHERE member_id = ? ORDER BY id DESC LIMIT 100", (member_id,))
        stats = {s: sum(1 for b in bookings if b["status"] == s) for s in STATUSES}
        today = self.today().isoformat()
        purchases = self.list_purchases(member_id)
        for p in purchases:
            p["active_now"] = (p["status"] == "active" and p["starts_on"] <= today and (not p["expires_on"] or p["expires_on"] >= today)
                               and (p["kind"] == "unlimited" or p["credits_left"] > 0))
        return {"member": member, "bookings": bookings, "waiting": waiting, "log": log, "stats": stats, "purchases": purchases}

    # ----------------------------------------------- class types, instructors
    def list_types(self, active_only=False):
        return self.rows("SELECT * FROM class_types" + (" WHERE active = 1" if active_only else "") + " ORDER BY active DESC, name")

    def save_type(self, type_id, name, color, duration, capacity, active=True):
        name = name.strip() if isinstance(name, str) else ""
        if (not name or not isinstance(color, str) or not COLOR_RE.match(color) or not is_int(duration)
                or not 5 <= duration <= 600 or not is_int(capacity) or capacity < 1):
            return err("class type needs a name, a colour, a length and a capacity", 400, "e_type_invalid")
        with self.lock:
            try:
                if type_id:
                    cur = self.db.execute("UPDATE class_types SET name=?, color=?, duration_min=?, capacity=?, active=? WHERE id=?",
                                          (name, color, duration, capacity, 1 if active else 0, type_id))
                    if cur.rowcount == 0:
                        return err("class type not found", 404, "e_not_found")
                else:
                    cur = self.db.execute("INSERT INTO class_types (name, color, duration_min, capacity, active) VALUES (?, ?, ?, ?, ?)",
                                          (name, color, duration, capacity, 1 if active else 0))
                    type_id = cur.lastrowid
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                return err("a class type with this name already exists", 409, "e_type_exists")
        return self.row("SELECT * FROM class_types WHERE id = ?", (type_id,)), 200

    def list_instructors(self, active_only=False):
        return self.rows("SELECT * FROM instructors" + (" WHERE active = 1" if active_only else "") + " ORDER BY active DESC, name")

    def add_instructor(self, name):
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            return err("name is required", 400, "e_name")
        with self.lock:
            try:
                cur = self.db.execute("INSERT INTO instructors (name) VALUES (?)", (name,))
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                return err("this instructor already exists", 409, "e_instructor_exists")
        return {"id": cur.lastrowid, "name": name}, 201

    def toggle_instructor(self, instructor_id):
        with self.lock:
            cur = self.db.execute("UPDATE instructors SET active = 1 - active WHERE id = ?", (instructor_id,))
            self.db.commit()
        return ({}, 200) if cur.rowcount else err("instructor not found", 404, "e_not_found")

    # ---------------------------------------------------------------- classes
    @staticmethod
    def _class_problem(title, capacity, duration):
        return (not title or not is_int(capacity) or capacity < 1 or not is_int(duration) or not 5 <= duration <= 600)

    def _type_defaults(self, type_id, title, capacity, duration):
        if type_id:
            t = self.row("SELECT * FROM class_types WHERE id = ?", (type_id,))
            if t:
                return title or t["name"], capacity or t["capacity"], duration or t["duration_min"]
        return title, capacity, duration

    def add_class(self, title, starts_at, capacity, duration=60, instructor="", type_id=None):
        title = title.strip() if isinstance(title, str) else title
        title, capacity, duration = self._type_defaults(type_id, title, capacity, duration)
        start = parse_dt(starts_at)
        if start is None or self._class_problem(title, capacity, duration):
            return err("title, a valid starts_at (YYYY-MM-DD HH:MM) and a positive integer capacity are required",
                       400, "e_class_invalid")
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO classes (title, starts_at, capacity, duration_min, instructor, type_id) VALUES (?, ?, ?, ?, ?, ?)",
                (title, start.strftime(FMT), capacity, duration, str(instructor or "").strip(), type_id or None))
            self.db.commit()
        return {"id": cur.lastrowid, "title": title, "starts_at": start.strftime(FMT), "capacity": capacity,
                "duration_min": duration, "instructor": str(instructor or "").strip()}, 201

    def add_series(self, title, starts_at, capacity, duration, instructor, weekdays, weeks, type_id=None):
        """Create weekly repeating classes: the given weekdays (Mon=0) for `weeks` weeks from the start date."""
        title = title.strip() if isinstance(title, str) else title
        title, capacity, duration = self._type_defaults(type_id, title, capacity, duration)
        start = parse_dt(starts_at)
        if start is None or self._class_problem(title, capacity, duration):
            return err("title, a valid starts_at (YYYY-MM-DD HH:MM) and a positive integer capacity are required",
                       400, "e_class_invalid")
        days = {d for d in weekdays if is_int(d) and 0 <= d <= 6}
        if not days or not is_int(weeks) or not 1 <= weeks <= 52:
            return err("pick at least one weekday and 1 to 52 weeks", 400, "e_series_invalid")
        dates = [start + timedelta(days=i) for i in range(weeks * 7) if (start + timedelta(days=i)).weekday() in days]
        with self.lock:
            series_id = self.db.execute("SELECT COALESCE(MAX(series_id), 0) + 1 FROM classes").fetchone()[0]
            for d in dates:
                self.db.execute(
                    "INSERT INTO classes (title, starts_at, capacity, duration_min, instructor, series_id, type_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (title, d.strftime(FMT), capacity, duration, str(instructor or "").strip(), series_id, type_id or None))
            self.db.commit()
        return {"series_id": series_id, "created": len(dates)}, 201

    def cancel_class(self, class_id):
        """Cancel a whole class: every booked member gets their credit back."""
        with self.lock:
            if self.db.execute("SELECT 1 FROM classes WHERE id = ?", (class_id,)).fetchone() is None:
                return err("class not found", 404, "e_not_found")
            booked = self.db.execute("SELECT member_id, purchase_id FROM bookings WHERE class_id = ?", (class_id,)).fetchall()
            for b in booked:
                self._refund(b["member_id"], b["purchase_id"], "class_cancelled")
            self.db.execute("DELETE FROM waitlist WHERE class_id = ?", (class_id,))
            self.db.execute("DELETE FROM bookings WHERE class_id = ?", (class_id,))
            self.db.execute("DELETE FROM classes WHERE id = ?", (class_id,))
            self.db.commit()
        return {"refunded": len(booked)}, 200

    def list_classes(self, start=None, end=None, type_id=None, instructor=None):
        """Classes with counts. `start`/`end` are 'YYYY-MM-DD HH:MM' strings (end is exclusive)."""
        where, params = [], []
        if start:
            where.append("c.starts_at >= ?")
            params.append(start)
        if end:
            where.append("c.starts_at < ?")
            params.append(end)
        if type_id:
            where.append("c.type_id = ?")
            params.append(type_id)
        if instructor:
            where.append("c.instructor = ?")
            params.append(instructor)
        sql = CLASS_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY c.starts_at, c.id"
        classes = self.rows(sql, params)
        for c in classes:
            c["seats_left"] = c["capacity"] - c["booked"]
            c["color"] = c["color"] or DEFAULT_COLOR
        return classes

    def class_detail(self, class_id):
        found = self.row(CLASS_SELECT + " WHERE c.id = ?", (class_id,))
        if found is None:
            return None
        found["seats_left"] = found["capacity"] - found["booked"]
        found["color"] = found["color"] or DEFAULT_COLOR
        found["started"] = self._started(found)
        bookings = self.rows(
            """SELECT b.id, b.status, b.member_id, m.name, m.email FROM bookings b
               JOIN members m ON m.id = b.member_id WHERE b.class_id = ? ORDER BY b.id""", (class_id,))
        balances = self.balances(found["starts_at"][:10])
        waiting = self.waitlist_for_class(class_id)
        for w in waiting:
            w["credits"] = balances.get(w["member_id"], {}).get("credits", 0)
            w["unlimited"] = balances.get(w["member_id"], {}).get("unlimited", False)
        return {"class": found, "bookings": bookings, "waiting": waiting}

    # --------------------------------------------------------------- bookings
    def _promote(self, class_id):
        """Fill free seats from the waitlist, first come first served. Members who cannot pay keep their place."""
        promoted = []
        cls = self.db.execute("SELECT capacity, starts_at FROM classes WHERE id = ?", (class_id,)).fetchone()
        taken = self._taken(class_id)
        waiting = self.db.execute("SELECT id, member_id FROM waitlist WHERE class_id = ? ORDER BY id", (class_id,)).fetchall()
        for w in waiting:
            if taken >= cls["capacity"]:
                break
            purchase = self._charge(w["member_id"], cls["starts_at"][:10])
            if purchase is None:
                continue
            self.db.execute("INSERT INTO bookings (member_id, class_id, created_at, purchase_id) VALUES (?, ?, ?, ?)",
                            (w["member_id"], class_id, self._stamp(), purchase["id"]))
            self._log(w["member_id"], 0 if purchase["kind"] == "unlimited" else -1, "promoted")
            self.db.execute("DELETE FROM waitlist WHERE id = ?", (w["id"],))
            taken += 1
            promoted.append(w["member_id"])
        return promoted

    def _too_far(self, cls, member_self):
        start = parse_dt(cls["starts_at"])
        return member_self and start is not None and start > self.now() + timedelta(days=self.window_days())

    def book(self, member_id, class_id, member_self=False):
        with self.lock:
            member = self.db.execute("SELECT id FROM members WHERE id = ?", (member_id,)).fetchone()
            cls = self.db.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
            if member is None or cls is None:
                return err("member or class not found", 404, "e_not_found")
            if self._started(cls):
                return err("class already started", 409, "e_started")
            if self._too_far(cls, member_self):
                return err("this class is not open for booking yet", 409, "e_window", days=self.window_days())
            if self.db.execute("SELECT 1 FROM bookings WHERE member_id = ? AND class_id = ?", (member_id, class_id)).fetchone():
                return err("already booked", 409, "e_already_booked")
            if self._taken(class_id) >= cls["capacity"]:
                return err("class is full: join the waitlist", 409, "e_full")
            purchase = self._charge(member_id, cls["starts_at"][:10])
            if purchase is None:
                return err("not enough credits", 402, "e_credits")
            cur = self.db.execute("INSERT INTO bookings (member_id, class_id, created_at, purchase_id) VALUES (?, ?, ?, ?)",
                                  (member_id, class_id, self._stamp(), purchase["id"]))
            self._log(member_id, 0 if purchase["kind"] == "unlimited" else -1, "book", purchase["name"] if purchase["kind"] == "unlimited" else "")
            self.db.commit()
        return {"id": cur.lastrowid, "member_id": member_id, "class_id": class_id}, 201

    def cancel(self, booking_id, member_id=None, refund=None):
        """Cancel a booking.

        Inside the cancellation window (at least `cancel_cutoff_hours` before the start) the credit is refunded.
        Members cannot cancel late. Admins can; the credit is kept unless `refund=True`.
        """
        with self.lock:
            booking = self.db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            if booking is None or (member_id is not None and booking["member_id"] != member_id):
                return err("booking not found", 404, "e_not_found")
            cls = self.db.execute("SELECT * FROM classes WHERE id = ?", (booking["class_id"],)).fetchone()
            start = parse_dt(cls["starts_at"])
            hours_left = (start - self.now()).total_seconds() / 3600 if start else 0
            in_time = hours_left > 0 and hours_left >= self.cutoff_hours()
            if member_id is not None and not in_time:
                return err(f"too late to cancel: classes can be cancelled up to {self.cutoff_hours()} hours before they start",
                           409, "e_late", hours=self.cutoff_hours())
            give_back = in_time if refund is None else bool(refund)
            self.db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
            if give_back:
                self._refund(booking["member_id"], booking["purchase_id"], "cancel_refund")
            else:
                self._log(booking["member_id"], 0, "late_cancel")
            promoted = self._promote(booking["class_id"]) if hours_left > 0 else []
            self.db.commit()
        return {"refunded": give_back, "promoted_member_ids": promoted, "class_id": booking["class_id"]}, 200

    def mark(self, booking_id, status):
        if status not in STATUSES:
            return err("status must be booked, attended or no_show", 400, "e_status")
        with self.lock:
            cur = self.db.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
            self.db.commit()
        if cur.rowcount == 0:
            return err("booking not found", 404, "e_not_found")
        return {"id": booking_id, "status": status}, 200

    # --------------------------------------------------------------- waitlist
    def join_waitlist(self, member_id, class_id, member_self=False):
        with self.lock:
            member = self.db.execute("SELECT id FROM members WHERE id = ?", (member_id,)).fetchone()
            cls = self.db.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
            if member is None or cls is None:
                return err("member or class not found", 404, "e_not_found")
            if self._started(cls):
                return err("class already started", 409, "e_started")
            if self._too_far(cls, member_self):
                return err("this class is not open for booking yet", 409, "e_window", days=self.window_days())
            if self.db.execute("SELECT 1 FROM bookings WHERE member_id = ? AND class_id = ?", (member_id, class_id)).fetchone():
                return err("already booked", 409, "e_already_booked")
            if self.db.execute("SELECT 1 FROM waitlist WHERE member_id = ? AND class_id = ?", (member_id, class_id)).fetchone():
                return err("already on the waitlist", 409, "e_already_waiting")
            if self._taken(class_id) < cls["capacity"]:
                return err("class has free seats: book directly", 409, "e_free_seats")
            cur = self.db.execute("INSERT INTO waitlist (member_id, class_id) VALUES (?, ?)", (member_id, class_id))
            self.db.commit()
            position = self.db.execute("SELECT COUNT(*) FROM waitlist WHERE class_id = ? AND id <= ?",
                                       (class_id, cur.lastrowid)).fetchone()[0]
        return {"id": cur.lastrowid, "member_id": member_id, "class_id": class_id, "position": position}, 201

    def leave_waitlist(self, entry_id, member_id=None):
        sql, params = "DELETE FROM waitlist WHERE id = ?", [entry_id]
        if member_id is not None:
            sql += " AND member_id = ?"
            params.append(member_id)
        with self.lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
        if cur.rowcount == 0:
            return err("waitlist entry not found", 404, "e_not_found")
        return {}, 204

    def waitlist_for_class(self, class_id):
        return self.rows(
            """SELECT w.id, w.member_id, m.name FROM waitlist w
               JOIN members m ON m.id = w.member_id WHERE w.class_id = ? ORDER BY w.id""", (class_id,))

    # ------------------------------------------------- dashboards and schedule
    def dashboard(self):
        today = self.today()
        day = lambda d: f"{d.isoformat()} 00:00"
        today_classes = self.list_classes(day(today), day(today + timedelta(days=1)))
        week = self.list_classes(day(today), day(today + timedelta(days=7)))
        capacity = sum(c["capacity"] for c in week)
        booked = sum(c["booked"] for c in week)
        month = today.strftime("%Y-%m")
        members = self.list_members()
        soon = (today + timedelta(days=7)).isoformat()
        return {
            "today": today,
            "today_classes": today_classes,
            "today_bookings": sum(c["booked"] for c in today_classes),
            "week_classes": len(week),
            "week_fill": round(100 * booked / capacity) if capacity else 0,
            "member_count": len(members),
            "waiting_total": sum(c["waitlist_size"] for c in week),
            "waitlisted_classes": [c for c in week if c["waitlist_size"] > 0],
            "low_credit": sorted([m for m in members if not m["unlimited"] and m["credits"] <= 1],
                                 key=lambda m: (m["credits"], m["name"]))[:8],
            "expiring": sorted([m for m in members if (m["expires"] and m["expires"] <= soon) or (m["until"] and m["until"] <= soon)],
                               key=lambda m: m["expires"] or m["until"])[:8],
            "revenue_month": self.row("SELECT COALESCE(SUM(price), 0) AS n FROM purchases WHERE status = 'active' AND method != 'adjust' AND substr(created_at, 1, 7) = ?", (month,))["n"],
        }

    def week_schedule(self, week_start, type_id=None, instructor=None):
        end = week_start + timedelta(days=7)
        classes = self.list_classes(f"{week_start.isoformat()} 00:00", f"{end.isoformat()} 00:00", type_id, instructor)
        days = []
        for i in range(7):
            d = week_start + timedelta(days=i)
            days.append({"date": d, "classes": [c for c in classes if c["starts_at"][:10] == d.isoformat()]})
        return days

    def portal(self, member_id, days=None):
        """Everything a member sees on their own pages."""
        member = self.member(member_id)
        if member is None:
            return None
        now = self.now()
        days = min(days or 14, self.window_days())
        mine = {r["class_id"]: r["id"] for r in self.rows("SELECT id, class_id FROM bookings WHERE member_id = ?", (member_id,))}
        waiting = {r["class_id"]: r["id"] for r in self.rows("SELECT id, class_id FROM waitlist WHERE member_id = ?", (member_id,))}
        cutoff = self.cutoff_hours()
        upcoming = self.list_classes(now.strftime(FMT), (now + timedelta(days=days)).strftime(FMT))
        for c in upcoming:
            c["my_booking"] = mine.get(c["id"])
            c["my_waitlist"] = waiting.get(c["id"])
            c["can_cancel"] = (parse_dt(c["starts_at"]) - now).total_seconds() / 3600 >= cutoff
        history = self.rows(
            """SELECT b.status, c.title, c.starts_at FROM bookings b JOIN classes c ON c.id = b.class_id
               WHERE b.member_id = ? AND c.starts_at < ? ORDER BY c.starts_at DESC LIMIT 20""",
            (member_id, now.strftime(FMT)))
        log = self.rows("SELECT * FROM credit_log WHERE member_id = ? ORDER BY id DESC LIMIT 15", (member_id,))
        my_bookings = self.rows(
            """SELECT b.id AS booking_id, c.id, c.title, c.starts_at, c.duration_min, c.instructor, t.color
               FROM bookings b JOIN classes c ON c.id = b.class_id LEFT JOIN class_types t ON t.id = c.type_id
               WHERE b.member_id = ? AND c.starts_at >= ? ORDER BY c.starts_at""", (member_id, now.strftime(FMT)))
        for b in my_bookings:
            b["can_cancel"] = (parse_dt(b["starts_at"]) - now).total_seconds() / 3600 >= cutoff
            b["color"] = b["color"] or DEFAULT_COLOR
        plans = [p for p in self.list_purchases(member_id) if p["status"] == "active"]
        today = self.today().isoformat()
        for p in plans:
            p["active_now"] = (p["starts_on"] <= today and (not p["expires_on"] or p["expires_on"] >= today)
                               and (p["kind"] == "unlimited" or p["credits_left"] > 0))
        return {"member": member, "upcoming": upcoming, "my_bookings": my_bookings, "history": history, "log": log,
                "cutoff": cutoff, "window_days": self.window_days(), "plans": plans,
                "types": self.list_types(active_only=True)}

    # ---------------------------------------------------------------- reports
    def report(self, month):
        """Numbers for one month ('YYYY-MM')."""
        try:
            first = datetime.strptime(month + "-01", "%Y-%m-%d").date()
        except ValueError:
            first = self.today().replace(day=1)
        month = first.strftime("%Y-%m")
        days_in_month = ((first.replace(day=28) + timedelta(days=4)).replace(day=1) - first).days
        sales = self.rows("SELECT * FROM purchases WHERE status = 'active' AND method != 'adjust' AND substr(created_at, 1, 7) = ? ORDER BY id DESC", (month,))
        by_plan, daily = {}, {d: 0 for d in range(1, days_in_month + 1)}
        for s in sales:
            entry = by_plan.setdefault(s["name"], {"name": s["name"], "count": 0, "revenue": 0})
            entry["count"] += 1
            entry["revenue"] += s["price"]
            daily[int(s["created_at"][8:10])] += s["price"]
        classes = self.rows(
            f"SELECT c.id, c.title, c.starts_at, c.capacity, c.instructor, t.name AS type_name, {COUNTS} "
            "FROM classes c LEFT JOIN class_types t ON t.id = c.type_id WHERE substr(c.starts_at, 1, 7) = ?", (month,))

        def group(key):
            out = {}
            for c in classes:
                name = key(c) or "-"
                g = out.setdefault(name, {"name": name, "classes": 0, "booked": 0, "capacity": 0, "attended": 0, "no_show": 0})
                g["classes"] += 1
                g["booked"] += c["booked"]
                g["capacity"] += c["capacity"]
                g["attended"] += c["attended"]
                g["no_show"] += c["no_show"]
            rows = sorted(out.values(), key=lambda g: -g["booked"])
            for g in rows:
                g["fill"] = round(100 * g["booked"] / g["capacity"]) if g["capacity"] else 0
            return rows

        weekdays = [0] * 7
        for c in classes:
            weekdays[parse_dt(c["starts_at"]).weekday()] += c["booked"]
        booked = sum(c["booked"] for c in classes)
        capacity = sum(c["capacity"] for c in classes)
        marked = sum(c["attended"] + c["no_show"] for c in classes)
        top = self.rows(
            """SELECT m.id, m.name, COUNT(*) AS visits FROM bookings b JOIN classes c ON c.id = b.class_id
               JOIN members m ON m.id = b.member_id WHERE b.status = 'attended' AND substr(c.starts_at, 1, 7) = ?
               GROUP BY m.id ORDER BY visits DESC, m.name LIMIT 5""", (month,))
        return {
            "month": month, "prev": (first - timedelta(days=1)).strftime("%Y-%m"),
            "next": (first + timedelta(days=days_in_month)).strftime("%Y-%m"),
            "revenue": sum(s["price"] for s in sales), "sales": sales, "sales_count": len(sales),
            "by_plan": sorted(by_plan.values(), key=lambda e: -e["revenue"]),
            "daily": [{"day": d, "amount": a} for d, a in daily.items()],
            "classes": len(classes), "booked": booked, "attended": sum(c["attended"] for c in classes),
            "no_show": sum(c["no_show"] for c in classes),
            "fill": round(100 * booked / capacity) if capacity else 0,
            "no_show_rate": round(100 * sum(c["no_show"] for c in classes) / marked) if marked else 0,
            "by_type": group(lambda c: c["type_name"] or c["title"]), "by_instructor": group(lambda c: c["instructor"]),
            "weekdays": weekdays, "top_members": top,
            "new_members": self.row("SELECT COUNT(*) AS n FROM members WHERE substr(created_at, 1, 7) = ?", (month,))["n"],
        }

    # ---------------------------------------------------------------- exports
    @staticmethod
    def _csv(header, rows):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        writer.writerows(rows)
        return buffer.getvalue()

    def export_members(self):
        return self._csv(["id", "name", "email", "phone", "credits", "unlimited", "upcoming", "visits", "last_visit", "joined"],
                         [[m["id"], m["name"], m["email"], m["phone"], m["credits"], "yes" if m["unlimited"] else "",
                           m["upcoming"], m["visits"], (m["last_visit"] or "")[:10], m["created_at"][:10]] for m in self.list_members()])

    def export_sales(self, month):
        rows = self.rows("""SELECT p.id, p.created_at, m.name, p.name AS plan, p.kind, p.price, p.method, p.starts_on, p.expires_on, p.status, p.note
                            FROM purchases p JOIN members m ON m.id = p.member_id WHERE p.method != 'adjust' AND substr(p.created_at, 1, 7) = ? ORDER BY p.id""", (month,))
        return self._csv(["id", "date", "member", "plan", "type", "price", "method", "starts", "expires", "status", "note"],
                         [[r["id"], r["created_at"], r["name"], r["plan"], r["kind"], r["price"], r["method"], r["starts_on"],
                           r["expires_on"] or "", r["status"], r["note"]] for r in rows])

    def export_bookings(self, month):
        rows = self.rows("""SELECT c.starts_at, c.title, c.instructor, m.name, m.email, b.status FROM bookings b
                            JOIN classes c ON c.id = b.class_id JOIN members m ON m.id = b.member_id
                            WHERE substr(c.starts_at, 1, 7) = ? ORDER BY c.starts_at, m.name""", (month,))
        return self._csv(["class_start", "class", "instructor", "member", "email", "status"],
                         [[r["starts_at"], r["title"], r["instructor"], r["name"], r["email"], r["status"]] for r in rows])
