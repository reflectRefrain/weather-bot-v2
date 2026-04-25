# Weather Bot v2 — Phone + PC Cheat Sheet

## Current Safe State

Recommended normal testing state:

```text
container: healthy
mode: paper
kill_switch: OFF
open_positions: 0
```

If anything feels wrong, use Telegram:

```text
/pause
```

---

## Phone Commands

Use these in Telegram.

### Status

```text
/status
```

Shows bot status, mode, position count, and time.

### Positions

```text
/positions
```

Shows open positions.

### Pause Bot

```text
/pause
```

Turns kill switch ON. Bot stops opening new trades.

### Resume Bot

```text
/resume
```

Turns kill switch OFF. Bot can run again.

### Mode

```text
/mode
```

Shows paper/live mode.

### Paper Mode

```text
/setpaper
```

Switches to paper mode.

### Live Mode

```text
/setlive
```

Switches to live mode. Be careful.

### Help

```text
/help
```

Shows command list.

---

## PC / VPS Basics

Connect from PC:

```bash
ssh root@YOUR_VPS_IP
```

Go to bot folder:

```bash
cd ~/weather-bot-v2
```

Check container:

```bash
docker compose ps
```

View recent logs:

```bash
docker compose logs trader --tail=30
```

Follow live logs:

```bash
docker compose logs -f trader
```

Stop following logs:

```text
CTRL + C
```

---

## Fast State Check

```bash
docker compose exec -T trader python - << 'PY'
from executor import kill_switch_on, get_state
from reconcile import open_positions
print("kill_switch:", "ON" if kill_switch_on() else "OFF")
print("mode:", get_state("mode"))
print("open_positions:", len(open_positions()))
PY
```

Good result:

```text
kill_switch: OFF
mode: paper
open_positions: 0
```

---

## Restart Bot

Normal restart:

```bash
docker compose down
docker compose up -d
docker compose ps
```

Restart after code changes:

```bash
docker compose down
docker compose up -d --build
docker compose ps
```

---

## Analytics Reports

Summary:

```bash
docker compose exec -T trader python /app/src/report.py summary
```

Today:

```bash
docker compose exec -T trader python /app/src/report.py today
```

PnL:

```bash
docker compose exec -T trader python /app/src/report.py pnl
```

Positions:

```bash
docker compose exec -T trader python /app/src/report.py positions
```

Orders:

```bash
docker compose exec -T trader python /app/src/report.py orders
```

Events:

```bash
docker compose exec -T trader python /app/src/report.py events
```

Export CSVs:

```bash
docker compose exec -T trader python /app/src/report.py export
docker compose exec -T trader ls -lah /app/data/exports
```

---

## Night Owl Routine

Since I work 3rd shift, use this routine instead of a normal morning routine.

### Before Sleep

```bash
cd ~/weather-bot-v2
docker compose ps
docker compose logs trader --tail=30
docker compose exec -T trader python /app/src/report.py summary
```

If I want the bot to keep paper testing:

```text
mode: paper
kill_switch: OFF
```

If I want no activity while sleeping:

```text
/pause
```

### After Wake Up

```bash
cd ~/weather-bot-v2
docker compose logs trader --tail=80
docker compose exec -T trader python /app/src/report.py today
docker compose exec -T trader python /app/src/report.py positions
docker compose exec -T trader python /app/src/report.py pnl
```

### During Work

Use Telegram:

```text
/status
/positions
```

Emergency:

```text
/pause
```

---

## Git Safety

Before changing anything:

```bash
cd ~/weather-bot-v2
git status --short
git add .
git commit -m "Backup before changes"
git push
```

After safe changes:

```bash
git status --short
git add FILE_NAME
git commit -m "Describe change"
git push
```

Create stable branch:

```bash
git branch stable-paper-v1
git push origin stable-paper-v1
```

Rollback to a previous commit:

```bash
git log --oneline -5
git reset --hard COMMIT_ID
docker compose down
docker compose up -d --build
```

---

## Files To Avoid Editing Casually

Core bot files:

```text
src/main.py
src/executor.py
src/market_scanner.py
src/telegram_bot.py
config.yaml
docker-compose.yml
```

Safe files:

```text
src/report.py
docs/*
README.md
notes/*
```

---

## Emergency Checklist

If something looks wrong:

1. Telegram:

```text
/pause
```

2. VPS:

```bash
cd ~/weather-bot-v2
docker compose ps
docker compose logs trader --tail=50
docker compose exec -T trader python /app/src/report.py summary
```

3. Verify:

```text
kill_switch: ON
mode: paper
```

4. Do not switch live unless intentionally planned.

---

## Most Important Rules

```text
/pause = emergency stop
mode: paper = no real orders
git push = backup
report.py = safe analytics
core files = do not touch unless planned
```
