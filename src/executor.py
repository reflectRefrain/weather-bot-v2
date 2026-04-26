"""
Executor — places and cancels orders with full risk guards.
RULES ENFORCED HERE:
  1. Never enter same ticker twice (no accumulation bug)
  2. Hard position cap
  3. Hard per-ticker dollar cap
  4. Daily loss limit auto-kill
  5. Min balance guard
  6. Kill switch check before every order
  7. Cancel stale resting orders automatically
  8. Block entry if resting order already exists for same ticker
"""
import datetime as dt, math, yaml, os
from db import conn, init_db
from reconcile import position_for, open_positions
from cooldown import is_blocked

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yaml")

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def log_event(level, module, message):
    with conn() as c:
        c.execute(
            "INSERT INTO events(ts,level,module,message) VALUES(?,?,?,?)",
            (dt.datetime.utcnow().isoformat(), level, module, message)
        )

def get_state(key):
    with conn() as c:
        row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None

def set_state(key, value):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, str(value)))

def kill_switch_on():
    return get_state("kill_switch") == "ON"

def set_kill_switch(on: bool):
    set_state("kill_switch", "ON" if on else "OFF")
    log_event("WARN" if on else "INFO", "executor",
              "Kill switch " + ("ACTIVATED" if on else "RELEASED"))

def daily_loss_check(kalshi_client, cfg) -> bool:
    """Returns True if daily loss limit is breached — triggers kill switch."""
    try:
        bal_resp = kalshi_client.balance()
        balance  = bal_resp.get("balance", 0) / 100.0
        day_start = float(get_state("day_start_balance") or balance)
        loss      = day_start - balance
        limit     = cfg["bankroll_usd"] * cfg["risk"]["daily_loss_limit_pct"]
        if loss >= limit:
            log_event("WARN", "executor",
                      f"Daily loss limit hit: lost ${loss:.2f} vs limit ${limit:.2f}")
            set_kill_switch(True)
            return True
        min_bal = cfg["risk"]["min_balance_usd"]
        if balance < min_bal:
            log_event("WARN", "executor",
                      f"Balance ${balance:.2f} below minimum ${min_bal:.2f}")
            set_kill_switch(True)
            return True
    except Exception as e:
        log_event("ERROR", "executor", f"daily_loss_check failed: {e}")
    return False

def resting_order_for(ticker: str) -> bool:
    """Returns True if there is already a resting order for this ticker."""
    with conn() as c:
        row = c.execute(
            "SELECT order_id FROM orders WHERE ticker=? AND status='resting' LIMIT 1",
            (ticker,)
        ).fetchone()
    return row is not None

def can_enter(ticker: str, cfg: dict) -> tuple[bool, str]:
    """
    Returns (True, '') if safe to enter, or (False, reason) if blocked.
    """
    if kill_switch_on():
        return False, "kill_switch_ON"

    existing = position_for(ticker)
    if existing:
        return False, f"already_open:{ticker}"

    if resting_order_for(ticker):
        return False, f"resting_order_exists:{ticker}"

    open_pos = open_positions()
    max_pos  = cfg["risk"]["max_open_positions"]
    if len(open_pos) >= max_pos:
        return False, f"position_cap:{len(open_pos)}/{max_pos}"

    return True, ""

def place_order(kalshi_client, candidate: dict, cfg: dict) -> dict:
    """
    Full guarded order placement.
    candidate comes from market_scanner.scan()
    Returns dict with success, order_id, reason
    """
    init_db()
    ticker  = candidate["ticker"]
    side    = candidate["side"]
    price_c = candidate["price_cents"]
    p       = candidate["model_prob"]

    # Compute Kelly sizing inline
    bankroll = float(cfg.get("bankroll_usd", 100))
    b        = (100 - price_c) / price_c if price_c < 100 else 0.0
    f_raw    = max(0.0, (p * b - (1 - p)) / b) if b > 0 else 0.0
    f_scaled = f_raw * float(cfg["risk"].get("kelly_fraction", 0.25))
    kelly_usd = min(
        f_scaled * bankroll,
        float(cfg["risk"].get("max_trade_usd", 2.0)),
        float(cfg["risk"].get("max_per_ticker_usd", 2.0))
    )
    kelly_usd = max(kelly_usd, float(cfg["risk"].get("min_trade_usd", 1.0)))

    # Pre-flight checks
    blocked = is_blocked(ticker, side)
    if blocked:
        reason = f"cooldown_until:{blocked['expires_at']}"
        log_event("WARN", "cooldown", f"SKIP {ticker} {side} — {reason}")
        return {"success": False, "reason": reason}

    ok, reason = can_enter(ticker, cfg)
    if not ok:
        log_event("INFO", "executor", f"SKIP {ticker} — {reason}")
        return {"success": False, "reason": reason}

    if daily_loss_check(kalshi_client, cfg):
        return {"success": False, "reason": "daily_loss_limit"}

    # Calculate qty — never less than 1
    qty = max(1, math.floor((kelly_usd * 100) / price_c))

    # Hard dollar cap check
    cost_usd = qty * price_c / 100.0
    max_usd  = cfg["risk"]["max_per_ticker_usd"]
    if cost_usd > max_usd:
        qty      = max(1, math.floor((max_usd * 100) / price_c))
        cost_usd = qty * price_c / 100.0

    mode = get_state("mode") or cfg.get("mode", "paper")

    if mode == "paper":
        log_event("INFO", "executor",
                  f"PAPER BUY {ticker} {side} x{qty} @ {price_c}c = ${cost_usd:.2f}")
        tp = min(99, price_c + max(10, int(round(candidate["edge_cents"]))))
        sl = max(1,  price_c - max(5,  int(round(candidate["edge_cents"] * 0.5))))
        with conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO positions
                    (ticker, side, qty, avg_price_cents, opened_at, tp_price, sl_price, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """, (ticker, side, qty, price_c, dt.datetime.utcnow().isoformat(), tp, sl))
        return {"success": True, "order_id": f"paper-{ticker}", "qty": qty,
                "cost_usd": cost_usd, "mode": "paper"}

    # Live mode
    try:
        yes_price = price_c if side == "yes" else None
        no_price  = price_c if side == "no"  else None
        resp = kalshi_client.create_order(
            ticker=ticker, side=side, action="buy", count=qty,
            type_="limit", yes_price=yes_price, no_price=no_price
        )
        order_id = resp.get("order", {}).get("order_id", "unknown")
        with conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO orders
                    (order_id, ticker, side, action, qty, price_cents, status, placed_at)
                VALUES (?, ?, ?, 'buy', ?, ?, 'resting', ?)
            """, (order_id, ticker, side, qty, price_c, dt.datetime.utcnow().isoformat()))
        log_event("INFO", "executor",
                  f"LIVE BUY {ticker} {side} x{qty} @ {price_c}c order_id={order_id}")
        return {"success": True, "order_id": order_id, "qty": qty,
                "cost_usd": cost_usd, "mode": "live"}
    except Exception as e:
        log_event("ERROR", "executor", f"create_order failed {ticker}: {e}")
        return {"success": False, "reason": str(e)}

def cancel_stale_orders(kalshi_client, cfg: dict):
    """Cancel any resting orders older than cancel_after_seconds."""
    max_age = cfg["risk"].get("cancel_after_seconds", 120)
    with conn() as c:
        stale = c.execute("""
            SELECT order_id, ticker FROM orders
            WHERE status='resting'
            AND datetime(placed_at) < datetime('now', ? || ' seconds')
        """, (f"-{max_age}",)).fetchall()

    for row in stale:
        try:
            kalshi_client.cancel_order(row["order_id"])
            with conn() as c:
                c.execute("UPDATE orders SET status='cancelled' WHERE order_id=?",
                          (row["order_id"],))
            log_event("INFO", "executor", f"Cancelled stale order {row['order_id']} {row['ticker']}")
        except Exception as e:
            log_event("ERROR", "executor", f"cancel_order failed {row['order_id']}: {e}")
