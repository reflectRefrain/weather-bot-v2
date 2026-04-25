"""
Cooldown guard for Weather Bot v2.

Purpose:
- After a stop-loss, block re-entry into the same ticker/side.
- Keeps the bot from revenge-trading the same bad market.

Safe first version:
- Uses a simple SQLite table.
- Default cooldown: rest of UTC day.
- Executor can ask is_blocked(ticker, side).
"""

import datetime as dt
from db import conn


def nowiso():
    return dt.datetime.utcnow().isoformat()


def end_of_utc_day():
    now = dt.datetime.utcnow()
    end = dt.datetime(now.year, now.month, now.day, 23, 59, 59)
    return end.isoformat()


def init_cooldown_table():
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cooldowns_ticker_side ON cooldowns(ticker, side)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cooldowns_expires ON cooldowns(expires_at)")


def add_cooldown(ticker, side, reason="SL", expires_at=None):
    init_cooldown_table()
    expires_at = expires_at or end_of_utc_day()
    with conn() as c:
        c.execute("""
            INSERT INTO cooldowns(ticker, side, reason, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, (ticker, side, reason, nowiso(), expires_at))
    return {"ticker": ticker, "side": side, "reason": reason, "expires_at": expires_at}


def is_blocked(ticker, side):
    init_cooldown_table()
    now = nowiso()
    with conn() as c:
        row = c.execute("""
            SELECT ticker, side, reason, expires_at
            FROM cooldowns
            WHERE ticker=? AND side=? AND expires_at > ?
            ORDER BY id DESC
            LIMIT 1
        """, (ticker, side, now)).fetchone()
    if not row:
        return None
    return {
        "ticker": row["ticker"],
        "side": row["side"],
        "reason": row["reason"],
        "expires_at": row["expires_at"],
    }


def cleanup_expired():
    init_cooldown_table()
    now = nowiso()
    with conn() as c:
        c.execute("DELETE FROM cooldowns WHERE expires_at <= ?", (now,))


def list_active():
    init_cooldown_table()
    now = nowiso()
    with conn() as c:
        return c.execute("""
            SELECT ticker, side, reason, created_at, expires_at
            FROM cooldowns
            WHERE expires_at > ?
            ORDER BY expires_at ASC
        """, (now,)).fetchall()


def clear_all():
    init_cooldown_table()
    with conn() as c:
        c.execute("DELETE FROM cooldowns")


def main():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        rows = list_active()
        if not rows:
            print("(no active cooldowns)")
        for r in rows:
            print(dict(r))

    elif cmd == "clear":
        clear_all()
        print("cooldowns cleared")

    elif cmd == "test-add":
        add_cooldown("TEST-COOLDOWN", "yes", "SL")
        print("added TEST-COOLDOWN yes")

    elif cmd == "test-check":
        print(is_blocked("TEST-COOLDOWN", "yes"))

    elif cmd == "cleanup":
        cleanup_expired()
        print("expired cooldowns cleaned")

    else:
        print("commands: list, clear, test-add, test-check, cleanup")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
