"""
Telegram bot — phone command center.
Runs as async task inside the main thread’s event loop.

Commands:
  /help       — command list
  /status     — bot health + balance
  /balance    — quick balance check
  /positions  — open positions
  /pnl        — today’s PnL
  /pnl7       — last 7 days PnL
  /summary    — full daily summary
  /pause      — stop trading
  /resume     — resume trading
  /mode       — current mode
  /setpaper   — switch to paper
  /setlive    — switch to live
"""
import os, asyncio, datetime as dt
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from db import init_db, conn
from executor import get_state, set_state, set_kill_switch, kill_switch_on
from reconcile import open_positions

TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))


def only_owner(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != CHAT_ID:
            await update.message.reply_text("⛔ Unauthorized")
            return
        return await func(update, ctx)
    return wrapper


# ──────────────────────────────────────────────────
# Formatters
# ──────────────────────────────────────────────────

def fmt_balance() -> str:
    try:
        from kalshi_client import KalshiClient
        resp      = KalshiClient().balance()
        free      = resp.get("balance", 0) / 100.0
        portfolio = resp.get("portfolio_value", 0) / 100.0
        total     = free + portfolio
        n_pos     = len(open_positions())
        return (
            f"💵 Balance\n"
            f"Free cash : ${free:.2f}\n"
            f"Portfolio : ${portfolio:.2f}\n"
            f"Total     : ${total:.2f}\n"
            f"Positions : {n_pos} open"
        )
    except Exception as e:
        return f"Could not fetch balance: {e}"


def fmt_positions() -> str:
    rows = open_positions()
    if not rows:
        return "No open positions."
    lines = []
    for p in rows:
        lines.append(
            f"• {p['ticker']}\n"
            f"  {p['side'].upper()} x{p['qty']} @ {p['avg_price_cents']}¢\n"
            f"  opened: {p['opened_at'][11:16]} UTC"
        )
    return "\n".join(lines)


def fmt_status() -> str:
    kill  = kill_switch_on()
    mode  = get_state("mode") or "paper"
    n_pos = len(open_positions())
    return (
        f"Weather Bot v3\n"
        f"Status    : {'🔴 PAUSED' if kill else '🟢 RUNNING'}\n"
        f"Mode      : {'📄 PAPER' if mode == 'paper' else '💰 LIVE'}\n"
        f"Positions : {n_pos} open\n"
        f"Time      : {dt.datetime.utcnow().strftime('%H:%M UTC')}"
    )


def fmt_pnl(days: int = 1) -> str:
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    with conn() as c:
        rows = c.execute(
            "SELECT ticker, side, realized_usd FROM pnl "
            "WHERE date(closed_at) >= ? ORDER BY closed_at DESC",
            (since,),
        ).fetchall()
    if not rows:
        return f"No settled trades in the last {days} day(s)."
    total    = sum(r["realized_usd"] for r in rows)
    wins     = sum(1 for r in rows if r["realized_usd"] > 0)
    losses   = len(rows) - wins
    win_rate = wins / len(rows) * 100
    lines    = [
        f"PnL — last {days} day(s)",
        f"Trades : {len(rows)} ({wins}W / {losses}L  {win_rate:.0f}%)",
        f"Total  : ${total:+.2f}",
        "",
    ]
    for r in rows[:10]:
        icon = "✅" if r["realized_usd"] > 0 else "❌"
        lines.append(f"{icon} {r['ticker'][-20:]} {r['side'].upper()} ${r['realized_usd']:+.2f}")
    if len(rows) > 10:
        lines.append(f"... and {len(rows)-10} more")
    return "\n".join(lines)


def fmt_daily_summary(kalshi_client=None) -> str:
    today = dt.date.today().isoformat()
    with conn() as c:
        pnl_rows = c.execute(
            "SELECT realized_usd FROM pnl WHERE date(closed_at) = ?", (today,)
        ).fetchall()
        open_pos = open_positions()
        last5 = c.execute(
            "SELECT ts, level, message FROM events ORDER BY ts DESC LIMIT 5"
        ).fetchall()

    total    = sum(r["realized_usd"] for r in pnl_rows)
    wins     = sum(1 for r in pnl_rows if r["realized_usd"] > 0)
    losses   = sum(1 for r in pnl_rows if r["realized_usd"] <= 0)
    win_rate = wins / len(pnl_rows) * 100 if pnl_rows else 0

    balance_str = ""
    if kalshi_client:
        try:
            resp      = kalshi_client.balance()
            free      = resp.get("balance", 0) / 100.0
            portfolio = resp.get("portfolio_value", 0) / 100.0
            balance_str = f"\n💵 Free: ${free:.2f}  Portfolio: ${portfolio:.2f}  Total: ${free+portfolio:.2f}"
        except Exception:
            pass

    lines = [
        f"🌤 Weather Bot Daily Summary — {today}",
        balance_str,
        f"",
        f"📊 Settled today: {len(pnl_rows)} trades  {wins}W/{losses}L  {win_rate:.0f}% win rate",
        f"💰 Today PnL: ${total:+.2f}",
        f"📂 Open positions: {len(open_pos)}",
    ]
    if last5:
        lines.append("\n🔍 Last 5 events:")
        for e in last5:
            lines.append(f"  [{e['level']}] {e['message'][:60]}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Weather Bot v3 Commands:\n"
        "/balance   — quick balance check\n"
        "/status    — bot health\n"
        "/positions — open positions\n"
        "/pnl       — today’s PnL\n"
        "/pnl7      — last 7 days PnL\n"
        "/summary   — full daily summary\n"
        "/pause     — stop trading\n"
        "/resume    — resume trading\n"
        "/mode      — current mode\n"
        "/setpaper  — switch to paper\n"
        "/setlive   — switch to live\n"
        "/help      — this message"
    )


@only_owner
async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_balance())


@only_owner
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_status())


@only_owner
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_positions())


@only_owner
async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_pnl(days=1))


@only_owner
async def cmd_pnl7(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_pnl(days=7))


@only_owner
async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_daily_summary())


@only_owner
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_kill_switch(True)
    await update.message.reply_text("🔴 Kill switch ON — bot paused.")


@only_owner
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_kill_switch(False)
    await update.message.reply_text("🟢 Kill switch OFF — bot resumed.")


@only_owner
async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Current mode: {(get_state('mode') or 'paper').upper()}")


@only_owner
async def cmd_setpaper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_state("mode", "paper")
    await update.message.reply_text("📄 Switched to PAPER mode.")


@only_owner
async def cmd_setlive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_state("mode", "live")
    await update.message.reply_text("💰 Switched to LIVE mode.")


# ──────────────────────────────────────────────────
# Async helpers
# ──────────────────────────────────────────────────

async def notify(text: str):
    if not TOKEN or not CHAT_ID:
        return
    try:
        async with Bot(TOKEN) as bot:
            await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception:
        pass


def notify_sync(text: str):
    try:
        asyncio.run(notify(text))
    except Exception:
        pass


async def run_bot_async():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))
    app.add_handler(CommandHandler("pnl7",      cmd_pnl7))
    app.add_handler(CommandHandler("summary",   cmd_summary))
    app.add_handler(CommandHandler("pause",     cmd_pause))
    app.add_handler(CommandHandler("resume",    cmd_resume))
    app.add_handler(CommandHandler("mode",      cmd_mode))
    app.add_handler(CommandHandler("setpaper",  cmd_setpaper))
    app.add_handler(CommandHandler("setlive",   cmd_setlive))
    print("[telegram] polling started")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await asyncio.sleep(1)
