"""
Executor — places and cancels orders with full risk guards.
RULES ENFORCED HERE:
  1. Never enter same ticker twice (no accumulation bug)
  2. Hard position cap
  3. Hard per-ticker dollar cap
  4. Daily loss limit auto-kill
  5. Min balance guard
  6. Kill switch check before every order
  7. Cancel stale resting orders automatically (mark expired on 400/404, stop retrying)
  8. Block entry if resting order already exists for same ticker
  9. Block same-day HIGHTEMP YES if obs shows socked-in conditions
 10. Free cash reserve guard — never spend below min_free_cash_usd
"""
import datetime as dt, math, yaml, os
from db import conn, init_db
from reconcile import position_for, open_positions
from cooldown import is_blocked

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yaml")

CITY_METAR = {
    "NYC":  "KNYC",
    "LAX":  "KLAX",
    "CHI":  "KORD",
    "MIA":  "KMIA",
    "DEN":  "KDEN",
    "AUS":  "KAUS",
    "PHIL": "KPHL",
    "BOS":  "KBOS",
    "HOU":  "KHOU",
}


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def log_event(level, module, message):
    with conn() as c:
        c.execute(
            "INSERT INTO events(ts,level,module,message) VALUES(?,?,?,?)",
            (dt.datetime.utcnow().isoformat(), level, module, message),
        )


def get_state(key):
    with conn() as c:
        row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(key, value):
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, str(value))
        )


def kill_switch_on():
    return get_state("kill_switch") == "ON"


def set_kill_switch(on: bool):
    set_state("kill_switch", "ON" if on else "OFF")
    log_event(
        "WARN" if on else "INFO",
        "executor",
        "Kill switch " + ("ACTIVATED" if on else "RELEASED"),
    )


def free_cash_usd(kalshi_client) -> float:
    """Return free (uninvested) cash only — not portfolio value."""
    resp = kalshi_client.balance()
    return resp.get("balance", 0) / 100.0


def total_balance_usd(kalshi_client) -> float:
    """Return free cash + open position portfolio value in USD."""
    resp = kalshi_client.balance()
    free_cash = resp.get("balance", 0) / 100.0
    portfolio = resp.get("portfolio_value", 0) / 100.0
    return free_cash + portfolio


def daily_loss_check(kalshi_client, cfg) -> bool:
    try:
        balance   = total_balance_usd(kalshi_client)
        day_start = float(get_state("day_start_balance") or balance)
        loss      = day_start - balance
        limit     = cfg["bankroll_usd"] * cfg["risk"]["daily_loss_limit_pct"]
        log_event("INFO", "executor",
                  f"Daily loss check: start=${day_start:.2f} now=${balance:.2f} "
                  f"loss=${loss:.2f} limit=${limit:.2f}")
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
    with conn() as c:
        row = c.execute(
            "SELECT order_id FROM orders WHERE ticker=? AND status='resting' LIMIT 1",
            (ticker,),
        ).fetchone()
    return row is not None


def can_enter(ticker: str, cfg: dict) -> tuple:
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


def obs_filter_check(candidate: dict) -> tuple:
    """
    Block same-day HIGHTEMP YES entries when current obs shows:
      - BKN or OVC cloud layer at or below 3000 ft AGL, OR
      - temp/dewpoint spread <= 3F
    Returns (True, reason) if BLOCKED, (False, '') if clear.
    """
    if candidate.get("variable") != "HIGHTEMP":
        return False, ""
    if candidate.get("horizon") != "same_day":
        return False, ""
    if candidate.get("side") != "yes":
        return False, ""
    city    = candidate.get("city", "")
    station = CITY_METAR.get(city)
    if not station:
        return False, ""
    try:
        from metar_client import MetarClient, is_socked_in
        obs = MetarClient().latest(station)
        if obs.get("error"):
            log_event("WARN", "executor",
                      f"obs_filter: METAR error {station}: {obs['error']}")
            return False, ""
        if is_socked_in(obs):
            reason = (
                f"obs_filter: {station} socked_in "
                f"cover={obs.get('sky_cover')} height={obs.get('sky_height_ft')}ft "
                f"temp={obs.get('temp_f')}F dew={obs.get('dewpoint_f')}F"
            )
            return True, reason
    except Exception as e:
        log_event("ERROR", "executor", f"obs_filter exception: {e}")
    return False, ""


def place_order(kalshi_client, candidate: dict, cfg: dict) -> dict:
    init_db()
    ticker  = candidate["ticker"]
    side    = candidate["side"]
    price_c = candidate["price_cents"]
    p       = candidate["model_prob"]

    # ── Free cash reserve guard ───────────────────────────────────────────
    # Stop placing orders when free cash drops to or below the reserve.
    # This prevents a single scan cycle from spending the entire bankroll.
    min_free = float(cfg["risk"].get("min_free_cash_usd", 2.0))
    try:
        cash = free_cash_usd(kalshi_client)
        if cash <= min_free:
            reason = f"free_cash_reserve: ${cash:.2f} <= ${min_free:.2f}"
            log_event("INFO", "executor", f"SKIP {ticker} — {reason}")
            return {"success": False, "reason": reason}
    except Exception as e:
        log_event("ERROR", "executor", f"free_cash check failed: {e}")

    bankroll  = float(cfg.get("bankroll_usd", 100))
    b         = (100 - price_c) / price_c if price_c < 100 else 0.0
    f_raw     = max(0.0, (p * b - (1 - p)) / b) if b > 0 else 0.0
    f_scaled  = f_raw * float(cfg["risk"].get("kelly_fraction", 0.25))
    kelly_usd = min(
        f_scaled * bankroll,
        float(cfg["risk"].get("max_trade_usd", 2.0)),
        float(cfg["risk"].get("max_per_ticker_usd", 2.0)),
    )
    kelly_usd = max(kelly_usd, float(cfg["risk"].get("min_trade_usd", 1.0)))

    # Obs filter
    blocked_by_obs, obs_reason = obs_filter_check(candidate)
    if blocked_by_obs:
        log_event("INFO", "executor", f"OBS_BLOCK {ticker} — {obs_reason}")
        return {"success": False, "reason": obs_reason}

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

    qty      = max(1, math.floor((kelly_usd * 100) / price_c))
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
            c.execute(
                """
                INSERT OR REPLACE INTO positions
                    (ticker, side, qty, avg_price_cents, opened_at, tp_price, sl_price, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')
                """,
                (ticker, side, qty, price_c,
                 dt.datetime.utcnow().isoformat(), tp, sl),
            )
        return {"success": True, "order_id": f"paper-{ticker}", "qty": qty,
                "cost_usd": cost_usd, "mode": "paper"}

    try:
        yes_price = price_c if side == "yes" else None
        no_price  = price_c if side == "no"  else None
        resp = kalshi_client.create_order(
            ticker=ticker, side=side, action="buy", count=qty,
            type_="limit", yes_price=yes_price, no_price=no_price,
        )
        order_id = resp.get("order", {}).get("order_id", "unknown")
        with conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO orders
                    (order_id, ticker, side, action, qty, price_cents, status, placed_at)
                VALUES (?, ?, ?, 'buy', ?, ?, 'resting', ?)
                """,
                (order_id, ticker, side, qty, price_c,
                 dt.datetime.utcnow().isoformat()),
            )
        log_event("INFO", "executor",
                  f"LIVE BUY {ticker} {side} x{qty} @ {price_c}c order_id={order_id}")
        return {"success": True, "order_id": order_id, "qty": qty,
                "cost_usd": cost_usd, "mode": "live"}
    except Exception as e:
        log_event("ERROR", "executor", f"create_order failed {ticker}: {e}")
        return {"success": False, "reason": str(e)}


def cancel_stale_orders(kalshi_client, cfg: dict):
    """Cancel resting orders older than cancel_after_seconds."""
    max_age = cfg["risk"].get("cancel_after_seconds", 120)
    with conn() as c:
        stale = c.execute(
            """
            SELECT order_id, ticker FROM orders
            WHERE status='resting'
            AND datetime(placed_at) < datetime('now', ? || ' seconds')
            """,
            (f"-{max_age}",),
        ).fetchall()
    for row in stale:
        try:
            kalshi_client.cancel_order(row["order_id"])
            with conn() as c:
                c.execute(
                    "UPDATE orders SET status='cancelled' WHERE order_id=?",
                    (row["order_id"],),
                )
            log_event("INFO", "executor",
                      f"Cancelled stale order {row['order_id']} {row['ticker']}")
        except Exception as e:
            err = str(e)
            if "400" in err or "404" in err or "not found" in err.lower() or "does not exist" in err.lower():
                with conn() as c:
                    c.execute(
                        "UPDATE orders SET status='expired' WHERE order_id=?",
                        (row["order_id"],),
                    )
                log_event("INFO", "executor",
                          f"Order {row['order_id']} already gone on Kalshi — marked expired")
            else:
                log_event("ERROR", "executor",
                          f"cancel_order failed {row['order_id']}: {err[:120]}")
