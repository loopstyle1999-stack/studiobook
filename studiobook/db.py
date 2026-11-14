import sqlite3

TABLES = """
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    credits INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0)
);
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0)
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES members(id),
    class_id INTEGER NOT NULL REFERENCES classes(id),
    UNIQUE (member_id, class_id)
);
CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES members(id),
    class_id INTEGER NOT NULL REFERENCES classes(id),
    UNIQUE (member_id, class_id)
);
CREATE TABLE IF NOT EXISTS credit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES members(id),
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    credits INTEGER NOT NULL DEFAULT 0,
    valid_days INTEGER NOT NULL DEFAULT 0,
    price INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES members(id),
    plan_id INTEGER,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    price INTEGER NOT NULL DEFAULT 0,
    credits_total INTEGER NOT NULL DEFAULT 0,
    credits_left INTEGER NOT NULL DEFAULT 0,
    starts_on TEXT NOT NULL,
    expires_on TEXT,
    method TEXT NOT NULL DEFAULT 'cash',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS class_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#0f766e',
    duration_min INTEGER NOT NULL DEFAULT 60,
    capacity INTEGER NOT NULL DEFAULT 10,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS instructors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);
"""

# Columns added after v0.2. Databases created by older versions are upgraded in place.
NEW_COLUMNS = {
    "members": {
        "phone": "TEXT NOT NULL DEFAULT ''",
        "notes": "TEXT NOT NULL DEFAULT ''",
        "code_hash": "TEXT NOT NULL DEFAULT ''",
        "created_at": "TEXT NOT NULL DEFAULT ''",
    },
    "classes": {
        "duration_min": "INTEGER NOT NULL DEFAULT 60",
        "instructor": "TEXT NOT NULL DEFAULT ''",
        "series_id": "INTEGER",
        "type_id": "INTEGER",
    },
    "bookings": {
        "status": "TEXT NOT NULL DEFAULT 'booked'",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "purchase_id": "INTEGER",
    },
}


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(TABLES)
    for table, columns in NEW_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    # Early versions accepted "2026-12-01T09:00"; store one format so dates sort correctly.
    conn.execute("UPDATE classes SET starts_at = replace(starts_at, 'T', ' ') WHERE starts_at LIKE '%T%'")
    migrate_credits(conn)
    conn.commit()
    return conn


def migrate_credits(conn):
    """v0.3 kept one credit number per member. From v0.4 credits live in purchases (packs, memberships)."""
    if conn.execute("SELECT 1 FROM settings WHERE key = 'purchases_migrated'").fetchone():
        return
    for member in conn.execute("SELECT id, credits FROM members WHERE credits > 0").fetchall():
        conn.execute(
            "INSERT INTO purchases (member_id, name, kind, credits_total, credits_left, starts_on, method) "
            "VALUES (?, 'Migrated balance', 'pack', ?, ?, '2000-01-01', 'adjust')",
            (member["id"], member["credits"], member["credits"]),
        )
    conn.execute("UPDATE members SET credits = 0")  # legacy column, no longer used
    conn.execute("INSERT INTO settings (key, value) VALUES ('purchases_migrated', '1')")
