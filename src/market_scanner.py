"""Scanner v2: pulls Kalshi weather markets, persists strike_type, prices, target_date."""
import datetime as dt, math, re, yaml
from zoneinfo import ZoneInfo
from db import conn
from kalshi_client import KalshiClient

# ─────────────────────────────────────────────────────────────────────────────
#  Full 18-city Kalshi HIGHTEMP coverage — added by morning-window PR.
#  Verified series tickers come from backtest/series_map.py (manual probe of
#  the Kalshi public series endpoint).
# ─────────────────────────────────────────────────────────────────────────────
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
    # ─ added 2026-05-08 in morning-window PR ─
    "ATL":  (33.6407, -84.4277),
    "PHX":  (33.4373, -112.0078),
    "DC":   (38.8512, -77.0402),    # KDCA — Reagan, NOT Dulles
    "LAS":  (36.0840, -115.1537),
    "SAT":  (29.5337, -98.4698),
    "MIN":  (44.8848, -93.2223),
    "DAL":  (32.8998, -97.0403),    # KDFW
    "SF":   (37.6213, -122.3790),   # KSFO
    "OKC":  (35.3931, -97.6007),
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
    "ATL":  "KATL",
    "PHX":  "KPHX",
    "DC":   "KDCA",
    "LAS":  "KLAS",
    "SAT":  "KSAT",
    "MIN":  "KMSP",
    "DAL":  "KDFW",
    "SF":   "KSFO",
    "OKC":  "KOKC",
}

CITY_TZ = {
    "NYC":  "America/New_York",
    "BOS":  "America/New_York",
    "PHIL": "America/New_York",
    "MIA":  "America/New_York",
    "DC":   "America/New_York",
    "ATL":  "America/New_York",
    "CHI":  "America/Chicago",
    "HOU":  "America/Chicago",
    "AUS":  "America/Chicago",
    "DAL":  "America/Chicago",
    "SAT":  "America/Chicago",
    "MIN":  "America/Chicago",
    "OKC":  "America/Chicago",
    "DEN":  "America/Denver",
    "PHX":  "America/Phoenix",      # MST, no DST
    "LAX":  "America/Los_Angeles",
    "LAS":  "America/Los_Angeles",
    "SF":   "America/Los_Angeles",
}

# Maps our internal city code -> the Kalshi series tail used to build series
# tickers via VAR_PREFIX. Verified May 2026 — 9 of these use the new KXHIGHT*
# prefix family rather than the original KXHIGH* family.
CITY_CODES = {
    "NYC":  ["NY", "NYC"],
    "LAX":  ["LAX", "LA"],
    "CHI":  ["CHI"],
    "MIA":  ["MIA"],
    "DEN":  ["DEN"],
    "AUS":  ["AUS"],          # KXHIGHAUS
    "PHIL": ["PHIL"],         # KXHIGHPHIL
    "BOS":  ["TBOS"],         # KXHIGHTBOS
    "HOU":  ["THOU"],         # KXHIGHTHOU
    "ATL":  ["TATL"],         # KXHIGHTATL
    "PHX":  ["TPHX"],         # KXHIGHTPHX
    "DC":   ["TDC"],          # KXHIGHTDC
    "LAS":  ["TLV"],          # KXHIGHTLV  (Las Vegas → LV)
    "SAT":  ["TSATX"],        # KXHIGHTSATX (San Antonio → SATX)
    "MIN":  ["TMIN"],         # KXHIGHTMIN
    "DAL":  ["TDAL"],         # KXHIGHTDAL
    "SF":   ["TSFO"],         # KXHIGHTSFO
    "OKC":  ["TOKC"],         # KXHIGHTOKC
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
# 2026-05-08: Morning-window PR. The afternoon backtest at 14:30-16:30 LOCAL
# (the prior window) showed every strategy unprofitable — mid_band at -39% EV,
# lockin_yes at -4% EV. The only window with measured positive EV across 90 days
# / 18 cities was 4:00-9:00 LOCAL: mid_band peaks at +19% EV (hour 4) and
# averages +6-12% EV across hours 4-8; lockin_yes is +4-9% EV across hours 4-10.
# By 9am the edge has decayed and goes negative through the afternoon as the
# day's high becomes increasingly observable.
SAME_DAY_ENTRY_START = 4.0   # 4:00 AM local — peak edge for both books
SAME_DAY_ENTRY_END   = 9.0   # 9:00 AM local — edge has decayed to ~0 by then
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
                    forecast_f, obs_f, projected_high_f, projection_method,
                    nbm_high_f, model_disagreement_f,
                    effective_forecast, sigma_used,
                    model_prob_yes, market_mid, yes_bid, yes_ask,
                    edge_yes_cents, edge_no_cents, decision, book_type, entry_price_cents
                ) VALUES (?,?,?,?,?,?,?, ?,?,?, ?,?,?,?, ?,?, ?,?, ?,?,?,?, ?,?,?,?,?)
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
                    row.get("projected_high_f"),
                    row.get("projection_method"),
                    row.get("nbm_high_f"),
                    row.get("model_disagreement_f"),
                    row.get("effective_forecast"),
                    row.get("sigma_used"),
                    row.get("model_prob_yes"),
                    row.get("market_mid"),
                    row.get("yes_bid"),
                    row.get("yes_ask"),
                    row.get("edge_yes_cents"),
                    row.get("edge_no_cents"),
                    row.get("decision"),
                    row.get("book_type"),
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


def score_candidates(markets, noaa_client, metar_client, cfg, nbm_client=None):
    """
    Score each market against NOAA forecast + METAR obs anchor.

    Tier 2.3 two-book candidate emission:
      - LOCK-IN BOOK: YES side, model_prob >= 0.85, price <= 92c, strike
        within 2F of effective forecast, min_reward_ratio 0.10. The bread
        and butter — small edge, frequent fills, high hit rate.
      - TAIL-SHORT BOOK: NO side, model_prob_NO >= 0.95, NO price 15-30c,
        strike >= 4F from effective forecast, min_reward_ratio 1.5. Picks
        up the small percentage of the time when an unhinged tail closes
        cheap and we can cash on it disqualifying.
      - LEGACY "either side has edge" path stays as a fallback so we keep
        non-extreme trades flowing while the new books accumulate data.

    VETERAN RULES ENFORCED (legacy path):
    1. max_entry_no_cents  — never pay >75c for a NO (bad risk/reward trap)
    2. min_reward_ratio    — profit potential must be >= 25c per $1 risked
    3. Sizing favors cheap high-edge trades over expensive "sure things"
    """
    from model import (sigma_for, time_adjusted_sigma, adjusted_forecast,
                       yes_prob, market_mid_prob, disagreement_sigma_bonus)
    risk = cfg.get("risk", {})
    min_model_prob      = float(risk.get("min_model_prob",    0.80))
    min_edge_cents      = float(risk.get("min_edge_cents",    6))
    min_entry_cents     = float(risk.get("min_entry_cents",   20))
    max_entry_cents     = float(risk.get("max_entry_cents",   90))
    max_entry_no_cents  = float(risk.get("max_entry_no_cents", 75))
    min_reward_ratio    = float(risk.get("min_reward_ratio",   0.25))

    # Tier 2.3 book filter parameters — overridable from config.yaml under risk:
    lockin_min_prob       = float(risk.get("lockin_min_prob",        0.85))
    lockin_max_price      = float(risk.get("lockin_max_price_cents", 92))
    lockin_max_dist_f     = float(risk.get("lockin_max_dist_f",      2.0))
    lockin_min_reward     = float(risk.get("lockin_min_reward_ratio", 0.10))
    lockin_fee_buffer     = float(risk.get("lockin_fee_buffer",      0.02))
    lockin_tie_buffer_f   = float(risk.get("lockin_tie_buffer_f",    1.0))
    tail_min_prob_no      = float(risk.get("tail_min_prob_no",       0.95))
    tail_no_price_min     = float(risk.get("tail_no_price_min",      15))
    tail_no_price_max     = float(risk.get("tail_no_price_max",      30))
    tail_min_dist_f       = float(risk.get("tail_min_dist_f",        4.0))
    tail_min_reward       = float(risk.get("tail_min_reward_ratio",  1.5))

    # MID_BAND BOOK — added by morning-window PR (2026-05-08).
    # Pure price-driven entry, no model dependency. Backtest finding:
    # YES @ 40-49¢, spread ≤ 2¢, between-strikes, HIGHTEMP only, skip CHI/DC/ATL
    # produced +18-23% EV across 90 days / 246 trades when entered in the
    # 4-9am LOCAL window. Tightly bounded — do not loosen without re-validating.
    mid_band_enabled       = bool(risk.get("mid_band_enabled",            False))
    mid_band_price_min     = float(risk.get("mid_band_price_min",         40))
    mid_band_price_max     = float(risk.get("mid_band_price_max",         49))
    mid_band_max_spread    = float(risk.get("mid_band_max_spread_cents",  2))
    mid_band_excluded      = set(risk.get("mid_band_excluded_cities",     ["CHI", "DC", "ATL"]))

    # Strategy gating — when False, only the two named books fire and the
    # generic edge scanner below is bypassed (decisions still logged as skipped).
    enable_legacy_edge_path = bool(risk.get("enable_legacy_edge_path", False))

    # Optional half-size mode for legacy candidates (Option B). When True, the
    # executor halves max_per_ticker_usd / kelly_usd for any candidate tagged
    # book_type='legacy', so legacy keeps generating data at reduced risk while
    # we compare it head-to-head against the named books.
    legacy_half_size = bool(risk.get("legacy_half_size", True))

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
        projection = None
        try:
            station = CITY_METAR.get(city)
            if station and metar_client:
                obs = metar_client.latest(station)
                obs_f = obs.get("temp_f")
                # Same-day HIGHTEMP only — obs trajectory has no signal otherwise.
                if hz == "same_day" and var == "HIGHTEMP":
                    try:
                        projection = metar_client.project_high(station, city)
                    except Exception:
                        projection = None
        except Exception:
            pass

        # Tier 2.2: pull NBM (Pirate Weather) second-source forecast.
        # Best-effort — missing key, network down, no daily block all return None.
        nbm_high_f = None
        nbm_low_f = None
        try:
            if nbm_client and nbm_client.enabled():
                nbm = nbm_client.forecast_high(coords[0], coords[1], m.get("target_date"))
                if nbm:
                    nbm_high_f = nbm.get("high_f")
                    nbm_low_f = nbm.get("low_f")
        except Exception:
            pass

        # Pick the right NBM value for the variable — model.adjusted_forecast()
        # accepts a single nbm_high_f param that means "NBM same-direction value".
        nbm_for_var = nbm_high_f if var == "HIGHTEMP" else nbm_low_f

        effective_forecast = adjusted_forecast(
            forecast_f, obs_f, var, hz,
            projection=projection, nbm_high_f=nbm_for_var,
        )

        # Disagreement signal — only meaningful when both NWS and NBM exist.
        model_disagreement_f = None
        sigma_bonus = 0.0
        if forecast_f is not None and nbm_for_var is not None:
            model_disagreement_f = abs(forecast_f - nbm_for_var)
            sigma_bonus = disagreement_sigma_bonus(model_disagreement_f)

        base_sigma = sigma_for(var, hz)
        sigma      = time_adjusted_sigma(base_sigma, hz, city) + sigma_bonus

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
            "ticker":               ticker,
            "city":                 city,
            "variable":             var,
            "horizon":              hz,
            "target_date":          m.get("target_date"),
            "strike_type":          st,
            "strike_low":           lo,
            "strike_high":          hi,
            "forecast_f":           forecast_f,
            "obs_f":                obs_f,
            "projected_high_f":     (projection or {}).get("projected_high_f"),
            "projection_method":    (projection or {}).get("method"),
            "nbm_high_f":           nbm_for_var,
            "model_disagreement_f": round(model_disagreement_f, 3) if model_disagreement_f is not None else None,
            "effective_forecast":   effective_forecast,
            "sigma_used":           round(sigma, 3) if sigma is not None else None,
            "model_prob_yes":       round(mp, 4),
            "market_mid":           round(mid, 4),
            "yes_bid":              yb,
            "yes_ask":              ya,
            "edge_yes_cents":       round(edge_yes, 2),
            "edge_no_cents":        round(edge_no, 2),
            "decision":             "skip:no_branch_taken",
            "book_type":            None,
            "entry_price_cents":    None,
        }

        # ----------------------------------------------------------------
        # Tier 2.3: TWO-BOOK candidate emission. Try lock-in book first, then
        # tail-short book. Both can fire on the same scan against the same
        # market only if they target different sides (lock-in YES + tail NO).
        # `strike_distance_f` = |effective_forecast - strike midpoint or edge|
        # used for the distance-from-projection floor in tail shorts.
        # ----------------------------------------------------------------
        if effective_forecast is not None:
            strike_ref = None
            if st == "greater" and lo is not None:
                strike_ref = float(lo)
            elif st == "less" and hi is not None:
                strike_ref = float(hi)
            elif st == "between" and lo is not None and hi is not None:
                strike_ref = (float(lo) + float(hi)) / 2.0
            strike_distance_f = abs(effective_forecast - strike_ref) if strike_ref is not None else None
        else:
            strike_distance_f = None

        # ----- MID_BAND BOOK (YES @ 40-49¢) -----
        # Fires before lockin/tail. Price band is below lockin's 70-92¢ floor,
        # so this block cannot collide with a lockin candidate on the same
        # ticker. If mid_band fires we `continue` to prevent the legacy path
        # from also emitting on the same market.
        if (
            mid_band_enabled
            and var == "HIGHTEMP"
            and st == "between"
            and city not in mid_band_excluded
            and ya is not None and yb is not None
            and mid_band_price_min <= ya <= mid_band_price_max
            and (ya - yb) <= mid_band_max_spread
        ):
            mb_row = dict(decision_row)
            mb_row["decision"] = "candidate_yes"
            mb_row["book_type"] = "mid_band"
            mb_row["entry_price_cents"] = ya
            log_decision(mb_row)
            candidates.append({
                "ticker":             ticker,
                "side":               "yes",
                "book_type":          "mid_band",
                "variable":           var,
                "city":               city,
                "horizon":            hz,
                "strike_type":        st,
                "strike_low":         lo,
                "strike_high":        hi,
                "yes_bid":            yb,
                "yes_ask":            ya,
                "model_prob":         mp,           # logged for telemetry only
                "market_mid":         mid,
                "edge_cents":         round(edge_yes, 2),
                "forecast_f":         forecast_f,
                "effective_forecast": effective_forecast,
                "obs_f":              obs_f,
                "sigma_used":         round(sigma, 2) if sigma is not None else None,
                "price_cents":        ya,
                "reward_ratio":       round(((100 - ya) / ya) if ya > 0 else 0, 3),
                "target_date":        m.get("target_date"),
                "strike_distance_f":  round(strike_distance_f, 2) if strike_distance_f is not None else None,
            })
            continue

        # ----- LOCK-IN BOOK (YES) -----
        no_ask_for_market = 100 - yb if yb is not None else None

        # Fee-aware mp_yes floor — Kalshi taker fee = ceil(0.07 * P * (1-P)).
        # Net win at YES price ya: (1 - ya/100 - fee). Pure breakeven mp_yes
        # = (ya/100) / (1 - fee). We require model_prob to clear breakeven by
        # at least lockin_fee_buffer (default 0.02) on top of lockin_min_prob.
        # This kills the negative-EV band at high yes_prices (86c-92c) without
        # affecting cheaper entries where 0.85 already binds.
        if ya is not None and ya > 0:
            ya_dollars = ya / 100.0
            _fee_yes = math.ceil(0.07 * ya_dollars * (1 - ya_dollars) * 100) / 100
            mp_breakeven_yes = ya_dollars / max(1e-6, (1 - _fee_yes))
            mp_required_yes = max(lockin_min_prob, mp_breakeven_yes + lockin_fee_buffer)
        else:
            mp_required_yes = lockin_min_prob

        # Tie-buffer for Kalshi 'greater than' / 'less than' rules — these are
        # STRICTLY greater/less. A miss landing exactly on the strike resolves
        # against YES. Require the forecast to clear the strike by at least
        # lockin_tie_buffer_f (default 1.0 deg F) on the same side as YES wins.
        tie_safe = True
        if strike_ref is not None:
            if st == "greater":
                # YES wins when actual > strike_low. Forecast must exceed strike
                # by tie_buffer to absorb a 1F adverse miss without landing on tie.
                tie_safe = (effective_forecast - strike_ref) >= lockin_tie_buffer_f
            elif st == "less":
                # YES wins when actual < strike_high.
                tie_safe = (strike_ref - effective_forecast) >= lockin_tie_buffer_f
            # 'between' covers an interior band — tie-on-edge less of a concern
            # because a 1F miss off forecast typically still lands inside the band.

        if (
            mp >= mp_required_yes
            and ya is not None
            and ya <= lockin_max_price
            and ya >= min_entry_cents
            and strike_distance_f is not None
            and strike_distance_f <= lockin_max_dist_f
            and tie_safe
        ):
            reward_ratio_lock = (100 - ya) / ya if ya > 0 else 0
            if reward_ratio_lock >= lockin_min_reward:
                lock_row = dict(decision_row)
                lock_row["decision"] = "candidate_yes"
                lock_row["book_type"] = "lockin"
                lock_row["entry_price_cents"] = ya
                log_decision(lock_row)
                candidates.append({
                    "ticker":             ticker,
                    "side":               "yes",
                    "book_type":          "lockin",
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
                    "reward_ratio":       round(reward_ratio_lock, 3),
                    "target_date":        m.get("target_date"),
                    "strike_distance_f":  round(strike_distance_f, 2),
                })
                # Lock-in candidates do NOT also try tail-short for the same
                # ticker — a strike close to forecast can't simultaneously be
                # a far-tail short.
                continue

        # ----- TAIL-SHORT BOOK (NO) -----
        prob_no = 1.0 - mp
        if (
            prob_no >= tail_min_prob_no
            and no_ask_for_market is not None
            and tail_no_price_min <= no_ask_for_market <= tail_no_price_max
            and strike_distance_f is not None
            and strike_distance_f >= tail_min_dist_f
        ):
            reward_ratio_tail = (100 - no_ask_for_market) / no_ask_for_market if no_ask_for_market > 0 else 0
            if reward_ratio_tail >= tail_min_reward:
                tail_row = dict(decision_row)
                tail_row["decision"] = "candidate_no"
                tail_row["book_type"] = "tail_short"
                tail_row["entry_price_cents"] = no_ask_for_market
                log_decision(tail_row)
                candidates.append({
                    "ticker":             ticker,
                    "side":               "no",
                    "book_type":          "tail_short",
                    "variable":           var,
                    "city":               city,
                    "horizon":            hz,
                    "strike_type":        st,
                    "strike_low":         lo,
                    "strike_high":        hi,
                    "yes_bid":            yb,
                    "yes_ask":            ya,
                    "model_prob":         prob_no,
                    "market_mid":         1 - mid,
                    "edge_cents":         round(edge_no, 2),
                    "forecast_f":         forecast_f,
                    "effective_forecast": effective_forecast,
                    "obs_f":              obs_f,
                    "sigma_used":         round(sigma, 2),
                    "price_cents":        no_ask_for_market,
                    "reward_ratio":       round(reward_ratio_tail, 3),
                    "target_date":        m.get("target_date"),
                    "strike_distance_f":  round(strike_distance_f, 2),
                })
                continue

        # ----- LEGACY EITHER-SIDE EDGE PATH (fallback below) -----
        # Gated by config: when enable_legacy_edge_path is False, the path is
        # bypassed entirely. When True, candidates are tagged book_type='legacy'
        # so the executor can apply half-size (legacy_half_size flag).
        if not enable_legacy_edge_path:
            decision_row["decision"] = "skip:legacy_path_disabled"
            log_decision(decision_row)
            continue

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
                "book_type":          "legacy",
                "half_size":          legacy_half_size,
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
                "book_type":          "legacy",
                "half_size":          legacy_half_size,
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


def scan(kalshi_client=None, noaa_client=None, metar_client=None,
         nbm_client=None, config_path="/app/config.yaml"):
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
    if nbm_client is None:
        try:
            from nbm_client import NbmClient
            nbm_client = NbmClient()  # reads PIRATE_WEATHER_API_KEY from env
        except Exception:
            nbm_client = None
    markets = scan_once(config_path=config_path)
    return score_candidates(markets, noaa_client, metar_client, cfg, nbm_client=nbm_client)


if __name__ == "__main__":
    rows = scan_once()
    print(f"Total: {len(rows)}")
    for r in rows[:10]:
        print(r["ticker"], r["city"], r["variable"], r["horizon"],
              "target=", r["target_date"], "st=", r["strike_type"],
              "lo=", r["strike_low"], "hi=", r["strike_high"],
              "yb=", r["yes_bid"], "ya=", r["yes_ask"])
