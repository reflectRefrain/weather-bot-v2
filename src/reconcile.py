"""Pull Kalshi positions and overwrite local DB.
Also writes pnl records when positions are cleared by reconcile.
"""
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
        # Snapshot open positions before we touch them
        stale_rows = c.execute(
            "SELECT ticker, side, qty, avg_price_cents FROM positions WHERE status='OPEN'"
        ).fetchall()
        stale_map = {r["ticker"]: r for r in stale_rows}

        c.execute("UPDATE positions SET status='STALE' WHERE status='OPEN'")

        for p in positions:
            fp = p.get("position_fp") or p.get("position") or "0"
            try:
                qty = int(round(float(fp)))
            except Exception:
                qty = 0
            if qty == 0:
                continue
            ticker   = p.get("ticker")
            side     = "yes" if qty > 0 else "no"
            absqty   = abs(qty)
            exposure = float(
                p.get("market_exposure_dollars") or p.get("market_exposure") or 0
            )
            avg_cents = int(round(exposure / absqty * 100)) if absqty else 0
            tp = min(99, avg_cents + 15)
            sl = max(1,  avg_cents - 10)
            c.execute(
                "INSERT INTO positions(ticker,side,qty,avg_price_cents,opened_at,tp_price,sl_price,status) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "side=excluded.side, qty=excluded.qty, "
                "avg_price_cents=excluded.avg_price_cents, status='OPEN'",
                (ticker, side, absqty, avg_cents, now, tp, sl, "OPEN"),
            )
            kept += 1

        # Write pnl for every position Kalshi no longer reports
        cleared_rows = c.execute(
            "SELECT ticker, side, qty, avg_price_cents FROM positions WHERE status='STALE'"
        ).fetchall()
        for row in cleared_rows:
            ticker = row["ticker"]
            # Skip if a pnl row already exists for this ticker
            existing = c.execute(
                "SELECT 1 FROM pnl WHERE ticker=?", (ticker,)
            ).fetchone()
            if not existing:
                side_v   = row["side"]
                qty_v    = int(row["qty"])
                entry_v  = int(row["avg_price_cents"])
                # We don't know the exit price at this point —
                # record 0 exit so the loss is captured; settlement checker
                # will correct it if Kalshi returns a result later.
                c.execute("""
                    INSERT INTO pnl
                        (ticker,side,qty,entry_cents,exit_cents,realized_usd,closed_at,reason)
                    VALUES(?,?,?,?,0,?,?,'RECONCILE_CLOSE')
                """, (
                    ticker, side_v, qty_v, entry_v,
                    (0 - entry_v) * qty_v / 100.0,
                    now,
                ))

        result = c.execute(
            "UPDATE positions SET status='CLOSED', exit_reason='RECONCILE_CLOSE' "
            "WHERE status='STALE'"
        )
        cleared = result.rowcount

    return {"synced": kept, "cleared": cleared}


# Alias expected by main.py
sync = sync_from_kalshi
