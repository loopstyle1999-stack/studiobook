"""Admin dashboard (browser login with a session cookie)."""
from datetime import date, timedelta
from functools import wraps

from flask import Response, abort, redirect, render_template, request, session, url_for

from .core import METHODS
from .web import flash_result, layout_week, redirect_back, to_int


def register(app, studio, password_ok, admin_enabled):
    def admin_page(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("admin"):
                return redirect(url_for("admin_login"))
            return view(*args, **kwargs)

        return wrapper

    def csv_response(text, filename):
        return Response("\ufeff" + text, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})

    # ------------------------------------------------------------------ login
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        """Kept for old links; renders the same unified sign-in page, teacher tab preselected."""
        error = None
        if request.method == "POST":
            if not admin_enabled:
                error = "admin_disabled"
            elif password_ok(request.form.get("password", "")):
                session["admin"] = True
                session.pop("member_id", None)
                return redirect(url_for("admin_dashboard"))
            else:
                error = "bad_password"
        return render_template("login.html", role="teacher", error=error, admin_ready=admin_enabled)

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("admin", None)
        return redirect(url_for("admin_login"))

    # -------------------------------------------------- dashboard and schedule
    @app.get("/admin")
    @admin_page
    def admin_dashboard():
        return render_template("admin_dashboard.html", d=studio.dashboard(), page="dashboard")

    @app.get("/admin/today")
    @admin_page
    def admin_today():
        today = studio.today()
        classes = studio.list_classes(f"{today.isoformat()} 00:00", f"{(today + timedelta(days=1)).isoformat()} 00:00")
        details = [studio.class_detail(c["id"]) for c in classes]
        return render_template("admin_today.html", details=details, today=today, now=studio.now().strftime("%Y-%m-%d %H:%M"),
                               page="today")

    @app.get("/admin/schedule")
    @admin_page
    def admin_schedule():
        today = studio.today()
        try:
            anchor = date.fromisoformat(request.args.get("week", ""))
        except ValueError:
            anchor = today
        start = anchor - timedelta(days=anchor.weekday())
        type_id, instructor = to_int(request.args.get("type")), request.args.get("instructor") or None
        days = studio.week_schedule(start, type_id, instructor)
        grid = layout_week(days)
        now = studio.now()
        minute = now.hour * 60 + now.minute
        now_top = minute - grid["lo"] * 60 if grid["lo"] * 60 <= minute < grid["hi"] * 60 else None
        return render_template(
            "admin_schedule.html", days=days, grid=grid, start=start, today=today, page="schedule",
            prev_week=start - timedelta(days=7), next_week=start + timedelta(days=7),
            this_week=today - timedelta(days=today.weekday()), types=studio.list_types(active_only=True),
            instructors=studio.list_instructors(active_only=True), type_id=type_id, instructor=instructor,
            now=now.strftime("%Y-%m-%d %H:%M"), now_top=now_top)

    @app.post("/admin/classes")
    @admin_page
    def admin_add_class():
        f = request.form
        type_id = to_int(f.get("type_id"))
        title, starts_at, instructor = f.get("title", "").strip(), f.get("starts_at", ""), f.get("instructor", "").strip()
        capacity = to_int(f.get("capacity"))
        duration = to_int(f.get("duration")) or (None if type_id else 60)
        if f.get("repeat"):
            weekdays = [to_int(d) for d in f.getlist("weekdays")]
            result = studio.add_series(title, starts_at, capacity, duration, instructor, weekdays,
                                       to_int(f.get("weeks") or 0), type_id)
            flash_result(result, "series_added", n=result[0].get("created"))
        else:
            flash_result(studio.add_class(title, starts_at, capacity, duration, instructor, type_id), "class_added")
        return redirect_back("admin_schedule")

    @app.get("/admin/classes/<int:class_id>")
    @admin_page
    def admin_class(class_id):
        detail = studio.class_detail(class_id)
        if detail is None:
            abort(404)
        return render_template("admin_class.html", d=detail, members=studio.list_members(), page="schedule")

    @app.post("/admin/classes/<int:class_id>/cancel")
    @admin_page
    def admin_cancel_class(class_id):
        result = studio.cancel_class(class_id)
        flash_result(result, "class_cancelled", n=result[0].get("refunded"))
        return redirect(url_for("admin_schedule"))

    @app.post("/admin/bookings")
    @admin_page
    def admin_book():
        f = request.form
        member_id, class_id = to_int(f.get("member_id")), to_int(f.get("class_id"))
        if f.get("do") == "waitlist":
            result = studio.join_waitlist(member_id, class_id)
            flash_result(result, "waitlisted_ok", n=result[0].get("position"))
        else:
            flash_result(studio.book(member_id, class_id), "booked_ok")
        return redirect_back("admin_class", class_id=class_id or 0)

    @app.post("/admin/bookings/<int:booking_id>/cancel")
    @admin_page
    def admin_cancel_booking(booking_id):
        refund = {"yes": True, "no": False}.get(request.form.get("refund"))
        result = studio.cancel(booking_id, refund=refund)
        payload = result[0]
        if result[1] < 400:
            ids = payload["promoted_member_ids"]
            if ids:
                marks = ",".join("?" * len(ids))
                names = ", ".join(r["name"] for r in studio.rows(f"SELECT name FROM members WHERE id IN ({marks})", ids))
                flash_result(result, "promoted_ok", names=names)
            else:
                flash_result(result, "cancelled_ok" if payload["refunded"] else "cancelled_no_refund")
            return redirect_back("admin_class", class_id=payload["class_id"])
        flash_result(result, "cancelled_ok")
        return redirect_back("admin_schedule")

    @app.post("/admin/bookings/<int:booking_id>/mark")
    @admin_page
    def admin_mark(booking_id):
        flash_result(studio.mark(booking_id, request.form.get("status")), "marked_ok")
        return redirect_back("admin_schedule")

    @app.post("/admin/waitlist/<int:entry_id>/remove")
    @admin_page
    def admin_remove_waitlist(entry_id):
        flash_result(studio.leave_waitlist(entry_id), "removed_ok")
        return redirect_back("admin_schedule")

    # ---------------------------------------------------------------- members
    @app.get("/admin/members")
    @admin_page
    def admin_members():
        q, flt = request.args.get("q", ""), request.args.get("filter", "all")
        return render_template("admin_members.html", members=studio.list_members(q, flt), q=q, flt=flt,
                               plans=studio.list_plans(active_only=True), page="members")

    @app.post("/admin/members")
    @admin_page
    def admin_add_member():
        f = request.form
        result = studio.add_member(f.get("name", ""), f.get("email", ""), to_int(f.get("credits") or 0),
                                   f.get("phone", ""), f.get("notes", ""))
        flash_result(result, "member_added", code=result[0].get("access_code"))
        if result[1] < 400 and request.form.get("next", "").startswith("/admin") is False:
            return redirect(url_for("admin_member", member_id=result[0]["id"]))
        return redirect_back("admin_members")

    @app.get("/admin/members/<int:member_id>")
    @admin_page
    def admin_member(member_id):
        detail = studio.member_detail(member_id)
        if detail is None:
            abort(404)
        return render_template("admin_member.html", d=detail, plans=studio.list_plans(active_only=True),
                               methods=METHODS, page="members")

    @app.post("/admin/members/<int:member_id>/update")
    @admin_page
    def admin_update_member(member_id):
        f = request.form
        flash_result(studio.update_member(member_id, f.get("name"), f.get("phone"), f.get("notes")), "member_saved")
        return redirect_back("admin_member", member_id=member_id)

    @app.post("/admin/members/<int:member_id>/credits")
    @admin_page
    def admin_add_credits(member_id):
        f = request.form
        flash_result(studio.add_credits(member_id, to_int(f.get("amount")), f.get("note", "").strip()), "credits_added")
        return redirect_back("admin_members")

    @app.post("/admin/members/<int:member_id>/sell")
    @admin_page
    def admin_sell(member_id):
        f = request.form
        price = to_int(f.get("price")) if f.get("price", "").strip() else None
        result = studio.sell_plan(member_id, to_int(f.get("plan_id")), f.get("method", "cash"), price,
                                  f.get("note", ""), f.get("start") or None)
        flash_result(result, "sold_ok", name=result[0].get("name"))
        return redirect_back("admin_member", member_id=member_id)

    @app.post("/admin/purchases/<int:purchase_id>/void")
    @admin_page
    def admin_void_purchase(purchase_id):
        flash_result(studio.void_purchase(purchase_id), "voided_ok")
        return redirect_back("admin_members")

    @app.post("/admin/members/<int:member_id>/code")
    @admin_page
    def admin_reset_code(member_id):
        result = studio.reset_code(member_id)
        flash_result(result, "code_reset", code=result[0].get("access_code"))
        return redirect_back("admin_member", member_id=member_id)

    # ----------------------------------------- catalog: plans, class types, staff
    @app.get("/admin/catalog")
    @admin_page
    def admin_catalog():
        return render_template("admin_catalog.html", plans=studio.list_plans(), types=studio.list_types(),
                               instructors=studio.list_instructors(), page="catalog")

    @app.post("/admin/plans")
    @admin_page
    def admin_save_plan():
        f = request.form
        flash_result(studio.save_plan(to_int(f.get("plan_id")), f.get("name"), f.get("kind"), to_int(f.get("credits") or 0),
                                      to_int(f.get("valid_days") or 0), to_int(f.get("price") or 0), bool(f.get("active"))),
                     "plan_saved")
        return redirect(url_for("admin_catalog"))

    @app.post("/admin/types")
    @admin_page
    def admin_save_type():
        f = request.form
        flash_result(studio.save_type(to_int(f.get("type_id")), f.get("name"), f.get("color", ""), to_int(f.get("duration")),
                                      to_int(f.get("capacity")), bool(f.get("active"))), "type_saved")
        return redirect(url_for("admin_catalog"))

    @app.post("/admin/instructors")
    @admin_page
    def admin_add_instructor():
        flash_result(studio.add_instructor(request.form.get("name")), "instructor_added")
        return redirect(url_for("admin_catalog"))

    @app.post("/admin/instructors/<int:instructor_id>/toggle")
    @admin_page
    def admin_toggle_instructor(instructor_id):
        flash_result(studio.toggle_instructor(instructor_id), "saved_ok")
        return redirect(url_for("admin_catalog"))

    # ---------------------------------------------------- reports and exports
    @app.get("/admin/reports")
    @admin_page
    def admin_reports():
        month = request.args.get("month") or studio.today().strftime("%Y-%m")
        return render_template("admin_reports.html", r=studio.report(month), page="reports")

    @app.get("/admin/export/members.csv")
    @admin_page
    def admin_export_members():
        return csv_response(studio.export_members(), "members.csv")

    @app.get("/admin/export/sales.csv")
    @admin_page
    def admin_export_sales():
        month = request.args.get("month") or studio.today().strftime("%Y-%m")
        return csv_response(studio.export_sales(month), f"sales-{month}.csv")

    @app.get("/admin/export/bookings.csv")
    @admin_page
    def admin_export_bookings():
        month = request.args.get("month") or studio.today().strftime("%Y-%m")
        return csv_response(studio.export_bookings(month), f"bookings-{month}.csv")

    # --------------------------------------------------------------- settings
    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_page
    def admin_settings():
        if request.method == "POST":
            f = request.form
            flash_result(studio.save_settings(f.get("studio_name", ""), to_int(f.get("cutoff")),
                                              to_int(f.get("window_days")), f.get("currency", "")), "settings_saved")
            return redirect(url_for("admin_settings"))
        return render_template("admin_settings.html", cutoff=studio.cutoff_hours(), window_days=studio.window_days(),
                               name=studio.setting("studio_name", ""), page="settings")
