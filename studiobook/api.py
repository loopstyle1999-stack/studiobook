"""JSON API. Everything except GET /api/classes needs HTTP Basic auth (user "admin")."""
from functools import wraps

from flask import jsonify, request


def register(app, studio, password_ok, admin_enabled):
    def body():
        return request.get_json(silent=True) or {}

    def respond(result):
        payload, status = result
        if status == 204:
            return "", 204
        return jsonify(payload), status

    def admin_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not admin_enabled:
                return jsonify(error="admin is disabled: set STUDIOBOOK_ADMIN_PASSWORD"), 503
            auth = request.authorization
            if auth is None or auth.username != "admin" or not password_ok(auth.password or ""):
                resp = jsonify(error="authentication required")
                resp.status_code = 401
                resp.headers["WWW-Authenticate"] = 'Basic realm="studiobook admin"'
                return resp
            return view(*args, **kwargs)

        return wrapper

    @app.get("/api/classes")
    def api_list_classes():
        return jsonify(studio.list_classes())

    @app.post("/api/classes")
    @admin_required
    def api_add_class():
        d = body()
        return respond(studio.add_class(d.get("title"), d.get("starts_at"), d.get("capacity"),
                                        d.get("duration_min", 60), d.get("instructor", "")))

    @app.post("/api/members")
    @admin_required
    def api_add_member():
        d = body()
        return respond(studio.add_member(d.get("name"), d.get("email"), d.get("credits", 0),
                                         d.get("phone", ""), d.get("notes", "")))

    @app.post("/api/members/<int:member_id>/credits")
    @admin_required
    def api_add_credits(member_id):
        return respond(studio.add_credits(member_id, body().get("amount")))

    @app.get("/api/plans")
    @admin_required
    def api_plans():
        return jsonify(studio.list_plans())

    @app.post("/api/members/<int:member_id>/purchases")
    @admin_required
    def api_sell(member_id):
        d = body()
        return respond(studio.sell_plan(member_id, d.get("plan_id"), d.get("method", "cash"), d.get("price"), d.get("note", "")))

    @app.post("/api/bookings")
    @admin_required
    def api_book():
        d = body()
        return respond(studio.book(d.get("member_id"), d.get("class_id")))

    @app.delete("/api/bookings/<int:booking_id>")
    @admin_required
    def api_cancel(booking_id):
        return respond(studio.cancel(booking_id))

    @app.post("/api/waitlist")
    @admin_required
    def api_join_waitlist():
        d = body()
        return respond(studio.join_waitlist(d.get("member_id"), d.get("class_id")))

    @app.get("/api/classes/<int:class_id>/waitlist")
    @admin_required
    def api_show_waitlist(class_id):
        if studio.class_detail(class_id) is None:
            return jsonify(error="class not found"), 404
        return jsonify([dict(r, position=i + 1) for i, r in enumerate(studio.waitlist_for_class(class_id))])

    @app.delete("/api/waitlist/<int:entry_id>")
    @admin_required
    def api_leave_waitlist(entry_id):
        return respond(studio.leave_waitlist(entry_id))
