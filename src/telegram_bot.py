"""
Telegram bot — phone command center.
Runs as async task inside the main thread's event loop.
"""
import os, asyncio, datetime as dt
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from db import init_db
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
        f"Weather Bot v2\n"
        f"Status : {'🔴 PAUSED' if kill else '🟢 RUNNING'}\n"
        f"Mode   : {'📄 PAPER' if mode == 'paper' else '💰 LIVE'}\n"
        f"Positions: {n_pos} open\n"
        f"Time   : {dt.datetime.utcnow().strftime('%H:%M UTC')}"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Weather Bot v2 Commands:\n"
        "/status   — bot health\n"
        "/positions — open positions\n"
        "/pause    — stop trading\n"
        "/resume   — resume trading\n"
        "/mode     — current mode\n"
        "/setpaper — switch to paper\n"
        "/setlive  — switch to live\n"
        "/help     — this message"
    )

@only_owner
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_status())

@only_owner
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_positions())

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

async def notify(text: str):
    """Async send — await this from the main async loop."""
    if not TOKEN or not CHAT_ID:
        return
    try:
        async with Bot(TOKEN) as bot:
            await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception:
        pass

def notify_sync(text: str):
    """Sync wrapper for non-async callers."""
    try:
        asyncio.run(notify(text))
    except Exception:
        pass

async def run_bot_async():
    """Run Telegram polling as a proper async task — call from main async loop."""
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
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
