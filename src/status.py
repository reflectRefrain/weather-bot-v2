"""
bot-status — one-shot CLI health check.

Usage (from inside container):
  python3 src/status.py

Or via docker exec:
  docker exec wb2-trader python3 src/status.py
"""
import datetime as dt
from db import conn, init_db
from executor import get_state, kill_switch_on
from reconcile import open_positions

SEP = "-" * 52


def get_balance_summary():
    try:
        from kalshi_client import KalshiClient
        resp      = KalshiClient().balance()
        free      = resp.get("balance", 0) / 100.0
        portfolio = resp.get("portfolio_value", 0) / 100.0
        return free, portfolio, free + portfolio
    except Exception as e:
        return None, None, None


def get_pnl_summary(days=7):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    with conn() as c:
        rows = c.execute(
            "SELECT realized_usd FROM pnl WHERE date(closed_at) >= ?", (since,)
        ).fetchall()
    if not rows:
        return 0, 0, 0, 0.0
    total    = sum(r["realized_usd"] for r in rows)
    wins     = sum(1 for r in rows if r["realized_usd"] > 0)
    losses   = len(rows) - wins
    win_rate = wins / len(rows) * 100
    return wins, losses, win_rate, total


def get_recent_events(n=8):
    with conn() as c:
        return c.execute(
            "SELECT ts, level, module, message FROM events ORDER BY ts DESC LIMIT ?", (n,)
        ).fetchall()


def main():
    init_db()

    kill = kill_switch_on()
    mode = get_state("mode") or "paper"
    now  = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    print(SEP)
    print(f" Weather Bot v3 — Status  [{now}]")
    print(SEP)

    # Bot state
    status_str = "🔴 PAUSED (kill switch ON)" if kill else "🟢 RUNNING"
    mode_str   = "📄 PAPER" if mode == "paper" else "💰 LIVE"
    print(f" Status  : {status_str}")
    print(f" Mode    : {mode_str}")

    # Balance
    free, portfolio, total = get_balance_summary()
    if total is not None:
        print(f" Balance : ${total:.2f} total  (free=${free:.2f}  portfolio=${portfolio:.2f})")
    else:
        print(" Balance : (could not fetch)")

    # Open positions
    open_pos = open_positions()
    print(f"\n{SEP}")
    print(f" Open Positions ({len(open_pos)})")
    print(SEP)
    if not open_pos:
        print(" (none)")
    else:
        for p in open_pos:
            print(
                f"  {p['ticker'][-28:]:<28}  "
                f"{p['side'].upper():<3}  x{p['qty']:<4}  "
                f"@ {p['avg_price_cents']:>3}c  "
                f"opened {p['opened_at'][11:16]} UTC"
            )

    # PnL (last 7 days)
    wins, losses, win_rate, total_pnl = get_pnl_summary(days=7)
    n_trades = wins + losses
    print(f"\n{SEP}")
    print(f" PnL — last 7 days")
    print(SEP)
    if n_trades == 0:
        print(" No settled trades yet.")
    else:
        print(f"  Trades   : {n_trades}  ({wins}W / {losses}L  {win_rate:.0f}% win rate)")
        print(f"  Total    : ${total_pnl:+.2f}")

    # Today's trades
    today = dt.date.today().isoformat()
    with conn() as c:
        today_rows = c.execute(
            "SELECT ticker, side, realized_usd, reason FROM pnl WHERE date(closed_at) = ?",
            (today,)
        ).fetchall()
    print(f"\n{SEP}")
    print(f" Today’s Settled Trades ({len(today_rows)})")
    print(SEP)
    if not today_rows:
        print(" (none yet)")
    else:
        for r in today_rows:
            icon = "✅" if r["realized_usd"] > 0 else "❌"
            print(f"  {icon} {r['ticker'][-28:]:<28}  {r['side'].upper():<3}  ${r['realized_usd']:+.2f}  [{r['reason']}]")

    # Recent events
    events = get_recent_events()
    print(f"\n{SEP}")
    print(f" Recent Events")
    print(SEP)
    for e in events:
        print(f"  [{e['level']:<5}] {e['ts'][11:19]}  {e['module']:<16}  {e['message'][:52]}")

    print(SEP)


if __name__ == "__main__":
    main()
