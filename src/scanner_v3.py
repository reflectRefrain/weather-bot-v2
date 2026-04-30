"""Scanner v3 — lock-in strategy.

Only trades near-certain outcomes based on METAR running max/min.
Does NOT use NOAA forecasts (no prediction, observation only).

Scoring logic:
  1. Pull running max/min from daily_obs (built by metar_tracker)
  2. Compute physical bounds on where the daily extreme can still go
  3. If outcome is mathematically locked (p >= 0.98), add to candidates
  4. Skip everything uncertain — if in doubt, do NOT trade
"""
import datetime as dt
import yaml
from zoneinfo import ZoneInfo
from db import conn
from kalshi_client import KalshiClient
from metar_tracker import get_obs, refresh_all_cities, CITY_TZ, CITY_METAR
from model_v3 import lockin_probability, hours_until_lock


CITY_COORDS = {
    "NYC":  (40.7789, -73.9692),
    "LAX":  (33.9425, -118.4081),
    "CHI":  (41.9742, -87.9073),
    "MIA":  (25.7959, -80.2870),
    "DEN":  (39.8561, -104.6737),
    "AUS":  (30.1975, -97.6664),
    "PHIL": (39.8729, -75.2437),
    "BOS":  (42.3606, -71.0106),
    "HOU":  (29.9844, -95.3414),
}

CITY_CODES = {
    "NYC":  ["NY", "NYC"],
    "LAX":  ["LAX", "LA"],
    "CHI":  ["CHI"],
    "MIA":  ["MIA"],
    "DEN":  ["DEN"],
    "AUS":  ["AUS"],
    "PHIL": ["PHIL"],
    "BOS":  ["BOS"],
    "HOU":  ["HOU"],
}

VAR_PREFIX = {
    "HIGHTEMP":  ["KXHIGH"],
    "LOWTEMP":   ["KXLOW"],
}

import re
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
)}
RE_TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")


def log_event(level, module, message):
    with conn() as c:
        c.execute(
            "INSERT INTO events(ts,level,module,message) VALUES(?,?,?,?)",
            (dt.datetime.utcnow().isoformat(), level, module, message),
        )


def target_date_from_ticker(tk):
    m = RE_TICKER_DATE.search(tk or "")
    if not m:
        return None
    yy = 2000 + int(m.group(1))
    mo = MONTHS.get(m.group(2))
    dd = int(m.group(3))
    if not mo:
        return None
    try:
        return dt.date(yy, mo, dd).isoformat()
    except Exception:
        return None


def horizon_of(target_date_iso):
    if not target_date_iso:
        return "unknown"
    try:
        td    = dt.date.fromisoformat(target_date_iso)
        today = dt.datetime.utcnow().date()
        delta = (td - today).days
        if delta <= 0:  return "same_day"
        if delta == 1:  return "next_day"
        return "weekly"
    except Exception:
        return "unknown"


def _d2c(v):
    if v is None or v == "": return None
    try:    return int(round(float(v) * 100))
    except: return None


def extract_prices(m):
    yb = _d2c(m.get("yes_bid_dollars"))
    ya = _d2c(m.get("yes_ask_dollars"))
    nb = _d2c(m.get("no_bid_dollars"))
    na = _d2c(m.get("no_ask_dollars"))
    last = _d2c(m.get("last_price_dollars"))
    if yb is None and na is not None: yb = 100 - na
    if ya is None and nb is not None: ya = 100 - nb
    if nb is None and ya is not None: nb = 100 - ya
    if na is None and yb is not None: na = 100 - yb
    if yb is None: yb = m.get("yes_bid")
    if ya is None: ya = m.get("yes_ask")
    return yb, ya, nb, na, last


def score_candidates_v3(markets, metar_client, cfg):
    """Score markets using lock-in logic only.

    Rules:
      - Only same_day markets (outcome observable today)
      - Only HIGHTEMP and LOWTEMP
      - Only trade if p_yes >= 0.98 (YES) or p_yes <= 0.02 (NO)
      - Buy YES if ya <= MAX_PAY_CENTS, edge = 100 - ya
      - Buy NO  if no_ask <= MAX_PAY_CENTS, edge = 100 - no_ask
      - Skip if obs data is missing or stale (< 2 readings today)
    """
    risk          = cfg.get("risk", {})
    MIN_LOCKIN    = float(risk.get("min_lockin_prob",  0.98))
    MAX_PAY       = int(risk.get("max_pay_cents",      97))
    MIN_PAY       = int(risk.get("min_pay_cents",       3))
    MIN_EDGE      = int(risk.get("min_edge_cents",       2))
    MIN_OBS_COUNT = int(risk.get("min_obs_count",        2))  # need at least 2 METAR readings

    candidates = []
    for m in markets:
        var    = m.get("variable")
        city   = m.get("city")
        hz     = m.get("horizon")
        st     = m.get("strike_type")
        lo     = m.get("strike_low")
        hi     = m.get("strike_high")
        yb     = m.get("yes_bid")
        ya     = m.get("yes_ask")
        ticker = m.get("ticker", "")

        if hz != "same_day":                   continue
        if var not in ("HIGHTEMP", "LOWTEMP"): continue
        if None in (yb, ya, st):               continue

        # Get today's running obs — skip if we haven't seen enough readings
        obs = get_obs(city)
        if obs is None:                        continue
        if obs.get("obs_count", 0) < MIN_OBS_COUNT: continue

        # Hours until physical lock
        tz        = ZoneInfo(CITY_TZ.get(city, "America/New_York"))
        now_local = dt.datetime.now(tz)
        hrs       = hours_until_lock(now_local, var)

        p_yes = lockin_probability(
            obs["running_max_f"], obs["running_min_f"], obs["current_f"],
            lo, hi, st, var, hrs,
        )

        # ── Locked YES ────────────────────────────────────────────────────
        if p_yes >= MIN_LOCKIN:
            if MIN_PAY <= ya <= MAX_PAY:
                edge = 100 - ya
                if edge >= MIN_EDGE:
                    candidates.append({
                        "ticker":       ticker, "side": "yes",
                        "variable":     var,    "city": city,
                        "horizon":      hz,     "strike_type": st,
                        "strike_low":   lo,     "strike_high": hi,
                        "yes_bid":      yb,     "yes_ask": ya,
                        "model_prob":   p_yes,  "edge_cents": edge,
                        "price_cents":  ya,
                        "running_max":  obs["running_max_f"],
                        "running_min":  obs["running_min_f"],
                        "hours_to_lock": round(hrs, 2),
                    })

        # ── Locked NO ─────────────────────────────────────────────────────
        elif p_yes <= (1.0 - MIN_LOCKIN):
            no_ask = 100 - yb
            if MIN_PAY <= no_ask <= MAX_PAY:
                edge = 100 - no_ask
                if edge >= MIN_EDGE:
                    candidates.append({
                        "ticker":       ticker, "side": "no",
                        "variable":     var,    "city": city,
                        "horizon":      hz,     "strike_type": st,
                        "strike_low":   lo,     "strike_high": hi,
                        "yes_bid":      yb,     "yes_ask": ya,
                        "model_prob":   1 - p_yes, "edge_cents": edge,
                        "price_cents":  no_ask,
                        "running_max":  obs["running_max_f"],
                        "running_min":  obs["running_min_f"],
                        "hours_to_lock": round(hrs, 2),
                    })
        # p_yes between 0.02 and 0.98 → uncertain → skip entirely

    candidates.sort(key=lambda x: x["edge_cents"], reverse=True)
    return candidates


def build_series_list(cfg):
    cities = [c["code"] for c in cfg["cities"]]
    series = []
    for var in cfg["markets"]:
        for pref in VAR_PREFIX.get(var, []):
            for our in cities:
                for kc in CITY_CODES.get(our, [our]):
                    series.append((var, our, pref + kc))
    seen = set()
    out  = []
    for row in series:
        if row[2] in seen: continue
        seen.add(row[2])
        out.append(row)
    return out


def scan_once_v3(config_path="/app/config.yaml"):
    """Fetch all same-day markets from Kalshi and return raw market dicts."""
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    k           = KalshiClient()
    series_list = build_series_list(cfg)
    collected   = []
    for var, our_city, series_ticker in series_list:
        try:
            resp = k.get_markets(series_ticker=series_ticker, status="open", limit=200)
        except Exception as e:
            if "404" not in str(e) and "not_found" not in str(e).lower():
                log_event("WARN", "scanner_v3", f"{series_ticker}: {str(e)[:120]}")
            continue
        ms = resp.get("markets", [])
        if not ms: continue
        for m in ms:
            tdate = target_date_from_ticker(m.get("ticker", ""))
            hz    = horizon_of(tdate)
            if hz != "same_day": continue   # v3 only same_day
            fs = m.get("floor_strike")
            cs = m.get("cap_strike")
            st = m.get("strike_type")
            yb, ya, nb, na, last = extract_prices(m)
            collected.append({
                "ticker":      m["ticker"], "series":    series_ticker,
                "city":        our_city,    "variable":  var,
                "horizon":     hz,          "target_date": tdate,
                "strike_type": st,
                "strike_low":  float(fs) if fs is not None else None,
                "strike_high": float(cs) if cs is not None else None,
                "yes_bid":     yb, "yes_ask": ya,
                "no_bid":      nb, "no_ask":  na,
                "last_price":  last,
                "close_time":  m.get("close_time"),
                "status":      m.get("status", "active"),
            })
    log_event("INFO", "scanner_v3", f"same_day markets fetched: {len(collected)}")
    return collected


def scan_v3(metar_client=None, config_path="/app/config.yaml"):
    """Main entry point — called by main.py when strategy=lockin_v3."""
    from metar_client import MetarClient
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    if metar_client is None:
        metar_client = MetarClient()
    # Refresh all METAR obs first
    refresh_all_cities(metar_client)
    markets = scan_once_v3(config_path=config_path)
    return score_candidates_v3(markets, metar_client, cfg)
