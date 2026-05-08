"""
Telegram bot — phone command center.
Runs as async task inside the main thread's event loop.
"""
import os, asyncio, datetime as dt, subprocess
from telegram import Update, Bot, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from db import init_db, conn
from executor import get_state, set_state, set_kill_switch, kill_switch_on
from reconcile import open_positions

TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# Host-side project path (mounted into container at /host/weather-bot-v2)
HOST_PROJECT_DIR = "/host/weather-bot-v2"

SEP = "─" * 22

# Commands registered with Telegram (shows in the / menu)
BOT_COMMANDS = [
    BotCommand("status",      "Bot health & current mode"),
    BotCommand("positions",   "Open positions + max win/loss"),
    BotCommand("pnl",         "Today's realized PnL"),
    BotCommand("balance",     "Live Kalshi balance"),
    BotCommand("strategy",    "How the bot works right now"),
    BotCommand("pause",       "Stop trading (kill switch ON)"),
    BotCommand("resume",      "Resume trading (kill switch OFF)"),
    BotCommand("mode",        "Show current mode (paper/live)"),
    BotCommand("setpaper",    "Switch to paper mode"),
    BotCommand("setlive",     "Switch to live mode"),
    BotCommand("botlogs",     "Last 30 log lines"),
    BotCommand("restartbot",  "docker compose restart (no rebuild)"),
    BotCommand("rebuildbot",  "docker compose up -d --build"),
    BotCommand("pullbot",     "git pull + rebuild (deploy latest code)"),
    BotCommand("help",        "Show all commands"),
]


def only_owner(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != CHAT_ID:
            await update.message.reply_text("⛔ Unauthorized")
            return
        return await func(update, ctx)
    return wrapper


# ──────────────────────────────────────────────────────────────────
# Formatters
# ──────────────────────────────────────────────────────────────────

def fmt_status() -> str:
    kill  = kill_switch_on()
    mode  = get_state("mode") or "paper"
    n_pos = len(open_positions())
    return (
        f"Weather Bot v2\n"
        f"{SEP}\n"
        f"Status   : {'🔴 PAUSED' if kill else '🟢 RUNNING'}\n"
        f"Mode     : {'📄 PAPER' if mode == 'paper' else '💰 LIVE'}\n"
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
            f"📌 {p['ticker']}\n"
            f"  {p['side'].upper()} x{p['qty']} @ {entry}¢\n"
            f"  Max Win: +${max_win}  |  Max Loss: -${max_loss}\n"
            f"  Opened : {p['opened_at'][11:16]} UTC"
        )
    return "\n".join(lines)


def fmt_pnl() -> str:
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
    lines = [f"📊 Today's PnL  ({today})", SEP]
    for r in rows:
        icon = "✅" if r['realized_usd'] >= 0 else "❌"
        lines.append(
            f"{icon} {r['ticker']}\n"
            f"  {r['side'].upper()} x{r['qty']}  "
            f"{r['entry_cents']}¢ → {r['exit_cents']}¢  "
            f"${r['realized_usd']:+.2f}  [{r['reason']}]"
        )
    lines.append(SEP)
    icon = "📈" if total >= 0 else "📉"
    lines.append(f"{icon} Total: ${total:+.2f}")
    return "\n".join(lines)


def fmt_balance(kalshi_client=None) -> str:
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
            f"💰 Kalshi Balance\n"
            f"{SEP}\n"
            f"Free Cash : ${free:.2f}\n"
            f"Positions : ${port:.2f}\n"
            f"Total     : ${total:.2f}"
        )
    except Exception as e:
        return f"Balance fetch failed: {e}"


def fmt_strategy() -> str:
    return (
        "🧠 Current Strategy: Lock-In\n"
        f"{SEP}\n"
        "How it works:\n"
        "By 2PM local time, real airport weather sensors\n"
        "(METAR) have recorded most of the day's temp.\n"
        "If Chicago already hit 72°F by 2PM, any Kalshi\n"
        'bracket asking "will the high exceed 68°F?" is\n'
        "nearly certain to resolve YES.\n"
        "\n"
        "The bot looks for cases where the market price\n"
        "hasn't caught up to reality yet — that gap\n"
        "is the edge we trade.\n"
        f"{SEP}\n"
        "Entry window  : 2PM – 3PM local per city\n"
        "Min confidence: 80% model probability\n"
        "Min edge      : 8¢ over market price\n"
        "Contract floor: 20¢ (avoids fee death zone)\n"
        "Max positions : 5 at a time\n"
        "Max per trade : $6.00\n"
        f"{SEP}\n"
        "Cities: 18 (NYC CHI MIA LAX DEN BOS AUS PHIL HOU ATL PHX DC LAS SAT MIN DAL SF OKC)\n"
        "Lockin floor : fee-aware (PR #9) | Tie buffer: 1°F\n"
        "Markets: High Temp + Low Temp (same-day only)"
    )


def _pnl_window(c, hours: int) -> tuple[int, int, int, float]:
    """Return (n_closed, wins, losses, net_pnl) over the last `hours` window."""
    rows = c.execute(
        "SELECT realized_usd FROM pnl WHERE closed_at > datetime('now', ?)",
        (f'-{hours} hours',)
    ).fetchall()
    wins = sum(1 for r in rows if r[0] > 0)
    losses = sum(1 for r in rows if r[0] < 0)
    net = sum(r[0] for r in rows)
    return len(rows), wins, losses, net


def fmt_daily_summary() -> str:
    today = dt.date.today().isoformat()
    with conn() as c:
        n24, w24, l24, p24 = _pnl_window(c, 24)
        n7,  w7,  l7,  p7  = _pnl_window(c, 168)
        n_all = c.execute("SELECT COUNT(*), COALESCE(SUM(realized_usd),0) FROM pnl").fetchone()
        w_all = c.execute("SELECT COUNT(*) FROM pnl WHERE realized_usd > 0").fetchone()[0]
    n_open = len(open_positions())

    def _line(label, n, w, l, pnl):
        if n == 0:
            return f"  {label}: no settled trades"
        wr = w / max(1, w + l) * 100
        icon = "📈" if pnl >= 0 else "📉"
        return f"  {label}: {n} closed | {w}W/{l}L | {wr:.0f}% | {icon} ${pnl:+.2f}"

    icon_all = "📈" if n_all[1] >= 0 else "📉"
    return (
        f"🌅 Daily Summary — {today}\n"
        f"{SEP}\n"
        f"{_line('Last 24h', n24, w24, l24, p24)}\n"
        f"{_line('Last 7d ', n7,  w7,  l7,  p7)}\n"
        f"  Lifetime: {n_all[0]} closed | {w_all}W/{n_all[0]-w_all}L | {icon_all} ${n_all[1]:+.2f}\n"
        f"  Open positions: {n_open}\n"
        f"{SEP}\n"
        "Send /pnl for trade breakdown\n"
        "Send /balance for live balance"
    )


# ──────────────────────────────────────────────────────────────────
# Rich alert builders (called from main.py)
# ──────────────────────────────────────────────────────────────────

def build_trade_alert(candidate: dict, result: dict) -> str:
    mode  = result.get("mode", "live").upper()
    icon  = "🟢" if mode == "LIVE" else "📄"
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
    obs_str  = f"{obs}°F" if obs is not None else "N/A"
    fcst_str = f"{fcst}°F" if fcst is not None else "N/A"
    return (
        f"{icon} {mode} TRADE — LOCK-IN\n"
        f"{SEP}\n"
        f"Ticker : {candidate.get('ticker')}\n"
        f"Side   : {side}  |  City: {city} ({hz})\n"
        f"Obs    : {obs_str}  |  Forecast: {fcst_str}\n"
        f"Conf   : {conf}%  |  Edge: +{edge}¢\n"
        f"{SEP}\n"
        f"Entry  : {price}¢  x{qty} contracts\n"
        f"Cost   : ${cost:.2f}\n"
        f"Max Win: +${max_win:.2f}  |  Max Loss: -${max_loss:.2f}\n"
        f"TP @ {tp}¢  |  SL @ {sl}¢"
    )


def build_close_alert(result: dict, kalshi_client=None) -> str:
    action  = result.get("action", "CLOSE")
    icon    = "✅" if action == "TP" else "❌"
    label   = "TAKE PROFIT" if action == "TP" else "STOP LOSS"
    ticker  = result.get("ticker", "")
    side    = result.get("side", "").upper()
    qty     = result.get("qty", 0)
    entry   = result.get("entry_cents", 0)
    exit_c  = result.get("exit_cents", 0)
    pnl     = result.get("realized_usd", 0)
    pnl_pct = round((pnl / (entry * qty / 100)) * 100, 1) if entry and qty else 0

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

    bal_str = ""
    if kalshi_client:
        try:
            resp = kalshi_client.balance()
            bal  = (resp.get("balance", 0) + resp.get("portfolio_value", 0)) / 100.0
            bal_icon = "📈" if pnl >= 0 else "📉"
            bal_str = f"\n{SEP}\n{bal_icon} Balance: ${bal:.2f}"
        except Exception:
            pass

    return (
        f"{icon} POSITION CLOSED — {label}\n"
        f"{SEP}\n"
        f"Ticker : {ticker}\n"
        f"Side   : {side}  x{qty} contracts\n"
        f"Entry  : {entry}¢  →  Exit: {exit_c}¢\n"
        f"PnL    : ${pnl:+.2f}  ({pnl_pct:+.1f}%)"
        f"{hold_str}"
        f"{bal_str}"
    )


# ──────────────────────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Weather Bot v2 Commands\n"
        f"{SEP}\n"
        "/status      — bot health & mode\n"
        "/positions   — open positions\n"
        "/pnl         — today's realized PnL\n"
        "/balance     — live Kalshi balance\n"
        "/strategy    — how the bot works\n"
        "/pause       — stop trading\n"
        "/resume      — resume trading\n"
        "/setpaper    — switch to paper mode\n"
        "/setlive     — switch to live mode\n"
        "/mode        — current mode\n"
        f"{SEP}\n"
        "🛠 Admin\n"
        "/botlogs     — last 30 log lines\n"
        "/restartbot  — restart container (no rebuild)\n"
        "/rebuildbot  — rebuild + restart container\n"
        "/pullbot     — git pull + rebuild (deploy latest)\n"
        "/help        — this message"
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


# ── Admin shell commands ───────────────────────────────────────────
# Commands run on the HOST filesystem via the bind-mounted project dir.
# Docker CLI calls use the mounted docker socket.
# Only fixed allow-listed commands — no arbitrary shell execution.

def _run_shell(cmd: str, timeout: int = 60) -> str:
    """Run a shell command and return stdout+stderr, truncated to 3800 chars."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        out = (result.stdout + result.stderr).strip()
        if len(out) > 3800:
            out = "..." + out[-3800:]
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


@only_owner
async def cmd_botlogs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📥 Fetching last 30 log lines...")
    out = _run_shell("docker logs wb2-trader --tail 30 2>&1")
    await update.message.reply_text(f"📝 Logs:\n{SEP}\n{out}")


@only_owner
async def cmd_restartbot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Restarting container (no rebuild)...")
    out = _run_shell(f"cd {HOST_PROJECT_DIR} && docker compose restart", timeout=30)
    await update.message.reply_text(f"Done:\n{out}")


@only_owner
async def cmd_rebuildbot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔨 Rebuilding + restarting... (this takes ~30s)")
    out = _run_shell(f"cd {HOST_PROJECT_DIR} && docker compose up -d --build 2>&1", timeout=120)
    await update.message.reply_text(f"Done:\n{out}")


@only_owner
async def cmd_pullbot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📦 Pulling latest code + rebuilding... (this takes ~60s)")
    out = _run_shell(
        f"cd {HOST_PROJECT_DIR} && git pull 2>&1 && docker compose up -d --build 2>&1",
        timeout=180
    )
    await update.message.reply_text(f"Done:\n{out}")


# ──────────────────────────────────────────────────────────────────
# Notify helpers
# ──────────────────────────────────────────────────────────────────

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


async def send_daily_summary():
    await notify(fmt_daily_summary())


# ──────────────────────────────────────────────────────────────────
# Bot runner
# ──────────────────────────────────────────────────────────────────

async def run_bot_async():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("positions",   cmd_positions))
    app.add_handler(CommandHandler("pnl",         cmd_pnl))
    app.add_handler(CommandHandler("balance",     cmd_balance))
    app.add_handler(CommandHandler("strategy",    cmd_strategy))
    app.add_handler(CommandHandler("pause",       cmd_pause))
    app.add_handler(CommandHandler("resume",      cmd_resume))
    app.add_handler(CommandHandler("mode",        cmd_mode))
    app.add_handler(CommandHandler("setpaper",    cmd_setpaper))
    app.add_handler(CommandHandler("setlive",     cmd_setlive))
    app.add_handler(CommandHandler("botlogs",     cmd_botlogs))
    app.add_handler(CommandHandler("restartbot",  cmd_restartbot))
    app.add_handler(CommandHandler("rebuildbot",  cmd_rebuildbot))
    app.add_handler(CommandHandler("pullbot",     cmd_pullbot))

    # Register command menu so the / list in Telegram stays current
    async with Bot(TOKEN) as bot:
        await bot.set_my_commands(BOT_COMMANDS)

    print("[telegram] polling started — command menu registered")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await asyncio.sleep(1)
