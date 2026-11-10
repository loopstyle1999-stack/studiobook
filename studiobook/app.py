"""studiobook: a small, self-hosted booking and membership app for studios."""
import hmac
import os
import threading
from functools import wraps

from flask import Flask, jsonify, render_template, request

from .db import connect

CLASS_QUERY = """SELECT c.*, c.capacity - COUNT(b.id) AS seats_left
                 FROM classes c LEFT JOIN bookings b ON b.class_id = c.id
                 GROUP BY c.id ORDER BY c.starts_at"""


def create_app(db_path: str = None, admin_password: str = None) -> Flask:
    app = Flask(__name__)
    admin_password = admin_password or os.environ.get("STUDIOBOOK_ADMIN_PASSWORD")
    db = connect(db_path or os.environ.get("STUDIOBOOK_DB", "studiobook.db"))
    lock = threading.Lock()

    def err(message, status):
        return jsonify(error=message), status

    def body():
        return request.get_json(silent=True) or {}

    def admin_required(view):
        """Protect a route with HTTP Basic auth (user "admin")."""

        @wraps(view)
        def wrapper(*args, **kwargs):
            if not admin_password:
                return err("admin is disabled: set STUDIOBOOK_ADMIN_PASSWORD", 503)
            auth = request.authorization
            ok = (
                auth is not None
                and auth.username == "admin"
                and hmac.compare_digest(
                    (auth.password or "").encode("utf-8"), admin_password.encode("utf-8")
                )
            )
            if not ok:
                resp = jsonify(error="authentication required")
                resp.status_code = 401
                resp.headers["WWW-Authenticate"] = 'Basic realm="studiobook admin"'
                return resp
            return view(*args, **kwargs)

        return wrapper

    @app.get("/")
    def index():
        return render_template("index.html", classes=db.execute(CLASS_QUERY).fetchall())

    @app.post("/api/members")
    @admin_required
    def add_member():
        data = body()
        name, email, credits = data.get("name"), data.get("email"), data.get("credits", 0)
        if not name or not email or not isinstance(credits, int) or credits < 0:
            return err("name, email and a non-negative integer credits are required", 400)
        with lock:
            try:
                cur = db.execute(
                    "INSERT INTO members (name, email, credits) VALUES (?, ?, ?)",
                    (name, email, credits),
                )
                db.commit()
            except Exception:
                db.rollback()
                return err("email already registered", 409)
        return jsonify(id=cur.lastrowid, name=name, email=email, credits=credits), 201

    @app.post("/api/members/<int:member_id>/credits")
    @admin_required
    def add_credits(member_id):
        amount = body().get("amount")
        if not isinstance(amount, int) or amount <= 0:
            return err("amount must be a positive integer", 400)
        with lock:
            cur = db.execute(
                "UPDATE members SET credits = credits + ? WHERE id = ?", (amount, member_id)
            )
            db.commit()
            if cur.rowcount == 0:
                return err("member not found", 404)
            row = db.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        return jsonify(dict(row))

    @app.post("/api/classes")
    @admin_required
    def add_class():
        data = body()
        title, starts_at, capacity = data.get("title"), data.get("starts_at"), data.get("capacity")
        if not title or not starts_at or not isinstance(capacity, int) or capacity < 1:
            return err("title, starts_at and a positive integer capacity are required", 400)
        with lock:
            cur = db.execute(
                "INSERT INTO classes (title, starts_at, capacity) VALUES (?, ?, ?)",
                (title, starts_at, capacity),
            )
            db.commit()
        return jsonify(id=cur.lastrowid, title=title, starts_at=starts_at, capacity=capacity), 201

    @app.get("/api/classes")
    def list_classes():
        return jsonify([dict(r) for r in db.execute(CLASS_QUERY).fetchall()])

    @app.post("/api/bookings")
    def book():
        data = body()
        member_id, class_id = data.get("member_id"), data.get("class_id")
        with lock:
            member = db.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
            cls = db.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
            if member is None or cls is None:
                return err("member or class not found", 404)
            if db.execute(
                "SELECT 1 FROM bookings WHERE member_id = ? AND class_id = ?", (member_id, class_id)
            ).fetchone():
                return err("already booked", 409)
            taken = db.execute(
                "SELECT COUNT(*) FROM bookings WHERE class_id = ?", (class_id,)
            ).fetchone()[0]
            if taken >= cls["capacity"]:
                return err("class is full", 409)
            if member["credits"] < 1:
                return err("not enough credits", 402)
            cur = db.execute(
                "INSERT INTO bookings (member_id, class_id) VALUES (?, ?)", (member_id, class_id)
            )
            db.execute("UPDATE members SET credits = credits - 1 WHERE id = ?", (member_id,))
            db.commit()
        return jsonify(id=cur.lastrowid, member_id=member_id, class_id=class_id), 201

    @app.delete("/api/bookings/<int:booking_id>")
    @admin_required
    def cancel(booking_id):
        with lock:
            row = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            if row is None:
                return err("booking not found", 404)
            db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
            db.execute("UPDATE members SET credits = credits + 1 WHERE id = ?", (row["member_id"],))
            db.commit()
        return "", 204

    return app


def main():
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))


if __name__ == "__main__":
    main()