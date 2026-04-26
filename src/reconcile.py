"""Pull Kalshi positions and overwrite local DB."""
import datetime as dt
from db import conn


def position_for(ticker: str):
    """Return open position row for ticker, or None."""
    with conn() as c:
        row = c.execute(
            "SELECT * FROM positions WHERE ticker=? AND status='OPEN' LIMIT 1",
            (ticker,)
        ).fetchone()
    return dict(row) if row else None


def open_positions() -> list:
    """Return all open positions."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM positions WHERE status='OPEN'"
        ).fetchall()
    return [dict(r) for r in rows]


def sync_from_kalshi(k):
    try:
        resp = k.positions(limit=200)
    except Exception as e:
        return {"error": str(e)}
    positions = resp.get("market_positions", []) or []
    now = dt.datetime.utcnow().isoformat()
    kept = cleared = 0
    with conn() as c:
        c.execute("UPDATE positions SET status='STALE' WHERE status='OPEN'")
        for p in positions:
            # Kalshi returns position_fp (fixed-point string), not "position"
            fp = p.get("position_fp") or p.get("position") or "0"
            try:
                qty = int(round(float(fp)))
            except Exception:
                qty = 0
            if qty == 0:
                continue
            ticker = p.get("ticker")
            side = "yes" if qty > 0 else "no"
            absqty = abs(qty)
            exposure = float(
                p.get("market_exposure_dollars") or p.get("market_exposure") or 0
            )
            avg_cents = int(round(exposure / absqty * 100)) if absqty else 0
            tp = min(99, avg_cents + 15)
            sl = max(1, avg_cents - 10)
            c.execute(
                "INSERT INTO positions(ticker,side,qty,avg_price_cents,opened_at,tp_price,sl_price,status) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "side=excluded.side, qty=excluded.qty, "
                "avg_price_cents=excluded.avg_price_cents, status='OPEN'",
                (ticker, side, absqty, avg_cents, now, tp, sl, "OPEN"),
            )
            kept += 1
        result = c.execute(
            "UPDATE positions SET status='CLOSED' WHERE status='STALE'"
        )
        cleared = result.rowcount
    return {"synced": kept, "cleared": cleared}


# Alias expected by main.py
sync = sync_from_kalshi
