"""Scanner v2: pulls Kalshi weather markets, persists strike_type, prices, target_date."""
import datetime as dt, re, yaml
from zoneinfo import ZoneInfo
from db import conn
from kalshi_client import KalshiClient

CITY_COORDS = {
    "NYC":  (40.7789, -73.9692),
    "LAX":  (33.9425, -118.4081),
    "CHI":  (41.7868, -87.7522),   # Midway (KMDW) — Kalshi settlement station
    "MIA":  (25.7959, -80.2870),
    "DEN":  (39.8561, -104.6737),
    "AUS":  (30.1975, -97.6664),
    "PHIL": (39.8729, -75.2437),
    "BOS":  (42.3606, -71.0106),
    "HOU":  (29.9844, -95.3414),
}

CITY_METAR = {
    "NYC":  "KNYC",
    "LAX":  "KLAX",
    "CHI":  "KMDW",   # Kalshi CLI settles on Midway, not O'Hare
    "MIA":  "KMIA",
    "DEN":  "KDEN",
    "AUS":  "KAUS",
    "PHIL": "KPHL",
    "BOS":  "KBOS",
    "HOU":  "KHOU",
}

CITY_TZ = {
    "NYC":  "America/New_York",
    "BOS":  "America/New_York",
    "PHIL": "America/New_York",
    "MIA":  "America/New_York",
    "CHI":  "America/Chicago",
    "HOU":  "America/Chicago",
    "AUS":  "America/Chicago",
    "DEN":  "America/Denver",
    "LAX":  "America/Los_Angeles",
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
    "RAIN":      ["KXRAIN"],
    "SNOW":      ["KXSNOW"],
    "WINDSPEED": ["KXHIGHWIND", "KXWIND"],
}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
)}

# ── Entry time windows (local hour, inclusive) ────────────────────────────────
SAME_DAY_ENTRY_START = 14.5  # 2:30 PM local — obs anchor strongest after 14:30
SAME_DAY_ENTRY_END   = 16.5  # 4:30 PM local — high usually in; pure lock-in window
NEXT_DAY_ENTRY_START = 6
NEXT_DAY_ENTRY_END   = 10


def log_event(level, module, message):
    with conn() as c:
        c.execute(
            "INSERT INTO events(ts,level,module,message) VALUES(?,?,?,?)",
            (dt.datetime.utcnow().isoformat(), level, module, message),
        )


def log_decision(row: dict):
    """Append one row to model_decisions. Foundation for empirical sigma
    calibration, Brier score reports, and replay backtest. Best-effort —
    never raises, never blocks the scan loop.
    """
    try:
        with conn() as c:
            c.execute(
                """
                INSERT INTO model_decisions(
                    ts, cycle_id, ticker, city, variable, horizon, target_date,
                    strike_type, strike_low, strike_high,
                    forecast_f, obs_f, effective_forecast, sigma_used,
                    model_prob_yes, market_mid, yes_bid, yes_ask,
                    edge_yes_cents, edge_no_cents, decision, entry_price_cents
                ) VALUES (?,?,?,?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)
                """,
                (
                    dt.datetime.utcnow().isoformat(),
                    row.get("cycle_id"),
                    row.get("ticker"),
                    row.get("city"),
                    row.get("variable"),
                    row.get("horizon"),
                    row.get("target_date"),
                    row.get("strike_type"),
                    row.get("strike_low"),
                    row.get("strike_high"),
                    row.get("forecast_f"),
                    row.get("obs_f"),
                    row.get("effective_forecast"),
                    row.get("sigma_used"),
                    row.get("model_prob_yes"),
                    row.get("market_mid"),
                    row.get("yes_bid"),
                    row.get("yes_ask"),
                    row.get("edge_yes_cents"),
                    row.get("edge_no_cents"),
                    row.get("decision"),
                    row.get("entry_price_cents"),
                ),
            )
    except Exception as e:
        # Never let logging break the trade loop.
        try:
            log_event("WARN", "scanner", f"log_decision failed: {str(e)[:160]}")
        except Exception:
            pass


def build_series_list(cfg):
    cities = [c["code"] for c in cfg["cities"]]
    series = []
    for var in cfg["markets"]:
        for pref in VAR_PREFIX.get(var, []):
            for our in cities:
                for kc in CITY_CODES.get(our, [our]):
                    series.append((var, our, pref + kc))
                    if pref in ("KXRAIN", "KXSNOW"):
                        series.append((var, our, pref + kc + "M"))
    seen = set()
    out = []
    for row in series:
        if row[2] in seen:
            continue
        seen.add(row[2])
        out.append(row)
    return out


RE_TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")


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


def horizon_of(target_date_iso: str, city: str = "NYC") -> str:
    """
    Determine horizon using LOCAL date for the city, not UTC.
    Prevents the bug where evening trades cross midnight UTC and
    get misclassified.
    """
    if not target_date_iso:
        return "unknown"
    try:
        td = dt.date.fromisoformat(target_date_iso)
        tz = ZoneInfo(CITY_TZ.get(city, "America/Chicago"))
        local_today = dt.datetime.now(tz).date()
        delta = (td - local_today).days
        if delta == 0:
            return "same_day"
        if delta == 1:
            return "next_day"
        if delta < 0:
            return "expired"
        return "weekly"
    except Exception:
        return "unknown"


def is_valid_entry_time(horizon: str, city: str) -> bool:
    """
    Gate entries by local time of day.
    same_day: 2:30PM-4:30PM local (obs-anchored lock-in window).
    """
    try:
        tz = ZoneInfo(CITY_TZ.get(city, "America/New_York"))
        now = dt.datetime.now(tz)
        hour = now.hour + now.minute / 60.0   # decimal local hour for fractional windows
        if horizon == "same_day":
            ok = SAME_DAY_ENTRY_START <= hour <= SAME_DAY_ENTRY_END
            if not ok:
                log_event("INFO", "scanner",
                          f"entry_time_block: {city} same_day local_hour={hour:.2f} "
                          f"window={SAME_DAY_ENTRY_START}-{SAME_DAY_ENTRY_END}")
            return ok
        if horizon == "next_day":
            return NEXT_DAY_ENTRY_START <= hour <= NEXT_DAY_ENTRY_END
        if horizon == "expired":
            return False
        return True
    except Exception:
        return True


def _d2c(v):
    if v is None or v == "":
        return None
    try:
        return int(round(float(v) * 100))
    except Exception:
        return None


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
    if nb is None: nb = m.get("no_bid")
    if na is None: na = m.get("no_ask")
    if last is None: last = m.get("last_price")
    return yb, ya, nb, na, last


def fetch_single(k, ticker):
    try:
        r = k.get_market(ticker)
        return r.get("market") or r
    except Exception:
        return {}


def score_candidates(markets, noaa_client, metar_client, cfg):
    """
    Score each market against NOAA forecast + METAR obs anchor.

    VETERAN RULES ENFORCED:
    1. max_entry_no_cents  — never pay >75c for a NO (bad risk/reward trap)
    2. min_reward_ratio    — profit potential must be >= 25c per $1 risked
    3. Sizing favors cheap high-edge trades over expensive "sure things"
    """
    from model import sigma_for, time_adjusted_sigma, adjusted_forecast, yes_prob, market_mid_prob
    risk = cfg.get("risk", {})
    min_model_prob      = float(risk.get("min_model_prob",    0.80))
    min_edge_cents      = float(risk.get("min_edge_cents",    6))
    min_entry_cents     = float(risk.get("min_entry_cents",   20))
    max_entry_cents     = float(risk.get("max_entry_cents",   90))
    max_entry_no_cents  = float(risk.get("max_entry_no_cents", 75))
    min_reward_ratio    = float(risk.get("min_reward_ratio",   0.25))

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

        if var not in ("HIGHTEMP", "LOWTEMP"):
            continue
        if ya is None or yb is None:
            continue

        coords = CITY_COORDS.get(city)
        if not coords:
            continue

        # Recompute horizon with local date
        hz = horizon_of(m.get("target_date", ""), city)

        if hz not in set(cfg.get("horizons", ["same_day"])):
            continue

        if not is_valid_entry_time(hz, city):
            continue

        try:
            periods = noaa_client.forecast(coords[0], coords[1])
            tdate   = m.get("target_date")
            forecast_f = None
            for p in periods:
                if p["startTime"][:10] == tdate and p["isDaytime"]:
                    forecast_f = p["temperature"]
                    break
            if forecast_f is None:
                for p in periods:
                    if p["isDaytime"]:
                        forecast_f = p["temperature"]
                        break
        except Exception:
            continue

        obs_f = None
        try:
            station = CITY_METAR.get(city)
            if station and metar_client:
                obs = metar_client.latest(station)
                obs_f = obs.get("temp_f")
        except Exception:
            pass

        effective_forecast = adjusted_forecast(forecast_f, obs_f, var, hz)
        base_sigma = sigma_for(var, hz)
        sigma      = time_adjusted_sigma(base_sigma, hz, city)

        mp  = yes_prob(effective_forecast, sigma, st, lo, hi)
        if mp is None:
            continue

        mid = market_mid_prob(yb, ya)
        if mid is None:
            continue

        edge_yes = (mp - mid) * 100
        edge_no  = ((1 - mp) - (1 - mid)) * 100

        # Capture decision for the model_decisions log.
        # Default outcome is 'skip:<reason>'; mutated below if we add to candidates.
        decision_row = {
            "ticker":             ticker,
            "city":               city,
            "variable":           var,
            "horizon":            hz,
            "target_date":        m.get("target_date"),
            "strike_type":        st,
            "strike_low":         lo,
            "strike_high":        hi,
            "forecast_f":         forecast_f,
            "obs_f":              obs_f,
            "effective_forecast": effective_forecast,
            "sigma_used":         round(sigma, 3) if sigma is not None else None,
            "model_prob_yes":     round(mp, 4),
            "market_mid":         round(mid, 4),
            "yes_bid":            yb,
            "yes_ask":            ya,
            "edge_yes_cents":     round(edge_yes, 2),
            "edge_no_cents":      round(edge_no, 2),
            "decision":           "skip:no_branch_taken",
            "entry_price_cents":  None,
        }

        if mp < min_model_prob and (1 - mp) < min_model_prob:
            decision_row["decision"] = "skip:below_min_model_prob"
            log_decision(decision_row)
            continue

        # YES side
        if edge_yes >= min_edge_cents and min_entry_cents <= ya <= max_entry_cents:
            reward_ratio = (100 - ya) / ya if ya > 0 else 0
            if reward_ratio < min_reward_ratio:
                log_event("INFO", "scanner",
                          f"reward_ratio_block YES {ticker}: entry={ya}c ratio={reward_ratio:.2f} min={min_reward_ratio}")
                decision_row["decision"] = f"skip:reward_ratio_yes:{reward_ratio:.2f}"
                log_decision(decision_row)
                continue
            decision_row["decision"] = "candidate_yes"
            decision_row["entry_price_cents"] = ya
            log_decision(decision_row)
            candidates.append({  # noqa: log_decision happens above
                "ticker":             ticker,
                "side":               "yes",
                "variable":           var,
                "city":               city,
                "horizon":            hz,
                "strike_type":        st,
                "strike_low":         lo,
                "strike_high":        hi,
                "yes_bid":            yb,
                "yes_ask":            ya,
                "model_prob":         mp,
                "market_mid":         mid,
                "edge_cents":         round(edge_yes, 2),
                "forecast_f":         forecast_f,
                "effective_forecast": effective_forecast,
                "obs_f":              obs_f,
                "sigma_used":         round(sigma, 2),
                "price_cents":        ya,
                "reward_ratio":       round(reward_ratio, 3),
                "target_date":        m.get("target_date"),
            })

        # YES had edge but failed price gate (too cheap or too expensive).
        # Log it so empirical sigma calibration sees the boundary cases too.
        elif edge_yes >= min_edge_cents:
            decision_row["decision"] = f"skip:yes_price_oob:{ya}"
            log_decision(decision_row)
            continue

        # NO side
        elif edge_no >= min_edge_cents:
            no_ask = 100 - yb
            # Veteran rule: never buy NO above max_entry_no_cents
            if no_ask > max_entry_no_cents:
                log_event("INFO", "scanner",
                          f"no_price_block {ticker}: no_ask={no_ask}c max={max_entry_no_cents}c")
                decision_row["decision"] = f"skip:no_price_block:{no_ask}"
                log_decision(decision_row)
                continue
            if not (min_entry_cents <= no_ask <= max_entry_cents):
                decision_row["decision"] = f"skip:no_price_oob:{no_ask}"
                log_decision(decision_row)
                continue
            reward_ratio = (100 - no_ask) / no_ask if no_ask > 0 else 0
            if reward_ratio < min_reward_ratio:
                log_event("INFO", "scanner",
                          f"reward_ratio_block NO {ticker}: entry={no_ask}c ratio={reward_ratio:.2f} min={min_reward_ratio}")
                decision_row["decision"] = f"skip:reward_ratio_no:{reward_ratio:.2f}"
                log_decision(decision_row)
                continue
            decision_row["decision"] = "candidate_no"
            decision_row["entry_price_cents"] = no_ask
            log_decision(decision_row)
            candidates.append({
                "ticker":             ticker,
                "side":               "no",
                "variable":           var,
                "city":               city,
                "horizon":            hz,
                "strike_type":        st,
                "strike_low":         lo,
                "strike_high":        hi,
                "yes_bid":            yb,
                "yes_ask":            ya,
                "model_prob":         1 - mp,
                "market_mid":         1 - mid,
                "edge_cents":         round(edge_no, 2),
                "forecast_f":         forecast_f,
                "effective_forecast": effective_forecast,
                "obs_f":              obs_f,
                "sigma_used":         round(sigma, 2),
                "price_cents":        no_ask,
                "reward_ratio":       round(reward_ratio, 3),
                "target_date":        m.get("target_date"),
            })

        # Neither YES nor NO had enough edge. Still log so we have the
        # full distribution for calibration (including 'boring' markets).
        else:
            decision_row["decision"] = "skip:no_edge_either_side"
            log_decision(decision_row)

    candidates.sort(key=lambda x: x["edge_cents"], reverse=True)
    return candidates


def scan_once(config_path="/app/config.yaml"):
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    k = KalshiClient()
    allowed_hz = set(cfg["horizons"])
    series_list = build_series_list(cfg)
    collected = []
    tried = hits = enriched = 0
    for var, our_city, series_ticker in series_list:
        tried += 1
        try:
            resp = k.get_markets(series_ticker=series_ticker, status="open", limit=200)
        except Exception as e:
            if "404" not in str(e) and "not_found" not in str(e).lower():
                log_event("WARN", "scanner", f"{series_ticker}: {str(e)[:120]}")
            continue
        ms = resp.get("markets", [])
        if not ms:
            continue
        hits += 1
        for m in ms:
            tdate = target_date_from_ticker(m.get("ticker", ""))
            hz = horizon_of(tdate, our_city)
            if hz not in allowed_hz:
                continue
            fs = m.get("floor_strike")
            cs = m.get("cap_strike")
            st = m.get("strike_type")
            yb, ya, nb, na, last = extract_prices(m)
            if yb is None and ya is None and nb is None and na is None:
                full = fetch_single(k, m["ticker"])
                if full:
                    enriched += 1
                    yb, ya, nb, na, last = extract_prices(full)
                    fs = fs if fs is not None else full.get("floor_strike")
                    cs = cs if cs is not None else full.get("cap_strike")
                    st = st or full.get("strike_type")
            collected.append({
                "ticker": m["ticker"], "series": series_ticker,
                "city": our_city, "variable": var, "horizon": hz,
                "target_date": tdate, "strike_type": st,
                "strike_low": float(fs) if fs is not None else None,
                "strike_high": float(cs) if cs is not None else None,
                "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na,
                "last_price": last, "close_time": m.get("close_time"),
                "status": m.get("status", "active"),
            })
    now_iso = dt.datetime.utcnow().isoformat()
    with conn() as c:
        for r in collected:
            c.execute(
                "INSERT INTO markets(ticker,series,city,variable,horizon,target_date,strike_type,"
                "strike_low,strike_high,yes_bid,yes_ask,no_bid,no_ask,last_price,resolution_time,status,last_seen) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "series=excluded.series, city=excluded.city, variable=excluded.variable, "
                "horizon=excluded.horizon, target_date=excluded.target_date, "
                "strike_type=excluded.strike_type, strike_low=excluded.strike_low, "
                "strike_high=excluded.strike_high, yes_bid=excluded.yes_bid, "
                "yes_ask=excluded.yes_ask, no_bid=excluded.no_bid, no_ask=excluded.no_ask, "
                "last_price=excluded.last_price, resolution_time=excluded.resolution_time, "
                "status=excluded.status, last_seen=excluded.last_seen",
                (r["ticker"], r["series"], r["city"], r["variable"], r["horizon"],
                 r["target_date"], r["strike_type"], r["strike_low"], r["strike_high"],
                 r["yes_bid"], r["yes_ask"], r["no_bid"], r["no_ask"], r["last_price"],
                 r.get("close_time"), r["status"], now_iso),
            )
    log_event("INFO", "scanner",
              f"probed {tried} series, {hits} hit, {len(collected)} markets, {enriched} enriched")
    return collected


def scan(kalshi_client=None, noaa_client=None, metar_client=None, config_path="/app/config.yaml"):
    """Main entry point called by main.py."""
    from noaa_client import NoaaClient
    from metar_client import MetarClient
    import yaml
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    if noaa_client is None:
        noaa_client = NoaaClient()
    if metar_client is None:
        metar_client = MetarClient()
    markets = scan_once(config_path=config_path)
    return score_candidates(markets, noaa_client, metar_client, cfg)


if __name__ == "__main__":
    rows = scan_once()
    print(f"Total: {len(rows)}")
    for r in rows[:10]:
        print(r["ticker"], r["city"], r["variable"], r["horizon"],
              "target=", r["target_date"], "st=", r["strike_type"],
              "lo=", r["strike_low"], "hi=", r["strike_high"],
              "yb=", r["yes_bid"], "ya=", r["yes_ask"])
