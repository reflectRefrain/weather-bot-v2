"""
Database — SQLite setup.
Single source of truth for local state.
Kalshi API always overrides this on reconcile.
"""
import sqlite3, os

DB_PATH = os.getenv("DB_PATH", "/app/data/bot.db")

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def _migrate(c):
    """Add new columns to existing tables without dropping data.
    NOTE: PRAGMA table_info rows have row_factory=Row so use r['name'],
    not r[0] (which is the integer cid).
    """
    cols = {r['name'] for r in c.execute("PRAGMA table_info(positions)").fetchall()}
    if "exit_price_cents" not in cols:
        c.execute("ALTER TABLE positions ADD COLUMN exit_price_cents INTEGER")
    if "exit_reason" not in cols:
        c.execute("ALTER TABLE positions ADD COLUMN exit_reason TEXT")

def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            ticker          TEXT PRIMARY KEY,
            series          TEXT,
            city            TEXT,
            variable        TEXT,
            horizon         TEXT,
            target_date     TEXT,
            strike_type     TEXT,
            strike_low      REAL,
            strike_high     REAL,
            yes_bid         INTEGER,
            yes_ask         INTEGER,
            no_bid          INTEGER,
            no_ask          INTEGER,
            last_price      INTEGER,
            resolution_time TEXT,
            status          TEXT DEFAULT 'active',
            last_seen       TEXT
        );

        CREATE TABLE IF NOT EXISTS positions (
            ticker              TEXT PRIMARY KEY,
            side                TEXT,
            qty                 INTEGER,
            avg_price_cents     INTEGER,
            opened_at           TEXT,
            tp_price            INTEGER,
            sl_price            INTEGER,
            status              TEXT DEFAULT 'OPEN',
            exit_price_cents    INTEGER,
            exit_reason         TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_id        TEXT PRIMARY KEY,
            ticker          TEXT,
            side            TEXT,
            action          TEXT,
            qty             INTEGER,
            price_cents     INTEGER,
            status          TEXT DEFAULT 'resting',
            placed_at       TEXT,
            filled_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS pnl (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT,
            side            TEXT,
            qty             INTEGER,
            entry_cents     INTEGER,
            exit_cents      INTEGER,
            realized_usd    REAL,
            closed_at       TEXT,
            reason          TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT,
            level           TEXT,
            module          TEXT,
            message         TEXT
        );

        CREATE TABLE IF NOT EXISTS state (
            key             TEXT PRIMARY KEY,
            value           TEXT
        );

        INSERT OR IGNORE INTO state(key,value) VALUES('kill_switch','OFF');
        INSERT OR IGNORE INTO state(key,value) VALUES('mode','paper');
        INSERT OR IGNORE INTO state(key,value) VALUES('daily_loss_usd','0');
        INSERT OR IGNORE INTO state(key,value) VALUES('day_start_balance','0');
        """)
        # Migrate existing DB to add new columns if upgrading
        _migrate(c)

if __name__ == "__main__":
    init_db()
    print("DB initialized OK")
