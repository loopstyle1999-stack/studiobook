"""Pages for members: log in, browse the schedule, book and cancel, see plans. Works as an installable web app."""
from datetime import date, timedelta
from functools import wraps

from flask import redirect, render_template, request, session, url_for

from .web import current_lang, flash_result, redirect_back, to_int


def register(app, studio):
    def member_page(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            member_id = session.get("member_id")
            if not member_id or studio.member(member_id) is None:
                session.pop("member_id", None)
                return redirect(url_for("member_login"))
            return view(member_id, *args, **kwargs)

        return wrapper

    @app.route("/me/login", methods=["GET", "POST"])
    def member_login():
        """Kept for old links; renders the same unified sign-in page, student tab preselected."""
        error = None
        if request.method == "POST":
            member_id, outcome = studio.login_member(request.form.get("email"), request.form.get("code"))
            if outcome == "ok":
                session["member_id"] = member_id
                session.pop("admin", None)
                return redirect(url_for("member_home"))
            error = "e_locked" if outcome == "locked" else "bad_login"
        return render_template("login.html", role="student", error=error, admin_ready=True)

    @app.post("/me/logout")
    def member_logout():
        session.pop("member_id", None)
        return redirect(url_for("index"))

    @app.get("/me")
    @member_page
    def member_home(member_id):
        data = studio.portal(member_id)
        today = studio.today()
        # 预约窗口
        span = max(int(data["window_days"] or 14), 14)
        chosen = request.args.get("day", today.isoformat())
        try:
            chosen_d0 = date.fromisoformat(chosen)
        except (TypeError, ValueError):
            chosen_d0 = today
        if not (today <= chosen_d0 <= today + timedelta(days=span - 1)):
            chosen = today.isoformat()
        type_id = to_int(request.args.get("type"))
        classes = [c for c in data["upcoming"] if c["starts_at"][:10] == chosen and (not type_id or c["type_id"] == type_id)]

        # 给每节课补上结束时间、满座比例，并按上午/下午/晚上分组
        def enrich(c):
            hh, mm = int(c["starts_at"][11:13]), int(c["starts_at"][14:16])
            end_min = hh * 60 + mm + int(c["duration_min"] or 0)
            c = dict(c)
            c["ends_at"] = f"{(end_min // 60) % 24:02d}:{end_min % 60:02d}"
            taken = (c["capacity"] or 0) - (c["seats_left"] or 0)
            c["fill_pct"] = round(100 * taken / c["capacity"]) if c["capacity"] else 0
            return c

        classes = [enrich(c) for c in classes]
        parts = [("morning", "上午", 0, 12), ("afternoon", "下午", 12, 17), ("evening", "晚上", 17, 24)]
        groups = []
        for key, label, lo, hi in parts:
            sel = [c for c in classes if lo <= int(c["starts_at"][11:13]) < hi]
            if sel:
                groups.append({"key": key, "label": label, "label_key": f"part_{key}", "classes": sel})

        counts = {}
        for c in data["upcoming"]:
            counts[c["starts_at"][:10]] = counts.get(c["starts_at"][:10], 0) + 1
        # 下一个有课的日子（空状态时给个"去看看"）
        next_day = next((k for k in sorted(counts) if k > chosen), None)

        # ---- 月历：可翻月，窗口外的日子灰掉不可点 ----
        window_end = today + timedelta(days=span - 1)
        chosen_d = date.fromisoformat(chosen)
        first = chosen_d.replace(day=1)
        grid_start = first - timedelta(days=first.weekday())
        cells = []
        for i in range(42):
            d = grid_start + timedelta(days=i)
            iso = d.isoformat()
            cells.append({
                "iso": iso, "day": d.day,
                "in_month": d.month == first.month,
                "in_window": today <= d <= window_end,
                "count": counts.get(iso, 0),
                "is_today": d == today,
                "is_on": iso == chosen,
            })
        weeks = [cells[i:i + 7] for i in range(0, 42, 7)]
        while len(weeks) > 4 and all(not c["in_month"] for c in weeks[-1]):
            weeks.pop()
        prev_m = (first - timedelta(days=1)).replace(day=1)
        next_m = (first + timedelta(days=32)).replace(day=1)
        lang = current_lang()
        month_title = (f"{first.year} 年 {first.month} 月" if str(lang).startswith("zh")
                       else first.strftime("%B %Y"))
        wd_zh = ["一", "二", "三", "四", "五", "六", "日"]
        chosen_label = (f"{chosen_d.month} 月 {chosen_d.day} 日 · 周{wd_zh[chosen_d.weekday()]}"
                        if str(lang).startswith("zh") else chosen_d.strftime("%a, %b %d"))
        cal = {
            "title": month_title,
            "chosen_label": chosen_label,
            "chosen_count": counts.get(chosen, 0),
            "weeks": weeks,
            "prev": prev_m.isoformat() if (prev_m + timedelta(days=31)).replace(day=1) > today else None,
            "next": next_m.isoformat() if next_m <= window_end else None,
            "today": today.isoformat(),
        }
        return render_template("me_home.html", d=data, cal=cal, chosen=chosen, classes=classes, groups=groups,
                               counts=counts, next_day=next_day, today_iso=today.isoformat(),
                               type_id=type_id, tab="schedule")

    @app.get("/me/bookings")
    @member_page
    def member_bookings(member_id):
        return render_template("me_bookings.html", d=studio.portal(member_id), tab="bookings")

    @app.get("/me/account")
    @member_page
    def member_account(member_id):
        return render_template("me_account.html", d=studio.portal(member_id), tab="account")

    @app.post("/me/book")
    @member_page
    def member_book(member_id):
        class_id = to_int(request.form.get("class_id"))
        if request.form.get("do") == "waitlist":
            result = studio.join_waitlist(member_id, class_id, member_self=True)
            flash_result(result, "waitlisted_ok", n=result[0].get("position"))
        else:
            flash_result(studio.book(member_id, class_id, member_self=True), "booked_ok")
        return redirect_back("member_home")

    @app.post("/me/cancel/<int:booking_id>")
    @member_page
    def member_cancel(member_id, booking_id):
        flash_result(studio.cancel(booking_id, member_id=member_id), "cancelled_ok")
        return redirect_back("member_home")

    @app.post("/me/leave/<int:entry_id>")
    @member_page
    def member_leave(member_id, entry_id):
        flash_result(studio.leave_waitlist(entry_id, member_id=member_id), "removed_ok")
        return redirect_back("member_home")

    @app.post("/me/profile")
    @member_page
    def member_profile(member_id):
        f = request.form
        flash_result(studio.update_profile(member_id, f.get("name"), f.get("phone")), "member_saved")
        return redirect(url_for("member_account"))

    @app.post("/me/password")
    @member_page
    def member_password(member_id):
        flash_result(studio.set_password(member_id, request.form.get("password", "")), "password_saved")
        return redirect(url_for("member_account"))
