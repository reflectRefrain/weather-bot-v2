"""
Executor — places and cancels orders with full risk guards.
RULES ENFORCED HERE:
  1. Never enter same ticker twice (no accumulation bug)
  2. Hard position cap (checked against LIVE Kalshi positions, not just local DB)
  3. Hard per-ticker dollar cap
  4. Daily loss limit auto-kill (based on free cash only, not portfolio value)
  5. Min balance guard
  6. Kill switch check before every order
  7. Cancel stale resting orders automatically (mark expired on 400/404, stop retrying)
  8. Block entry if resting order already exists for same ticker
  9. Block same-day HIGHTEMP YES if obs shows socked-in conditions
  10. Block entry if any open position already exists for same city + target_date
  11. Hard cap on max contracts per trade (prevents oversizing on cheap contracts)
"""
import datetime as dt, math, yaml, os
from db import conn, init_db
from reconcile import position_for, open_positions, sync_from_kalshi
from cooldown import is_blocked

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yaml")

CITY_METAR = {
    "NYC":  "KNYC",
    "LAX":  "KLAX",
    "CHI":  "KMDW",   # Kalshi CLI settles on Midway, not O'Hare
    "MIA":  "KMIA",
    "DEN":  "KDEN",
    "AUS":  "KAUS",
    "PHIL": "KPHL",
    "BOS":  "KBOS",
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
    """Return free (uninvested) cash only — not portfolio value.
    Daily loss limit is based on free cash so open winning positions
    don't create a false 'loss' reading."""
    resp = kalshi_client.balance()
    return resp.get("balance", 0) / 100.0


def total_balance_usd(kalshi_client) -> float:
    resp = kalshi_client.balance()
    free_cash = resp.get("balance", 0) / 100.0
    portfolio = resp.get("portfolio_value", 0) / 100.0
    return free_cash + portfolio


def daily_loss_check(kalshi_client, cfg) -> bool:
    try:
        # Use free cash only for loss tracking.
        # Portfolio value fluctuates with open positions and would
        # incorrectly trigger the kill switch when the bot is winning.
        free_cash = free_cash_usd(kalshi_client)
        day_start = float(get_state("day_start_balance") or free_cash)
        loss = day_start - free_cash
        limit = cfg["bankroll_usd"] * cfg["risk"]["daily_loss_limit_pct"]
        log_event("INFO", "executor",
                  f"Daily loss check: start=${day_start:.2f} free=${free_cash:.2f} loss=${loss:.2f} limit=${limit:.2f}")
        if loss >= limit:
            log_event("WARN", "executor",
                      f"Daily loss limit hit: lost ${loss:.2f} vs limit ${limit:.2f}")
            set_kill_switch(True)
            return True
        min_bal = cfg["risk"]["min_balance_usd"]
        if free_cash < min_bal:
            log_event("WARN", "executor",
                      f"Free cash ${free_cash:.2f} below minimum ${min_bal:.2f}")
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


def city_date_open(city: str, target_date: str) -> bool:
    if not city or not target_date:
        return False
    with conn() as c:
        row = c.execute(
            """
            SELECT p.ticker FROM positions p
            JOIN markets m ON p.ticker = m.ticker
            WHERE p.status = 'OPEN'
              AND m.city = ?
              AND m.target_date = ?
            LIMIT 1
            """,
            (city, target_date),
        ).fetchone()
    return row is not None


def live_open_position_count(kalshi_client) -> int:
    try:
        sync_from_kalshi(kalshi_client)
    except Exception as e:
        log_event("WARN", "executor", f"live_open_position_count sync failed: {e}")
    return len(open_positions())


def can_enter(ticker: str, cfg: dict, kalshi_client=None, candidate: dict = None) -> tuple:
    if kill_switch_on():
        return False, "kill_switch_ON"
    existing = position_for(ticker)
    if existing:
        return False, f"already_open:{ticker}"
    if resting_order_for(ticker):
        return False, f"resting_order_exists:{ticker}"

    if candidate:
        city = candidate.get("city", "")
        target_date = candidate.get("target_date", "")
        if city_date_open(city, target_date):
            return False, f"city_date_cap:{city}:{target_date}"

    if kalshi_client:
        open_count = live_open_position_count(kalshi_client)
    else:
        open_count = len(open_positions())

    max_pos = cfg["risk"]["max_open_positions"]
    if open_count >= max_pos:
        return False, f"position_cap:{open_count}/{max_pos}"
    return True, ""


def obs_filter_check(candidate: dict) -> tuple:
    if candidate.get("variable") != "HIGHTEMP":
        return False, ""
    if candidate.get("horizon") != "same_day":
        return False, ""
    if candidate.get("side") != "yes":
        return False, ""
    city = candidate.get("city", "")
    station = CITY_METAR.get(city)
    if not station:
        return False, ""
    try:
        from metar_client import MetarClient, is_socked_in
        obs = MetarClient().latest(station)
        if obs.get("error"):
            log_event("WARN", "executor", f"obs_filter: METAR error {station}: {obs['error']}")
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
    ticker = candidate["ticker"]
    side = candidate["side"]
    price_c = candidate["price_cents"]
    p = candidate["model_prob"]

    bankroll = float(cfg.get("bankroll_usd", 100))
    b = (100 - price_c) / price_c if price_c < 100 else 0.0
    f_raw = max(0.0, (p * b - (1 - p)) / b) if b > 0 else 0.0
    f_scaled = f_raw * float(cfg["risk"].get("kelly_fraction", 0.25))
    kelly_usd = min(
        f_scaled * bankroll,
        float(cfg["risk"].get("max_trade_usd", 2.0)),
        float(cfg["risk"].get("max_per_ticker_usd", 2.0)),
    )
    # Half-size mode for legacy candidates (Option B). Tagged on the candidate
    # by market_scanner.py so we can keep collecting data on the legacy strategy
    # at reduced risk while comparing it to the named books.
    book_type_tag = candidate.get("book_type")
    half_size = bool(candidate.get("half_size", False))
    if half_size:
        kelly_usd = kelly_usd * 0.5
        log_event("INFO", "executor",
                  f"HALF_SIZE applied to {ticker} (book={book_type_tag}) -> kelly=${kelly_usd:.2f}")
    kelly_usd = max(kelly_usd, float(cfg["risk"].get("min_trade_usd", 1.00)))

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

    ok, reason = can_enter(ticker, cfg, kalshi_client=kalshi_client, candidate=candidate)
    if not ok:
        log_event("INFO", "executor", f"SKIP {ticker} — {reason}")
        return {"success": False, "reason": reason}

    if daily_loss_check(kalshi_client, cfg):
        return {"success": False, "reason": "daily_loss_limit"}

    # Qty calculation with hard contract cap
    max_contracts = int(cfg["risk"].get("max_contracts", 15))
    qty = max(1, math.floor((kelly_usd * 100) / price_c))
    qty = min(qty, max_contracts)
    cost_usd = qty * price_c / 100.0
    max_usd = cfg["risk"]["max_per_ticker_usd"]
    if cost_usd > max_usd:
        qty = max(1, math.floor((max_usd * 100) / price_c))
        qty = min(qty, max_contracts)
        cost_usd = qty * price_c / 100.0

    mode = get_state("mode") or cfg.get("mode", "paper")

    if mode == "paper":
        log_event("INFO", "executor",
                  f"PAPER BUY {ticker} {side} x{qty} @ {price_c}c = ${cost_usd:.2f}")
        tp = min(99, price_c + max(10, int(round(candidate["edge_cents"]))))
        sl = max(1, price_c - max(5, int(round(candidate["edge_cents"] * 0.5))))
        with conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO positions
                    (ticker, side, qty, avg_price_cents, opened_at, tp_price, sl_price, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')
                """,
                (ticker, side, qty, price_c, dt.datetime.utcnow().isoformat(), tp, sl),
            )
        return {"success": True, "order_id": f"paper-{ticker}", "qty": qty,
                "cost_usd": cost_usd, "mode": "paper"}

    try:
        yes_price = price_c if side == "yes" else None
        no_price = price_c if side == "no" else None
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
                (order_id, ticker, side, qty, price_c, dt.datetime.utcnow().isoformat()),
            )
        log_event("INFO", "executor",
                  f"LIVE BUY {ticker} {side} x{qty} @ {price_c}c order_id={order_id}")
        return {"success": True, "order_id": order_id, "qty": qty,
                "cost_usd": cost_usd, "mode": "live"}
    except Exception as e:
        log_event("ERROR", "executor", f"create_order failed {ticker}: {e}")
        return {"success": False, "reason": str(e)}


def cancel_stale_orders(kalshi_client, cfg: dict):
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
