"""Small helpers shared by the web pages."""
from flask import flash, redirect, request, session, url_for

from .i18n import SUPPORTED, pick_language, translate

PIXELS_PER_MINUTE = 1.0


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def current_lang():
    chosen = request.args.get("lang")
    if chosen in SUPPORTED:
        session["lang"] = chosen
    return pick_language(session.get("lang"), request.headers.get("Accept-Language"))


def flash_result(result, ok_key, **fmt):
    """Flash a translated success or error message for a (payload, status) result. True on success."""
    payload, status = result
    lang = current_lang()
    if status >= 400:
        code = payload.get("code", "error")
        try:
            message = translate(lang, code, **payload)
        except KeyError:
            message = payload.get("error", "")
        flash(translate(lang, "error", msg=message), "error")
        return False
    flash(translate(lang, ok_key, **fmt), "ok")
    return True


def redirect_back(default_endpoint, **values):
    """Go back to the page the form came from (same-site /admin or /me paths only)."""
    target = request.form.get("next", "")
    if target.startswith(("/admin", "/me")) and not target.startswith("//"):
        return redirect(target)
    return redirect(url_for(default_endpoint, **values))


def layout_week(days, lo=7, hi=21):
    """Give every class a position on a time grid (one pixel per minute) and side-by-side lanes for overlaps."""
    for day in days:
        for c in day["classes"]:
            start = int(c["starts_at"][11:13]) * 60 + int(c["starts_at"][14:16])
            c["_start"], c["_end"] = start, start + c["duration_min"]
            lo, hi = min(lo, start // 60), max(hi, -(-c["_end"] // 60))
    for day in days:
        cluster, cluster_end = [], -1

        def flush():
            lanes = []
            for c in cluster:
                for i, lane_end in enumerate(lanes):
                    if lane_end <= c["_start"]:
                        lanes[i], c["lane"] = c["_end"], i
                        break
                else:
                    lanes.append(c["_end"])
                    c["lane"] = len(lanes) - 1
            for c in cluster:
                c["lanes"] = len(lanes)

        for c in sorted(day["classes"], key=lambda c: (c["_start"], c["id"])):
            if cluster and c["_start"] >= cluster_end:
                flush()
                cluster, cluster_end = [], -1
            cluster.append(c)
            cluster_end = max(cluster_end, c["_end"])
        if cluster:
            flush()
        for c in day["classes"]:
            c["top"] = round((c["_start"] - lo * 60) * PIXELS_PER_MINUTE)
            c["height"] = max(round(c["duration_min"] * PIXELS_PER_MINUTE), 34)
    return {"lo": lo, "hi": hi, "height": round((hi - lo) * 60 * PIXELS_PER_MINUTE), "hours": list(range(lo, hi))}
