"""Pull Kalshi positions and overwrite local DB.
Settlement reconciler: when Kalshi resolves a market, write the outcome
to the pnl table so /pnl shows accurate historical win/loss records.
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


def _already_in_pnl(ticker: str, today: str) -> bool:
    """
    Return True if this ticker already has a FINALIZED pnl entry for today.
    Scoped to today so the same ticker can trade again on a future day.
    settlement_pending entries do NOT count — they will be retried.
    """
    with conn() as c:
        row = c.execute(
            """
            SELECT 1 FROM pnl
            WHERE ticker = ?
              AND date(closed_at) = ?
              AND reason != 'settlement_pending'
            LIMIT 1
            """,
            (ticker, today),
        ).fetchone()
    return row is not None


def _write_settlement_pnl(ticker: str, side: str, qty: int,
                          entry_cents: int, exit_cents: int,
                          reason: str = "settlement"):
    """Write (or overwrite) a Kalshi-settled position to the pnl table."""
    realized = round((exit_cents - entry_cents) * qty / 100.0, 4)
    now = dt.datetime.utcnow().isoformat()
    today = dt.date.today().isoformat()
    with conn() as c:
        # Remove any stale settlement_pending entry for today before inserting
        c.execute(
            "DELETE FROM pnl WHERE ticker=? AND date(closed_at)=? AND reason='settlement_pending'",
            (ticker, today),
        )
        c.execute(
            """
            INSERT OR IGNORE INTO pnl
                (ticker, side, qty, entry_cents, exit_cents, realized_usd, reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, side, qty, entry_cents, exit_cents, realized, reason, now),
        )


def _get_settlement_price(k, ticker: str, side: str) -> int | None:
    """
    Try to determine the exit price for a settled position.
    Strategy:
      1. Check fills API for a settlement fill on this ticker
      2. Check market status: if settled, result is 0 or 100 cents
      3. Fall back to None if we can't determine
    """
    # 1. Try fills API
    try:
        resp = k.fills(ticker=ticker, limit=50)
        fills = resp.get("fills", []) or []
        for f in fills:
            action = f.get("action", "").lower()
            if action in ("settlement", "resolved", "settle"):
                price = f.get("yes_price") or f.get("no_price")
                if price is not None:
                    try:
                        cents = int(round(float(price) * 100)) if float(price) <= 1.0 else int(round(float(price)))
                        return cents if side == "yes" else (100 - cents)
                    except Exception:
                        pass
    except Exception:
        pass

    # 2. Try market status
    try:
        market = k.get_market(ticker)
        m = market.get("market") or market
        status = (m.get("status") or "").lower()
        result = m.get("result") or m.get("yes_sub_title") or ""
        if status in ("finalized", "settled", "resolved"):
            if isinstance(result, str):
                if result.lower() == "yes":
                    return 100 if side == "yes" else 0
                if result.lower() == "no":
                    return 0 if side == "yes" else 100
    except Exception:
        pass

    return None


def _pending_settlements() -> list:
    """Return pnl rows that were written as settlement_pending and need a retry."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM pnl WHERE reason='settlement_pending'"
        ).fetchall()
    return [dict(r) for r in rows]


def sync_from_kalshi(k):
    try:
        resp = k.positions(limit=200)
    except Exception as e:
        return {"error": str(e)}
    positions = resp.get("market_positions", []) or []
    now = dt.datetime.utcnow().isoformat()
    today = dt.date.today().isoformat()
    kept = cleared = settled_written = pending_retried = 0

    # Snapshot open positions BEFORE marking stale (needed for settlement write)
    with conn() as c:
        pre_open = {
            row["ticker"]: dict(row)
            for row in c.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall()
        }

    with conn() as c:
        c.execute("UPDATE positions SET status='STALE' WHERE status='OPEN'")
        for p in positions:
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

    # For each position that just got cleared, try to write settlement PnL
    if cleared > 0:
        with conn() as c:
            just_closed = c.execute(
                "SELECT ticker FROM positions WHERE status='CLOSED'"
            ).fetchall()
        for row in just_closed:
            ticker = row["ticker"]
            if ticker not in pre_open:
                continue
            if _already_in_pnl(ticker, today):
                continue
            pos = pre_open[ticker]
            entry_cents = int(pos.get("avg_price_cents") or 0)
            side = pos.get("side", "yes")
            qty = int(pos.get("qty") or 0)
            if not entry_cents or not qty:
                continue
            exit_cents = _get_settlement_price(k, ticker, side)
            if exit_cents is None:
                # Can't determine outcome yet — write as pending, will retry next cycle
                exit_cents = 0
                reason = "settlement_pending"
            else:
                reason = "settlement"
            _write_settlement_pnl(ticker, side, qty, entry_cents, exit_cents, reason)
            settled_written += 1

    # Retry any previously pending settlements
    for pending in _pending_settlements():
        ticker = pending["ticker"]
        side = pending["side"]
        qty = int(pending["qty"])
        entry_cents = int(pending["entry_cents"])
        exit_cents = _get_settlement_price(k, ticker, side)
        if exit_cents is not None:
            _write_settlement_pnl(ticker, side, qty, entry_cents, exit_cents, "settlement")
            pending_retried += 1

    return {
        "synced": kept,
        "cleared": cleared,
        "settled_written": settled_written,
        "pending_retried": pending_retried,
    }


# Alias expected by main.py
sync = sync_from_kalshi
