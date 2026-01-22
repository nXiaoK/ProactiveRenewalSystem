import os
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash

DEFAULT_PASSWORD = "123456"

DEFAULT_SETTINGS = {
    "access_password_hash": generate_password_hash(DEFAULT_PASSWORD),
    "default_reminder_days": "7",
    "tg_enabled": "0",
    "tg_bot_token": "",
    "tg_chat_id": "",
    "email_enabled": "0",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_sender": "",
    "smtp_tls": "1",
    "fx_api_url": "https://open.er-api.com/v6/latest/CNY",
    "fx_last_updated": "",
}

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT,
        amount REAL NOT NULL,
        currency TEXT NOT NULL,
        billing_cycle TEXT NOT NULL,
        due_date TEXT NOT NULL,
        renew_url TEXT,
        flow TEXT NOT NULL DEFAULT 'expense',
        reminder_days INTEGER NOT NULL DEFAULT 7,
        enabled INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fx_rates (
        currency TEXT PRIMARY KEY,
        rate_to_cny REAL NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reminder_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subscription_id INTEGER NOT NULL,
        due_date TEXT NOT NULL,
        channel TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        UNIQUE(subscription_id, due_date, channel)
    )
    """,
]


def get_data_dir():
    return os.environ.get("APP_DATA_DIR", os.path.join(os.getcwd(), "data"))


def get_db_path():
    return os.path.join(get_data_dir(), "app.db")


def connect_db():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(get_data_dir(), exist_ok=True)
    db = connect_db()
    cur = db.cursor()
    for statement in SCHEMA_STATEMENTS:
        cur.execute(statement)
    db.commit()
    ensure_migrations(db)
    ensure_default_settings(db)
    db.close()


def ensure_migrations(db):
    ensure_column(db, "subscriptions", "flow", "TEXT NOT NULL DEFAULT 'expense'")


def ensure_column(db, table, column, definition):
    columns = [row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column in columns:
        return
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.commit()


def ensure_default_settings(db):
    cur = db.cursor()
    existing = {row["key"] for row in cur.execute("SELECT key FROM settings").fetchall()}
    now = datetime.utcnow().isoformat()
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
    if "fx_last_updated" not in existing:
        cur.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("fx_last_updated", now),
        )
    db.commit()


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    db.commit()


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()
