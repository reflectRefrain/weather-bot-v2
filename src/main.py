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
                      kill_switch_on, set_state, get_state, log_event,
                      total_balance_usd)
from reconcile import sync
from position_manager import manage_positions
from telegram_bot import notify, run_bot_async, fmt_daily_summary

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yaml")


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def log(msg):
    ts = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def init_day_balance(kalshi_client, cfg):
    """Record day-start balance and auto-update bankroll_usd in config."""
    today = dt.date.today().isoformat()
    if get_state("balance_date") == today:
        return
    try:
        resp      = kalshi_client.balance()
        free      = resp.get("balance", 0) / 100.0
        portfolio = resp.get("portfolio_value", 0) / 100.0
        total     = free + portfolio
        set_state("day_start_balance", total)
        set_state("balance_date", today)
        log(f"Day start balance: ${total:.2f} (free=${free:.2f} portfolio=${portfolio:.2f})")

        # Auto-update bankroll_usd in config.yaml so Kelly sizing stays accurate
        try:
            with open(CONFIG_PATH) as f:
                raw = f.read()
            import re
            updated = re.sub(
                r"^bankroll_usd:.*$",
                f"bankroll_usd: {round(total, 2)}",
                raw,
                flags=re.MULTILINE,
            )
            with open(CONFIG_PATH, "w") as f:
                f.write(updated)
            log(f"bankroll_usd updated to ${total:.2f} in config.yaml")
        except Exception as e:
            log(f"Could not auto-update bankroll_usd: {e}")

    except Exception as e:
        log(f"Could not record day start balance: {e}")


async def send_daily_summary(kalshi_client):
    """Send the daily summary to Telegram."""
    try:
        text = fmt_daily_summary(kalshi_client)
        await notify(text)
        log("Daily summary sent to Telegram")
    except Exception as e:
        log(f"Daily summary failed: {e}")


async def trader_loop():
    init_db()
    cfg              = load_config()
    k                = KalshiClient()
    m                = MetarClient()
    cycle            = 0
    last_summary_day = None

    set_state("mode", cfg.get("mode", "paper"))
    strategy = cfg.get("strategy", "lockin_v3")
    log(f"Starting v3 | mode={cfg.get('mode','paper').upper()} | strategy={strategy}")
    await notify(
        f"Weather Bot v3 started\n"
        f"Mode: {cfg.get('mode','paper').upper()}\n"
        f"Strategy: {strategy}"
    )

    sleep_sec    = cfg.get("loop_sleep_seconds", 60)
    scan_every_n = cfg.get("scan_every_n_cycles", 3)
    summary_hour = cfg.get("telegram", {}).get("daily_summary_hour_utc", 12)

    while True:
        try:
            cfg   = load_config()
            cycle += 1
            mode  = get_state("mode") or cfg.get("mode", "paper")
            log(f"Cycle {cycle} | kill={'ON' if kill_switch_on() else 'OFF'} | mode={mode}")

            # ── Daily summary (once per day at summary_hour UTC) ──────────────
            now_utc = dt.datetime.utcnow()
            today   = now_utc.date()
            if (now_utc.hour == summary_hour
                    and last_summary_day != today):
                last_summary_day = today
                await send_daily_summary(k)

            # ── Safety checks ───────────────────────────────────────────
            if kill_switch_on():
                log("Kill switch ON — skipping cycle")
                await asyncio.sleep(sleep_sec)
                continue

            init_day_balance(k, cfg)

            if daily_loss_check(k, cfg):
                log("Daily loss limit hit — kill switch activated")
                await notify("⚠️ Daily loss limit hit — bot paused")
                await asyncio.sleep(sleep_sec)
                continue

            # ── Reconcile & position management ────────────────────────
            if mode != "paper":
                result = sync(k)
                if result.get("error"):
                    log(f"Reconcile error: {result['error']}")
                else:
                    log(f"Reconcile: {result['synced']} open, {result['cleared']} cleared")

            pm_results = manage_positions(k, dry_run=False)
            for pm in pm_results:
                if pm.get("action") == "SETTLEMENT":
                    outcome = pm.get("outcome", "?")
                    emoji   = "✅" if outcome == "WIN" else "❌"
                    msg = (
                        f"{emoji} SETTLEMENT {outcome}\n"
                        f"{pm['ticker']}\n"
                        f"{pm['side'].upper()} x{pm['qty']} "
                        f"entry={pm['entry_cents']}c "
                        f"pnl=${pm['realized_usd']:+.2f}"
                    )
                    log(msg.replace("\n", " | "))
                    await notify(msg)
                elif pm.get("action") in ("TP", "SL"):
                    log(
                        f"PM {pm['action']}: {pm['ticker']} {pm['side']} "
                        f"qty={pm['qty']} entry={pm['entry_cents']}c "
                        f"exit={pm['exit_cents']}c pnl=${pm['realized_usd']:+.2f}"
                    )

            cancel_stale_orders(k, cfg)

            # ── Always refresh METAR obs ──────────────────────────────
            try:
                temps = refresh_all_cities(m)
                log("METAR refresh: " + " ".join(
                    f"{c}={v:.1f}F" for c, v in temps.items() if v is not None
                ))
            except Exception as e:
                log(f"METAR refresh error: {e}")

            # ── Scan & trade every N cycles ───────────────────────────
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
                                f"🟢 {trade_type} LOCK-IN TRADE | {best['ticker']} | "
                                f"{best['side'].upper()} x{result['qty']} @ {best['price_cents']}c | "
                                f"Edge: {best['edge_cents']}c  Size: ${round(result['cost_usd'],2)} | "
                                f"Running max: {best.get('running_max','?')}F  "
                                f"Hrs to lock: {best.get('hours_to_lock','?')}"
                            )
                            log(msg)
                            await notify(msg)
                        else:
                            reason = result['reason']
                            # Only log non-routine skips to keep logs clean
                            if not any(x in reason for x in
                                       ["already_open", "position_cap",
                                        "resting_order", "free_cash_reserve"]):
                                log(f"Order skipped: {reason}")

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
