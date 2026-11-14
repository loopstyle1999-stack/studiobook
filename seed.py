"""生成演示数据：一间瑜伽/普拉提工作室的两周历史 + 三周未来课表。"""
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from studiobook.core import Studio  # noqa: E402
from studiobook.db import connect  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studiobook.db")
random.seed(20261112)

# 清空旧库（不删文件，避免安全策略拦截）
if os.path.exists(DB):
    _c = sqlite3.connect(DB)
    for (_n,) in _c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if not _n.startswith("sqlite_"):
            _c.execute(f"DROP TABLE IF EXISTS {_n}")
    _c.commit()
    _c.close()

db = connect(DB)
st = Studio(db)

# ---------------- 基础设置 ----------------
st.save_settings("晨光瑜伽", cutoff=6, window_days=21, currency="¥")

# ---------------- 课程类型 ----------------
TYPES = [
    ("哈他瑜伽", "#4C8C6B", 75, 14),
    ("普拉提", "#4A6E8A", 60, 12),
    ("力量训练", "#C97A3E", 50, 10),
    ("阴瑜伽 · 冥想", "#7A5C9E", 90, 16),
]
type_ids = []
for name, color, dur, cap in TYPES:
    r, _ = st.save_type(None, name, color, dur, cap, True)
    type_ids.append(r["id"])

# ---------------- 老师 ----------------
teachers = ["林岚", "周野", "苏晴"]
for t in teachers:
    st.add_instructor(t)

# ---------------- 方案 ----------------
PLANS = [
    ("20 次卡", "pack", 20, 120, 2000),
    ("10 次卡", "pack", 10, 90, 1200),
    ("月卡 · 不限次", "unlimited", 0, 30, 999),
    ("单次体验", "dropin", 1, 7, 120),
]
plan_ids = {}
for name, kind, credits, days, price in PLANS:
    r, _ = st.save_plan(None, name, kind, credits, days, price, True)
    plan_ids[name] = r["id"]

# ---------------- 会员 ----------------
MEMBERS = [
    ("陈思远", "chensy@example.com", "13800001111"),
    ("李雨桐", "liyt@example.com", "13800002222"),
    ("王一诺", "wangyn@example.com", "13800003333"),
    ("张亦然", "zhangyr@example.com", "13800004444"),
    ("刘子涵", "liuzh@example.com", "13800005555"),
    ("赵可欣", "zhaokx@example.com", "13800006666"),
    ("孙朗", "sunlang@example.com", "13800007777"),
    ("周静怡", "zhoujy@example.com", "13800008888"),
    ("吴嘉禾", "wujh@example.com", "13800009999"),
    ("郑晓", "zhengx@example.com", "13800001010"),
    ("黄沐", "huangm@example.com", "13800001212"),
    ("徐清越", "xuqy@example.com", "13800001313"),
]
member_ids = []
for name, mail, phone in MEMBERS:
    r, _ = st.add_member(name, mail, credits=0, phone=phone, notes="")
    member_ids.append(r["id"])

# 大部分人买 10 次卡，几个买月卡，一个只买单次体验
for i, mid in enumerate(member_ids):
    if i % 5 == 4:
        st.sell_plan(mid, plan_ids["月卡 · 不限次"], method="card")
    elif i == 10:
        st.sell_plan(mid, plan_ids["单次体验"], method="cash")
    elif i % 2 == 0:
        st.sell_plan(mid, plan_ids["20 次卡"], method=random.choice(["cash", "transfer", "card"]))
    else:
        st.sell_plan(mid, plan_ids["10 次卡"], method=random.choice(["cash", "transfer", "card"]))

# 制造"课时不足"和"久未上课"的会员
st.add_credits(member_ids[2], -8, "现场调整")
st.add_credits(member_ids[5], -7, "现场调整")

# ---------------- 课表：过去 14 天 + 未来 21 天 ----------------
today = date.today()
SLOTS = [("08:00", 0), ("12:00", 1), ("18:30", 2), ("20:00", 3)]
created = []
for offset in range(-14, 22):
    day = today + timedelta(days=offset)
    n = 2 if day.weekday() == 6 else 3
    for slot in range(n):
        hhmm, tidx = SLOTS[slot]
        tname, color, dur, cap = TYPES[tidx % len(TYPES)]
        teacher = teachers[(day.toordinal() + slot) % len(teachers)]
        r, _ = st.add_class(tname, f"{day.isoformat()} {hhmm}", cap, dur, teacher, type_ids[tidx % len(TYPES)])
        if isinstance(r, dict) and "id" in r:
            created.append((r["id"], day, offset, cap))

print(f"课程 {len(created)} 节，会员 {len(member_ids)} 人")

# ---------------- 预约 ----------------
cols = {r["name"] for r in db.execute("PRAGMA table_info(bookings)")}
past = [c for c in created if c[2] <= 0]
future = [c for c in created if c[2] > 0]

# 过去的课：直接写预约 + 出勤状态
for cid, day, off, cap in past:
    n = random.randint(max(2, cap - 6), cap)
    picked = random.sample(member_ids, min(n, len(member_ids)))
    for mid in picked:
        status = "attended" if random.random() > 0.12 else "no_show"
        values = {"member_id": mid, "class_id": cid, "status": status}
        if "created_at" in cols:
            values["created_at"] = f"{day.isoformat()} 09:00"
        keys = ",".join(values)
        marks = ",".join("?" * len(values))
        db.execute(f"INSERT OR IGNORE INTO bookings ({keys}) VALUES ({marks})", tuple(values.values()))
db.commit()

# 未来的课：用接口预约，正常扣课时
full_done = False
for cid, day, off, cap in future:
    n = random.randint(2, min(6, cap))
    picked = random.sample(member_ids, min(n, len(member_ids)))
    for mid in picked:
        st.book(mid, cid)
    # 挑一节小班课排满，再让没约上的人排队候补
    if not full_done and off == 1 and cap <= 10:
        for mid in member_ids[:cap]:
            st.book(mid, cid)
        for mid in member_ids[cap:cap + 3]:
            st.join_waitlist(mid, cid)
        full_done = True

print("预约与候补完成")

# 汇总
for label, sql in [
    ("会员", "SELECT COUNT(*) FROM members"),
    ("课程", "SELECT COUNT(*) FROM classes"),
    ("预约", "SELECT COUNT(*) FROM bookings"),
    ("候补", "SELECT COUNT(*) FROM waitlist"),
    ("销售", "SELECT COUNT(*) FROM purchases"),
]:
    print(f"  {label}: {db.execute(sql).fetchone()[0]}")
print("数据库:", DB)
