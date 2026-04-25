"""
Weather Bot v2 — Main trader loop (fully async).
"""
import asyncio, datetime as dt, os, yaml
from db import init_db, conn
from kalshi_client import KalshiClient
from noaa_client import NoaaClient
from metar_client import MetarClient
from market_scanner import scan
from executor import (place_order, cancel_stale_orders, daily_loss_check,
                      kill_switch_on, set_state, get_state, log_event)
from reconcile import sync
from position_manager import manage_positions
from telegram_bot import notify, run_bot_async

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yaml")

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def log(msg):
    ts = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)

def is_trading_hours() -> bool:
    from zoneinfo import ZoneInfo
    hour = dt.datetime.now(ZoneInfo("America/New_York")).hour
    return 6 <= hour < 21

def init_day_balance(kalshi_client):
    today = dt.date.today().isoformat()
    if get_state("balance_date") == today:
        return
    try:
        bal = kalshi_client.balance().get("balance", 0) / 100.0
        set_state("day_start_balance", bal)
        set_state("balance_date", today)
        log(f"Day start balance: ${bal:.2f}")
    except Exception as e:
        log(f"Could not record day start balance: {e}")

async def trader_loop():
    init_db()
    cfg     = load_config()
    k       = KalshiClient()
    n       = NoaaClient()
    m       = MetarClient()
    cycle   = 0

    set_state("mode", cfg.get("mode", "paper"))
    log(f"Starting in {cfg.get('mode','paper').upper()} mode")
    await notify(f"🤖 Weather Bot v2 started\nMode: {cfg.get('mode','paper').upper()}")

    sleep_sec    = cfg.get("loop_sleep_seconds", 60)
    scan_every_n = cfg.get("scan_every_n_cycles", 5)

    while True:
        try:
            cfg   = load_config()
            cycle += 1
            log(f"Cycle {cycle} — kill={'ON' if kill_switch_on() else 'OFF'} mode={get_state('mode')}")

            if kill_switch_on():
                log("Kill switch ON — skipping cycle")
                await asyncio.sleep(sleep_sec)
                continue

            if not is_trading_hours():
                log("Outside trading hours (6AM-9PM ET) — sleeping")
                await asyncio.sleep(sleep_sec)
                continue

            init_day_balance(k)

            if daily_loss_check(k, cfg):
                log("Daily loss limit hit — kill switch activated")
                await notify("🔴 Daily loss limit hit — bot paused automatically")
                await asyncio.sleep(sleep_sec)
                continue

            result = sync(k)
            if result.get("error"):
                log(f"Reconcile error: {result['error']}")
            else:
                log(f"Reconcile: {result['synced']} open, {result['cleared']} cleared")

            pm_results = manage_positions(k, dry_run=False)
            for pm in pm_results:
                if pm.get("action") in ("TP", "SL"):
                    log(
                        f"Position manager {pm['action']}: {pm['ticker']} "
                        f"{pm['side']} qty={pm['qty']} "
                        f"entry={pm['entry_cents']}c exit={pm['exit_cents']}c "
                        f"pnl=${pm['realized_usd']:+.2f}"
                    )

            cancel_stale_orders(k, cfg)

            if cycle % scan_every_n == 0:
                log("Scanning markets...")
                candidates = scan(k, n, m, config_path=CONFIG_PATH)

                if candidates and candidates[0].get("error"):
                    log(f"Scan error: {candidates[0]['error']}")
                elif not candidates:
                    log("No candidates found")
                else:
                    log(f"Found {len(candidates)} — best edge: {candidates[0]['edge_cents']}c")
                    best   = candidates[0]
                    result = place_order(k, best, cfg)
                    if result["success"]:
                        msg = (
                            f"{'📄' if result['mode']=='paper' else '💰'} "
                            f"{'PAPER' if result['mode']=='paper' else 'LIVE'} TRADE\n"
                            f"{best['ticker']}\n"
                            f"{best['side'].upper()} x{result['qty']} @ {best['price_cents']}¢\n"
                            f"Edge: {best['edge_cents']}¢  Kelly: ${best['kelly_usd']}\n"
                            f"Forecast: {best['forecast_f']}°F  Obs: {best['obs_f']}°F"
                        )
                        log(msg.replace("\n", " | "))
                        await notify(msg)
                    else:
                        log(f"Order skipped: {result['reason']}")

        except Exception as e:
            log(f"Cycle error: {e}")
            log_event("ERROR", "main", str(e))

        await asyncio.sleep(sleep_sec)

async def main():
    tg_task     = asyncio.create_task(run_bot_async())
    trader_task = asyncio.create_task(trader_loop())
    await asyncio.gather(tg_task, trader_task)

if __name__ == "__main__":
    asyncio.run(main())
