"""
Reconcile — pulls live positions from Kalshi and overwrites local DB.
Kalshi is always the source of truth.
Called at bot startup and every N cycles.
"""
import datetime as dt
from db import conn, init_db

def sync(kalshi_client) -> dict:
    init_db()
    try:
        resp = kalshi_client.positions(limit=200)
    except Exception as e:
        return {"error": str(e), "synced": 0, "cleared": 0}

    positions = resp.get("market_positions", [])

    with conn() as c:
        # Clear all positions then re-insert from Kalshi
        c.execute("DELETE FROM positions")
        kept = 0
        for p in positions:
            qty = p.get("position", 0)
            if qty == 0:
                continue
            ticker = p.get("market_id") or p.get("ticker", "")
            if not ticker:
                continue
            # Kalshi returns avg_price in cents for yes side
            avg_price = p.get("average_price") or 0
            # Determine side from position sign
            side = "yes" if qty > 0 else "no"
            qty  = abs(qty)
            c.execute("""
                INSERT OR REPLACE INTO positions
                    (ticker, side, qty, avg_price_cents, opened_at, status)
                VALUES (?, ?, ?, ?, ?, 'OPEN')
            """, (ticker, side, qty, int(avg_price * 100), dt.datetime.utcnow().isoformat()))
            kept += 1

        # Also cancel any stale resting orders in our DB
        # (Kalshi order state is queried separately in order manager)
        cleared = c.execute(
            "DELETE FROM orders WHERE status='resting' AND "
            "datetime(placed_at) < datetime('now', '-10 minutes')"
        ).rowcount

        c.execute("""
            INSERT OR IGNORE INTO events(ts, level, module, message)
            VALUES (?, 'INFO', 'reconcile', ?)
        """, (dt.datetime.utcnow().isoformat(),
              f"Synced {kept} open positions from Kalshi, cleared {cleared} stale orders"))

    return {"synced": kept, "cleared": cleared, "error": None}


def open_positions(c=None) -> list:
    """Return all OPEN positions from local DB."""
    close_conn = False
    if c is None:
        c = conn()
        close_conn = True
    rows = c.execute(
        "SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at DESC"
    ).fetchall()
    if close_conn:
        c.close()
    return [dict(r) for r in rows]


def position_for(ticker: str) -> dict | None:
    """Return a single open position or None."""
    with conn() as c:
        row = c.execute(
            "SELECT * FROM positions WHERE ticker=? AND status='OPEN'", (ticker,)
        ).fetchone()
    return dict(row) if row else None
