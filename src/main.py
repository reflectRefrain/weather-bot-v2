"""
Weather Bot v3 — Main trader loop.
Strategy: lockin_v3 (observation-based, near-certainty only)
"""
import asyncio, datetime as dt, os, yaml
from db import init_db, conn
from kalshi_client import KalshiClient
from metar_client import MetarClient
from metar_tracker import refresh_all_cities
from scanner_v3 import scan_v3
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
    cfg   = load_config()
    k     = KalshiClient()
    m     = MetarClient()
    cycle = 0

    set_state("mode", cfg.get("mode", "paper"))
    strategy = cfg.get("strategy", "lockin_v3")
    log(f"Starting v3 | mode={cfg.get('mode','paper').upper()} | strategy={strategy}")
    await notify(
        f"Weather Bot v3 started\n"
        f"Mode: {cfg.get('mode','paper').upper()}\n"
        f"Strategy: {strategy}"
    )

    sleep_sec    = cfg.get("loop_sleep_seconds", 60)
    scan_every_n = cfg.get("scan_every_n_cycles", 3)  # scan more often for lock-in

    while True:
        try:
            cfg   = load_config()
            cycle += 1
            log(f"Cycle {cycle} | kill={'ON' if kill_switch_on() else 'OFF'} | mode={get_state('mode')}")

            # ── Safety checks ───────────────────────────────────────────────────
            if kill_switch_on():
                log("Kill switch ON — skipping cycle")
                await asyncio.sleep(sleep_sec)
                continue

            init_day_balance(k)

            if daily_loss_check(k, cfg):
                log("Daily loss limit hit — kill switch activated")
                await notify("⚠️ Daily loss limit hit — bot paused")
                await asyncio.sleep(sleep_sec)
                continue

            # ── Reconcile & position management ──────────────────────────────
            mode = get_state("mode") or cfg.get("mode", "paper")
            if mode != "paper":
                result = sync(k)
                if result.get("error"):
                    log(f"Reconcile error: {result['error']}")
                else:
                    log(f"Reconcile: {result['synced']} open, {result['cleared']} cleared")

            pm_results = manage_positions(k, dry_run=False)
            for pm in pm_results:
                if pm.get("action") in ("TP", "SL"):
                    log(
                        f"PM {pm['action']}: {pm['ticker']} {pm['side']} "
                        f"qty={pm['qty']} entry={pm['entry_cents']}c "
                        f"exit={pm['exit_cents']}c pnl=${pm['realized_usd']:+.2f}"
                    )

            cancel_stale_orders(k, cfg)

            # ── Always refresh METAR obs (even off-cycle) ─────────────────────
            try:
                temps = refresh_all_cities(m)
                log("METAR refresh: " + " ".join(
                    f"{c}={v:.1f}F" for c, v in temps.items() if v is not None
                ))
            except Exception as e:
                log(f"METAR refresh error: {e}")

            # ── Scan & trade every N cycles ───────────────────────────────
            if cycle % scan_every_n == 0:
                log("Scanning lock-in candidates...")
                candidates = scan_v3(metar_client=m, config_path=CONFIG_PATH)

                if not candidates:
                    log("No lock-in candidates — nothing certain enough to trade")
                else:
                    log(f"{len(candidates)} candidate(s) — best edge: {candidates[0]['edge_cents']}c")
                    for best in candidates:
                        result = place_order(k, best, cfg)
                        if result["success"]:
                            trade_type = "PAPER" if result.get("mode") == "paper" else "LIVE"
                            msg = (
                                f"🟢 {trade_type} LOCK-IN TRADE\n"
                                f"{best['ticker']}\n"
                                f"{best['side'].upper()} x{result['qty']} @ {best['price_cents']}c\n"
                                f"Edge: {best['edge_cents']}c  Size: ${round(result['cost_usd'],2)}\n"
                                f"Running max: {best.get('running_max','?')}F  "
                                f"Hrs to lock: {best.get('hours_to_lock','?')}"
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
