"""
Telegram bot — phone command center.
Runs as async task inside the main thread's event loop.
"""
import os, asyncio, datetime as dt
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from db import init_db, conn
from executor import get_state, set_state, set_kill_switch, kill_switch_on
from reconcile import open_positions

TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

SEP = "─" * 22


def only_owner(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != CHAT_ID:
            await update.message.reply_text("⛔ Unauthorized")
            return
        return await func(update, ctx)
    return wrapper


# ──────────────────────────────────────────────────────────────────
Formatters
# ──────────────────────────────────────────────────────────────────

def fmt_status() -> str:
    kill  = kill_switch_on()
    mode  = get_state("mode") or "paper"
    n_pos = len(open_positions())
    return (
        f"Weather Bot v2\n"
        f"{SEP}\n"
        f"Status   : {'\U0001f534 PAUSED' if kill else '\U0001f7e2 RUNNING'}\n"
        f"Mode     : {'\U0001f4c4 PAPER' if mode == 'paper' else '\U0001f4b0 LIVE'}\n"
        f"Positions: {n_pos} open\n"
        f"Time     : {dt.datetime.utcnow().strftime('%H:%M UTC')}"
    )


def fmt_positions() -> str:
    rows = open_positions()
    if not rows:
        return "No open positions."
    lines = [f"Open Positions ({len(rows)})", SEP]
    for p in rows:
        entry = int(p['avg_price_cents'])
        max_win  = round((100 - entry) * int(p['qty']) / 100, 2)
        max_loss = round(entry * int(p['qty']) / 100, 2)
        lines.append(
            f"\U0001f4cc {p['ticker']}\n"
            f"  {p['side'].upper()} x{p['qty']} @ {entry}\u00a2\n"
            f"  Max Win: +${max_win}  |  Max Loss: -${max_loss}\n"
            f"  Opened : {p['opened_at'][11:16]} UTC"
        )
    return "\n".join(lines)


def fmt_pnl() -> str:
    """Today's realized PnL from the pnl table."""
    today = dt.date.today().isoformat()
    with conn() as c:
        rows = c.execute(
            "SELECT ticker, side, qty, entry_cents, exit_cents, realized_usd, reason "
            "FROM pnl WHERE date(closed_at) = ? ORDER BY closed_at DESC",
            (today,)
        ).fetchall()
        total = c.execute(
            "SELECT COALESCE(SUM(realized_usd), 0) FROM pnl WHERE date(closed_at) = ?",
            (today,)
        ).fetchone()[0]
    if not rows:
        return f"No closed trades today ({today})."
    lines = [f"\U0001f4ca Today's PnL  ({today})", SEP]
    for r in rows:
        icon = "\u2705" if r['realized_usd'] >= 0 else "\u274c"
        lines.append(
            f"{icon} {r['ticker']}\n"
            f"  {r['side'].upper()} x{r['qty']}  "
            f"{r['entry_cents']}\u00a2 \u2192 {r['exit_cents']}\u00a2  "
            f"${r['realized_usd']:+.2f}  [{r['reason']}]"
        )
    lines.append(SEP)
    icon = "\U0001f4c8" if total >= 0 else "\U0001f4c9"
    lines.append(f"{icon} Total: ${total:+.2f}")
    return "\n".join(lines)


def fmt_balance(kalshi_client=None) -> str:
    """Live Kalshi balance."""
    if kalshi_client is None:
        try:
            from kalshi_client import KalshiClient
            kalshi_client = KalshiClient()
        except Exception:
            return "Could not connect to Kalshi."
    try:
        resp = kalshi_client.balance()
        free  = resp.get("balance", 0) / 100.0
        port  = resp.get("portfolio_value", 0) / 100.0
        total = free + port
        return (
            f"\U0001f4b0 Kalshi Balance\n"
            f"{SEP}\n"
            f"Free Cash : ${free:.2f}\n"
            f"Positions : ${port:.2f}\n"
            f"Total     : ${total:.2f}"
        )
    except Exception as e:
        return f"Balance fetch failed: {e}"


def fmt_strategy() -> str:
    return (
        "\U0001f9e0 Current Strategy: Lock-In\n"
        f"{SEP}\n"
        "How it works:\n"
        "By 2PM local time, real airport weather sensors\n"
        "(METAR) have recorded most of the day's temperature.\n"
        "If Chicago already hit 72\u00b0F by 2PM, then any Kalshi\n"
        "bracket asking \"will the high exceed 68\u00b0F?\" is\n"
        "nearly certain to resolve YES.\n"
        "\n"
        "The bot looks for cases where the market price\n"
        "hasn't caught up to that reality yet — that gap\n"
        "is the edge.\n"
        f"{SEP}\n"
        "Entry window : 2PM \u2013 3PM local per city\n"
        "Min confidence: 80% model probability\n"
        "Min edge      : 8\u00a2 over market price\n"
        "Contract floor: 20\u00a2 (avoids fee death zone)\n"
        "Max positions : 5 at a time\n"
        "Max per trade : $6.00\n"
        f"{SEP}\n"
        "Cities watched: NYC, CHI, MIA, LAX, DEN,\n"
        "               BOS, AUS, PHIL, HOU\n"
        "Markets       : High Temp + Low Temp"
    )


def fmt_daily_summary() -> str:
    """Full daily summary: PnL, positions, balance snapshot."""
    today = dt.date.today().isoformat()
    with conn() as c:
        closed = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(realized_usd),0) FROM pnl WHERE date(closed_at)=?",
            (today,)
        ).fetchone()
        wins = c.execute(
            "SELECT COUNT(*) FROM pnl WHERE date(closed_at)=? AND realized_usd > 0",
            (today,)
        ).fetchone()[0]
    n_closed = closed[0]
    total_pnl = closed[1]
    n_open = len(open_positions())
    win_rate = f"{wins}/{n_closed}" if n_closed else "0/0"
    icon = "\U0001f4c8" if total_pnl >= 0 else "\U0001f4c9"
    return (
        f"\U0001f305 Daily Summary — {today}\n"
        f"{SEP}\n"
        f"Trades closed : {n_closed}\n"
        f"Win/Loss      : {win_rate}\n"
        f"{icon} Realized PnL : ${total_pnl:+.2f}\n"
        f"Open positions: {n_open}\n"
        f"{SEP}\n"
        f"Send /pnl for trade breakdown\n"
        f"Send /balance for live balance"
    )


# ──────────────────────────────────────────────────────────────────
Rich alert builders (called from main.py and position_manager.py)
# ──────────────────────────────────────────────────────────────────

def build_trade_alert(candidate: dict, result: dict) -> str:
    """Rich alert sent when a new trade is placed."""
    mode  = result.get("mode", "live").upper()
    icon  = "\U0001f7e2" if mode == "LIVE" else "\U0001f4c4"
    side  = candidate.get("side", "").upper()
    price = int(candidate.get("price_cents", 0))
    qty   = int(result.get("qty", 0))
    cost  = float(result.get("cost_usd", 0))
    conf  = round(candidate.get("model_prob", 0) * 100, 1)
    edge  = candidate.get("edge_cents", 0)
    obs   = candidate.get("obs_f")
    fcst  = candidate.get("forecast_f")
    city  = candidate.get("city", "")
    hz    = candidate.get("horizon", "").replace("_", " ").title()
    tp    = min(99, price + max(10, int(round(edge))))
    sl    = max(1,  price - max(5,  int(round(edge * 0.5))))
    max_win  = round((100 - price) * qty / 100, 2)
    max_loss = round(price * qty / 100, 2)
    obs_str  = f"{obs}\u00b0F" if obs is not None else "N/A"
    fcst_str = f"{fcst}\u00b0F" if fcst is not None else "N/A"
    return (
        f"{icon} {mode} TRADE — LOCK-IN\n"
        f"{SEP}\n"
        f"Ticker : {candidate.get('ticker')}\n"
        f"Side   : {side}  |  City: {city} ({hz})\n"
        f"Obs    : {obs_str}  |  Forecast: {fcst_str}\n"
        f"Conf   : {conf}%  |  Edge: +{edge}\u00a2\n"
        f"{SEP}\n"
        f"Entry  : {price}\u00a2  x{qty} contracts\n"
        f"Cost   : ${cost:.2f}\n"
        f"Max Win: +${max_win:.2f}  |  Max Loss: -${max_loss:.2f}\n"
        f"TP @ {tp}\u00a2  |  SL @ {sl}\u00a2"
    )


def build_close_alert(result: dict, kalshi_client=None) -> str:
    """Rich alert sent when a position is closed (TP or SL)."""
    action  = result.get("action", "CLOSE")
    icon    = "\u2705" if action == "TP" else "\u274c"
    label   = "TAKE PROFIT" if action == "TP" else "STOP LOSS"
    ticker  = result.get("ticker", "")
    side    = result.get("side", "").upper()
    qty     = result.get("qty", 0)
    entry   = result.get("entry_cents", 0)
    exit_c  = result.get("exit_cents", 0)
    pnl     = result.get("realized_usd", 0)
    pnl_pct = round((pnl / (entry * qty / 100)) * 100, 1) if entry and qty else 0

    # Calc hold time from DB
    hold_str = ""
    try:
        with conn() as c:
            row = c.execute(
                "SELECT opened_at FROM positions WHERE ticker=? ORDER BY opened_at DESC LIMIT 1",
                (ticker,)
            ).fetchone()
        if row:
            opened = dt.datetime.fromisoformat(row["opened_at"])
            mins = int((dt.datetime.utcnow() - opened).total_seconds() / 60)
            hold_str = f"\nHold   : {mins} min"
    except Exception:
        pass

    # Live balance
    bal_str = ""
    if kalshi_client:
        try:
            resp = kalshi_client.balance()
            bal  = (resp.get("balance", 0) + resp.get("portfolio_value", 0)) / 100.0
            bal_icon = "\U0001f4c8" if pnl >= 0 else "\U0001f4c9"
            bal_str = f"\n{SEP}\n{bal_icon} Balance: ${bal:.2f}"
        except Exception:
            pass

    return (
        f"{icon} POSITION CLOSED — {label}\n"
        f"{SEP}\n"
        f"Ticker : {ticker}\n"
        f"Side   : {side}  x{qty} contracts\n"
        f"Entry  : {entry}\u00a2  \u2192  Exit: {exit_c}\u00a2\n"
        f"PnL    : ${pnl:+.2f}  ({pnl_pct:+.1f}%)"
        f"{hold_str}"
        f"{bal_str}"
    )


# ──────────────────────────────────────────────────────────────────
Command handlers
# ──────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Weather Bot v2 Commands\n"
        f"{SEP}\n"
        "/status    — bot health & mode\n"
        "/positions — open positions + max win/loss\n"
        "/pnl       — today's realized PnL\n"
        "/balance   — live Kalshi balance\n"
        "/strategy  — how the bot works\n"
        "/pause     — stop trading\n"
        "/resume    — resume trading\n"
        "/setpaper  — switch to paper mode\n"
        "/setlive   — switch to live mode\n"
        "/mode      — current mode\n"
        "/help      — this message"
    )


@only_owner
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_status())


@only_owner
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_positions())


@only_owner
async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_pnl())


@only_owner
async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_balance())


@only_owner
async def cmd_strategy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_strategy())


@only_owner
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_kill_switch(True)
    await update.message.reply_text("\U0001f534 Kill switch ON — bot paused.")


@only_owner
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_kill_switch(False)
    await update.message.reply_text("\U0001f7e2 Kill switch OFF — bot resumed.")


@only_owner
async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Current mode: {(get_state('mode') or 'paper').upper()}")


@only_owner
async def cmd_setpaper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_state("mode", "paper")
    await update.message.reply_text("\U0001f4c4 Switched to PAPER mode.")


@only_owner
async def cmd_setlive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_state("mode", "live")
    await update.message.reply_text("\U0001f4b0 Switched to LIVE mode.")


# ──────────────────────────────────────────────────────────────────
Notify helpers
# ──────────────────────────────────────────────────────────────────

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


async def send_daily_summary():
    """Send daily summary at midnight CT (5 AM UTC)."""
    await notify(fmt_daily_summary())


# ──────────────────────────────────────────────────────────────────
Bot runner
# ──────────────────────────────────────────────────────────────────

async def run_bot_async():
    """Run Telegram polling as a proper async task — call from main async loop."""
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("strategy",  cmd_strategy))
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
