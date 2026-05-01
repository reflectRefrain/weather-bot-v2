"""
Position manager for Weather Bot v3.

Manages PAPER and LIVE positions.
- Reads open positions from SQLite.
- Checks latest market prices from Kalshi.
- Closes positions when TP or SL is hit.
- Writes realized PnL.
- close_settled_positions(): detects Kalshi-settled markets and records final PnL.
  Also force-expires any position whose target date is strictly in the past.
"""

import re
import sys
import datetime as dt
from db import conn
from kalshi_client import KalshiClient
from executor import get_state
from cooldown import add_cooldown


TEST_TICKER = "TEST-POSITION-MANAGER"
TEST_EXIT_PRICE = None

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
)}
RE_TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")


def nowiso():
    return dt.datetime.utcnow().isoformat()


def log_event(level, module, message):
    with conn() as c:
        c.execute(
            "INSERT INTO events(ts,level,module,message) VALUES(?,?,?,?)",
            (nowiso(), level, module, message),
        )


def get_mode():
    return get_state("mode") or "paper"


def ensure_position_columns():
    with conn() as c:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(positions)").fetchall()]
    needed = ["ticker", "side", "qty", "avg_price_cents", "tp_price", "sl_price", "status"]
    missing = [x for x in needed if x not in cols]
    if missing:
        raise RuntimeError("positions table missing columns: " + ", ".join(missing))


def cents(v):
    if v is None or v == "":
        return None
    try:
        if isinstance(v, float) and 0 <= v <= 1:
            return int(round(v * 100))
        return int(round(float(v)))
    except Exception:
        return None


def extract_prices(market):
    yb = cents(market.get("yes_bid"))
    ya = cents(market.get("yes_ask"))
    nb = cents(market.get("no_bid"))
    na = cents(market.get("no_ask"))
    last = cents(market.get("last_price"))

    if yb is None: yb = cents(market.get("yes_bid_dollars"))
    if ya is None: ya = cents(market.get("yes_ask_dollars"))
    if nb is None: nb = cents(market.get("no_bid_dollars"))
    if na is None: na = cents(market.get("no_ask_dollars"))
    if last is None: last = cents(market.get("last_price_dollars"))

    if yb is None and na is not None: yb = 100 - na
    if ya is None and nb is not None: ya = 100 - nb
    if nb is None and ya is not None: nb = 100 - ya
    if na is None and yb is not None: na = 100 - yb

    return yb, ya, nb, na, last


def target_date_from_ticker(tk: str):
    """Parse YYYY-MM-DD target date from a Kalshi ticker string."""
    m = RE_TICKER_DATE.search(tk or "")
    if not m:
        return None
    yy = 2000 + int(m.group(1))
    mo = MONTHS.get(m.group(2))
    dd = int(m.group(3))
    if not mo:
        return None
    try:
        return dt.date(yy, mo, dd)
    except Exception:
        return None


def get_exit_price_for_position(kalshi_client, ticker, side):
    if ticker == TEST_TICKER:
        return None
    resp = kalshi_client.get_market(ticker)
    market = resp.get("market") or resp
    yb, ya, nb, na, last = extract_prices(market)
    if side == "yes":
        return yb if yb is not None else last
    if side == "no":
        return nb if nb is not None else last
    return None


def open_positions():
    with conn() as c:
        return c.execute("""
            SELECT ticker, side, qty, avg_price_cents, tp_price, sl_price, opened_at, status
            FROM positions
            WHERE status='OPEN'
            ORDER BY opened_at ASC
        """).fetchall()


def close_paper_position(row, exit_cents, reason, dry_run=True):
    ticker   = row["ticker"]
    side     = row["side"]
    qty      = int(row["qty"])
    entry    = int(row["avg_price_cents"])
    realized = (int(exit_cents) - entry) * qty / 100.0
    msg = (
        f"{'DRY ' if dry_run else ''}PAPER EXIT {ticker} {side} "
        f"qty={qty} entry={entry}c exit={exit_cents}c pnl=${realized:+.2f} reason={reason}"
    )
    if dry_run:
        log_event("INFO", "position_manager", msg)
        return {"ticker": ticker, "side": side, "qty": qty,
                "entry_cents": entry, "exit_cents": int(exit_cents),
                "realized_usd": realized, "reason": reason, "dry_run": True}
    with conn() as c:
        c.execute("""
            INSERT INTO pnl(ticker,side,qty,entry_cents,exit_cents,realized_usd,closed_at,reason)
            VALUES(?,?,?,?,?,?,?,?)
        """, (ticker, side, qty, entry, int(exit_cents), realized, nowiso(), reason))
        c.execute(
            "UPDATE positions SET status='CLOSED', exit_price_cents=?, exit_reason=? "
            "WHERE ticker=? AND status='OPEN'",
            (int(exit_cents), reason, ticker),
        )
    if reason == "SL":
        cd = add_cooldown(ticker, side, reason="SL")
        log_event("WARN", "cooldown",
                  f"Cooldown added {ticker} {side} until {cd['expires_at']} after SL")
    log_event("INFO", "position_manager", msg)
    return {"ticker": ticker, "side": side, "qty": qty,
            "entry_cents": entry, "exit_cents": int(exit_cents),
            "realized_usd": realized, "reason": reason, "dry_run": False}


def close_live_position(kalshi_client, row, exit_cents, reason, dry_run=True):
    ticker   = row["ticker"]
    side     = row["side"]
    qty      = int(row["qty"])
    entry    = int(row["avg_price_cents"])
    realized = (int(exit_cents) - entry) * qty / 100.0

    if dry_run:
        log_event("INFO", "position_manager",
                  f"DRY LIVE EXIT {ticker} {side} qty={qty} entry={entry}c "
                  f"exit={exit_cents}c pnl=${realized:+.2f} reason={reason}")
        return {"ticker": ticker, "side": side, "qty": qty,
                "entry_cents": entry, "exit_cents": int(exit_cents),
                "realized_usd": realized, "reason": reason, "dry_run": True}

    yes_price = int(exit_cents) if side == "yes" else None
    no_price  = int(exit_cents) if side == "no"  else None
    resp = kalshi_client.create_order(
        ticker=ticker, side=side, action="sell", count=qty, type_="limit",
        yes_price=yes_price, no_price=no_price,
    )
    order_id = resp.get("order", {}).get("order_id", "unknown")
    with conn() as c:
        c.execute("""
            INSERT INTO pnl(ticker,side,qty,entry_cents,exit_cents,realized_usd,closed_at,reason)
            VALUES(?,?,?,?,?,?,?,?)
        """, (ticker, side, qty, entry, int(exit_cents), realized, nowiso(), reason))
        c.execute(
            "UPDATE positions SET status='CLOSED', exit_price_cents=?, exit_reason=? "
            "WHERE ticker=? AND status='OPEN'",
            (int(exit_cents), reason, ticker),
        )
    if reason == "SL":
        cd = add_cooldown(ticker, side, reason="SL")
        log_event("WARN", "cooldown",
                  f"Cooldown added {ticker} {side} until {cd['expires_at']} after SL")
    log_event("INFO", "position_manager",
              f"LIVE EXIT {ticker} {side} qty={qty} entry={entry}c "
              f"exit={exit_cents}c pnl=${realized:+.2f} order_id={order_id} reason={reason}")
    return {"ticker": ticker, "side": side, "qty": qty,
            "entry_cents": entry, "exit_cents": int(exit_cents),
            "realized_usd": realized, "reason": reason,
            "dry_run": False, "order_id": order_id}


def decision_for_position(row, exit_price_cents):
    tp = row["tp_price"]
    sl = row["sl_price"]
    if exit_price_cents is None or exit_price_cents <= 1:
        return "HOLD", "no_exit_price"
    if tp is not None and exit_price_cents >= int(tp):
        return "TP", "take_profit"
    if sl is not None and exit_price_cents <= int(sl):
        return "SL", "stop_loss"
    return "HOLD", "inside_band"


def force_expire_past_positions() -> list:
    """Force-close any OPEN position whose ticker target date is strictly before today.

    These contracts are 100% settled — Kalshi will never update them again.
    We can't know the win/loss without the fills API, so we record exit as None
    and reason as EXPIRED so they don't block the position cap.
    """
    today = dt.date.today()
    results = []
    rows = open_positions()
    for row in rows:
        ticker = row["ticker"]
        tdate  = target_date_from_ticker(ticker)
        if tdate is None or tdate >= today:
            continue
        with conn() as c:
            c.execute(
                "UPDATE positions SET status='CLOSED', exit_reason='EXPIRED' "
                "WHERE ticker=? AND status='OPEN'",
                (ticker,),
            )
        log_event("WARN", "position_manager",
                  f"FORCE_EXPIRED {ticker} — target_date={tdate} is before today={today}")
        results.append({"ticker": ticker, "action": "FORCE_EXPIRED", "target_date": str(tdate)})
    return results


def close_settled_positions(kalshi_client) -> list:
    """Detect Kalshi-settled markets and record final PnL.

    For OPEN same-day or future positions:
      1. Call get_market to check status + result fields.
      2. If settled: payout = 100c (winner) or 0c (loser).
      3. Write to pnl table and mark CLOSED.
    """
    results = []
    today   = dt.date.today()
    rows    = open_positions()

    for row in rows:
        ticker = row["ticker"]
        side   = row["side"]
        qty    = int(row["qty"])
        entry  = int(row["avg_price_cents"])

        tdate = target_date_from_ticker(ticker)
        if tdate is None or tdate > today:
            continue

        try:
            resp   = kalshi_client.get_market(ticker)
            market = resp.get("market") or resp
            status = (market.get("status") or "").lower()
            result = (market.get("result") or "").lower()
        except Exception as e:
            log_event("ERROR", "position_manager",
                      f"settlement check failed {ticker}: {str(e)[:120]}")
            continue

        settled_statuses = {"settled", "finalized", "resolved", "closed"}
        if status not in settled_statuses:
            continue
        if result not in ("yes", "no"):
            continue

        exit_cents = 100 if result == side else 0
        outcome    = "WIN" if result == side else "LOSS"
        realized   = (exit_cents - entry) * qty / 100.0

        with conn() as c:
            existing = c.execute(
                "SELECT 1 FROM pnl WHERE ticker=? AND reason='SETTLEMENT'", (ticker,)
            ).fetchone()
            if existing:
                continue
            c.execute("""
                INSERT INTO pnl(ticker,side,qty,entry_cents,exit_cents,realized_usd,closed_at,reason)
                VALUES(?,?,?,?,?,?,?,?)
            """, (ticker, side, qty, entry, exit_cents, realized, nowiso(), "SETTLEMENT"))
            c.execute(
                "UPDATE positions SET status='CLOSED', exit_price_cents=?, exit_reason=? "
                "WHERE ticker=? AND status='OPEN'",
                (exit_cents, f"SETTLEMENT_{outcome}", ticker),
            )

        log_event("INFO", "position_manager",
                  f"SETTLEMENT {outcome} {ticker} {side} qty={qty} "
                  f"entry={entry}c exit={exit_cents}c pnl=${realized:+.2f} "
                  f"market_result={result}")
        results.append({
            "ticker": ticker, "side": side, "qty": qty,
            "entry_cents": entry, "exit_cents": exit_cents,
            "realized_usd": realized, "outcome": outcome,
            "market_result": result, "action": "SETTLEMENT",
        })

    return results


def manage_positions(kalshi_client=None, dry_run=True):
    ensure_position_columns()
    mode = get_mode()
    if kalshi_client is None:
        kalshi_client = KalshiClient()

    # 1. Force-expire any position whose date is strictly in the past
    expired = force_expire_past_positions()
    results = list(expired)

    # 2. Close any today contracts that Kalshi has already settled
    settlements = close_settled_positions(kalshi_client)
    results.extend(settlements)

    # 3. Manage still-open positions with TP/SL bands
    rows = open_positions()
    for row in rows:
        ticker = row["ticker"]
        side   = row["side"]

        if ticker == TEST_TICKER and TEST_EXIT_PRICE is not None:
            exit_price = int(TEST_EXIT_PRICE)
        elif ticker == TEST_TICKER:
            exit_price = int(row["avg_price_cents"])
        else:
            try:
                exit_price = get_exit_price_for_position(kalshi_client, ticker, side)
            except Exception as e:
                msg = f"price fetch failed {ticker}: {str(e)[:160]}"
                log_event("ERROR", "position_manager", msg)
                results.append({"ticker": ticker, "action": "ERROR", "reason": msg})
                continue

        action, reason = decision_for_position(row, exit_price)
        if action in ("TP", "SL"):
            if mode == "paper":
                result = close_paper_position(row, exit_price, action, dry_run=dry_run)
            else:
                result = close_live_position(kalshi_client, row, exit_price, action, dry_run=dry_run)
            result["action"] = action
            result["reason_detail"] = reason
            results.append(result)
        else:
            results.append({
                "ticker":           ticker,
                "side":             side,
                "action":           "HOLD",
                "reason":           reason,
                "exit_price_cents": exit_price,
                "tp_price":         row["tp_price"],
                "sl_price":         row["sl_price"],
            })

    return results


def print_results(results):
    if not results:
        print("(no open positions)")
        return
    for r in results:
        print(r)


def make_test_position(kind):
    global TEST_EXIT_PRICE
    cleanup_test_position()
    avg, tp, sl = 50, 55, 40
    if kind == "tp":   TEST_EXIT_PRICE = 55
    elif kind == "sl": TEST_EXIT_PRICE = 40
    else:              TEST_EXIT_PRICE = 50
    with conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO positions
                (ticker,side,qty,avg_price_cents,opened_at,tp_price,sl_price,status)
            VALUES (?,?,4,?,?,?,?,'OPEN')
        """, (TEST_TICKER, "yes", avg, nowiso(), tp, sl))
    print(f"created test {kind.upper()} position:", TEST_TICKER, "fake_exit=", TEST_EXIT_PRICE)


def cleanup_test_position():
    global TEST_EXIT_PRICE
    TEST_EXIT_PRICE = None
    with conn() as c:
        c.execute("DELETE FROM positions WHERE ticker=?", (TEST_TICKER,))
        c.execute("DELETE FROM pnl WHERE ticker=?", (TEST_TICKER,))
        c.execute("DELETE FROM events WHERE message LIKE ?", (f"%{TEST_TICKER}%",))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dry-run"
    if cmd == "dry-run":
        print_results(manage_positions(dry_run=True))
    elif cmd == "manage":
        print_results(manage_positions(dry_run=False))
    elif cmd == "test-paper-tp":
        make_test_position("tp")
        print_results(manage_positions(dry_run=False))
    elif cmd == "test-paper-sl":
        make_test_position("sl")
        print_results(manage_positions(dry_run=False))
    elif cmd == "cleanup-test":
        cleanup_test_position()
        print("cleanup done")
    elif cmd == "check-settlements":
        k = KalshiClient()
        print_results(close_settled_positions(k))
    elif cmd == "force-expire":
        print_results(force_expire_past_positions())
    else:
        print("unknown command:", cmd)
        sys.exit(1)


if __name__ == "__main__":
    main()
