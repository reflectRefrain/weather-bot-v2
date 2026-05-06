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

def _migrate():
    """Add new columns to existing tables without dropping data.
    Called AFTER executescript() so it opens its own fresh connection.
    executescript() issues an implicit COMMIT and can leave the
    original connection in an unusable state for further DDL.
    """
    with conn() as c:
        cols = {r['name'] for r in c.execute("PRAGMA table_info(positions)").fetchall()}
        if "exit_price_cents" not in cols:
            c.execute("ALTER TABLE positions ADD COLUMN exit_price_cents INTEGER")
        if "exit_reason" not in cols:
            c.execute("ALTER TABLE positions ADD COLUMN exit_reason TEXT")

        # model_decisions table — created by executescript above for fresh DBs.
        # For older DBs without it, create it here so migrations are idempotent.
        c.execute("""
            CREATE TABLE IF NOT EXISTS model_decisions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  TEXT NOT NULL,
                cycle_id            TEXT,
                ticker              TEXT NOT NULL,
                city                TEXT,
                variable            TEXT,
                horizon             TEXT,
                target_date         TEXT,
                strike_type         TEXT,
                strike_low          REAL,
                strike_high         REAL,
                forecast_f          REAL,
                obs_f               REAL,
                effective_forecast  REAL,
                sigma_used          REAL,
                model_prob_yes      REAL,
                market_mid          REAL,
                yes_bid             INTEGER,
                yes_ask             INTEGER,
                edge_yes_cents      REAL,
                edge_no_cents       REAL,
                decision            TEXT,
                entry_price_cents   INTEGER,
                settled_high_f      REAL,
                settled_outcome     TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_decisions_ts ON model_decisions(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_decisions_target ON model_decisions(city, target_date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON model_decisions(ticker)")

        # Tier 2: add obs-trajectory projection columns if missing (idempotent).
        md_cols = [r[1] for r in c.execute("PRAGMA table_info(model_decisions)").fetchall()]
        if "projected_high_f" not in md_cols:
            c.execute("ALTER TABLE model_decisions ADD COLUMN projected_high_f REAL")
        if "projection_method" not in md_cols:
            c.execute("ALTER TABLE model_decisions ADD COLUMN projection_method TEXT")

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

        -- Every-cycle decision log. Foundation for empirical sigma calibration,
        -- Brier score reporting, and replay backtest harness.
        -- One row per (cycle, market) we evaluate, traded or not.
        CREATE TABLE IF NOT EXISTS model_decisions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                  TEXT NOT NULL,
            cycle_id            TEXT,
            ticker              TEXT NOT NULL,
            city                TEXT,
            variable            TEXT,
            horizon             TEXT,
            target_date         TEXT,
            strike_type         TEXT,
            strike_low          REAL,
            strike_high         REAL,
            forecast_f          REAL,
            obs_f               REAL,
            projected_high_f    REAL,    -- Tier 2: obs-trajectory quadratic projection (F)
            projection_method   TEXT,    -- 'quadratic' | 'observed_max' | 'latest' | NULL
            effective_forecast  REAL,
            sigma_used          REAL,
            model_prob_yes      REAL,
            market_mid          REAL,
            yes_bid             INTEGER,
            yes_ask             INTEGER,
            edge_yes_cents      REAL,
            edge_no_cents       REAL,
            decision            TEXT,    -- 'enter_yes' | 'enter_no' | 'skip:<reason>'
            entry_price_cents   INTEGER,
            settled_high_f      REAL,    -- backfilled by calibrate script
            settled_outcome     TEXT     -- 'YES' | 'NO' | NULL until resolved
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_ts ON model_decisions(ts);
        CREATE INDEX IF NOT EXISTS idx_decisions_target ON model_decisions(city, target_date);
        CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON model_decisions(ticker);

        INSERT OR IGNORE INTO state(key,value) VALUES('kill_switch','OFF');
        INSERT OR IGNORE INTO state(key,value) VALUES('mode','paper');
        INSERT OR IGNORE INTO state(key,value) VALUES('daily_loss_usd','0');
        INSERT OR IGNORE INTO state(key,value) VALUES('day_start_balance','0');
        """)
    # executescript() issues implicit COMMIT and closes the tx.
    # Run migration on a clean separate connection.
    _migrate()

if __name__ == "__main__":
    init_db()
    print("DB initialized OK")
