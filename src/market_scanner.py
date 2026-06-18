"""Scanner v2: pulls Kalshi weather markets, persists strike_type, prices, target_date."""
import datetime as dt, math, re, yaml
from zoneinfo import ZoneInfo
from db import conn
from kalshi_client import KalshiClient

CITY_COORDS = {
    "NYC":  (40.7789, -73.9692),
    "LAX":  (33.9425, -118.4081),
    "CHI":  (41.7868, -87.7522),
    "MIA":  (25.7959, -80.2870),
    "DEN":  (39.8561, -104.6737),
    "AUS":  (30.1975, -97.6664),
    "PHIL": (39.8729, -75.2437),
    "BOS":  (42.3606, -71.0106),
    "HOU":  (29.9844, -95.3414),
    "ATL":  (33.6407, -84.4277),
    "PHX":  (33.4373, -112.0078),
    "DC":   (38.8512, -77.0402),
    "LAS":  (36.0840, -115.1537),
    "SAT":  (29.5337, -98.4698),
    "MIN":  (44.8848, -93.2223),
    "DAL":  (32.8998, -97.0403),
    "SF":   (37.6213, -122.3790),
    "OKC":  (35.3931, -97.6007),
}

CITY_METAR = {
    "NYC":  "KNYC",
    "LAX":  "KLAX",
    "CHI":  "KMDW",
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
    "PHX":  "America/Phoenix",
    "LAX":  "America/Los_Angeles",
    "LAS":  "America/Los_Angeles",
    "SF":   "America/Los_Angeles",
}

CITY_CODES = {
    "NYC":  ["NY", "NYC"],
    "LAX":  ["LAX", "LA"],
    "CHI":  ["CHI"],
    "MIA":  ["MIA"],
    "DEN":  ["DEN"],
    "AUS":  ["AUS"],
    "PHIL": ["PHIL"],
    "BOS":  ["TBOS"],
    "HOU":  ["THOU"],
    "ATL":  ["TATL"],
    "PHX":  ["TPHX"],
    "DC":   ["TDC"],
    "LAS":  ["TLV"],
    "SAT":  ["TSATX"],
    "MIN":  ["TMIN"],
    "DAL":  ["TDAL"],
    "SF":   ["TSFO"],
    "OKC":  ["TOKC"],
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

SAME_DAY_ENTRY_START = 4.0
SAME_DAY_ENTRY_END   = 9.0
NEXT_DAY_ENTRY_START = 6
NEXT_DAY_ENTRY_END   = 10


def log_event(level, module, message):
    with conn() as c:
        c.execute(
            "INSERT INTO events(ts,level,module,message) VALUES(?,?,?,?)",
            (dt.datetime.utcnow().isoformat(), level, module, message),
        )


def log_decision(row: dict):
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
    try:
        tz = ZoneInfo(CITY_TZ.get(city, "America/New_York"))
        now = dt.datetime.now(tz)
        hour = now.hour + now.minute / 60.0
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
    from model import (sigma_for, time_adjusted_sigma, adjusted_forecast,
                       yes_prob, market_mid_prob, disagreement_sigma_bonus)
    risk = cfg.get("risk", {})
    min_model_prob      = float(risk.get("min_model_prob",    0.80))
    min_edge_cents      = float(risk.get("min_edge_cents",    6))
    min_entry_cents     = float(risk.get("min_entry_cents",   20))
    max_entry_cents     = float(risk.get("max_entry_cents",   90))
    max_entry_no_cents  = float(risk.get("max_entry_no_cents", 75))
    min_reward_ratio    = float(risk.get("min_reward_ratio",   0.25))

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

    mid_band_enabled        = bool(risk.get("mid_band_enabled",           False))
    mid_band_price_min      = float(risk.get("mid_band_price_min",        40))
    mid_band_price_max      = float(risk.get("mid_band_price_max",        49))
    mid_band_max_spread     = float(risk.get("mid_band_max_spread_cents", 2))
    mid_band_excluded       = set(risk.get("mid_band_excluded_cities",    ["CHI", "DC", "ATL"]))
    mid_band_min_model_prob = float(risk.get("mid_band_min_model_prob",   0.48))

    enable_legacy_edge_path = bool(risk.get("enable_legacy_edge_path", False))
    legacy_half_size        = bool(risk.get("legacy_half_size", True))

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
                if hz == "same_day" and var == "HIGHTEMP":
                    try:
                        projection = metar_client.project_high(station, city)
                    except Exception:
                        projection = None
        except Exception:
            pass

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

        nbm_for_var = nbm_high_f if var == "HIGHTEMP" else nbm_low_f

        # FIX 2026-05-12: pass city= so adjusted_forecast uses the correct
        # local hour when computing projection_trust_weight. Without this,
        # all cities used America/New_York for the projection ramp.
        effective_forecast = adjusted_forecast(
            forecast_f, obs_f, var, hz,
            projection=projection, nbm_high_f=nbm_for_var, city=city,
        )

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
        if (
            mid_band_enabled
            and var == "HIGHTEMP"
            and st == "between"
            and city not in mid_band_excluded
            and ya is not None and yb is not None
            and mid_band_price_min <= ya <= mid_band_price_max
            and (ya - yb) <= mid_band_max_spread
            and mp >= mid_band_min_model_prob
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
                "model_prob":         mp,
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

        if ya is not None and ya > 0:
            ya_dollars = ya / 100.0
            _fee_yes = math.ceil(0.07 * ya_dollars * (1 - ya_dollars) * 100) / 100
            mp_breakeven_yes = ya_dollars / max(1e-6, (1 - _fee_yes))
            mp_required_yes = max(lockin_min_prob, mp_breakeven_yes + lockin_fee_buffer)
        else:
            mp_required_yes = lockin_min_prob

        tie_safe = True
        if strike_ref is not None:
            if st == "greater":
                tie_safe = (effective_forecast - strike_ref) >= lockin_tie_buffer_f
            elif st == "less":
                tie_safe = (strike_ref - effective_forecast) >= lockin_tie_buffer_f

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

        # ----- LEGACY PATH -----
        if not enable_legacy_edge_path:
            decision_row["decision"] = "skip:legacy_path_disabled"
            log_decision(decision_row)
            continue

        if mp < min_model_prob and (1 - mp) < min_model_prob:
            decision_row["decision"] = "skip:below_min_model_prob"
            log_decision(decision_row)
            continue

        if edge_yes >= min_edge_cents and min_entry_cents <= ya <= max_entry_cents:
            reward_ratio = (100 - ya) / ya if ya > 0 else 0
            if reward_ratio < min_reward_ratio:
                decision_row["decision"] = f"skip:reward_ratio_yes:{reward_ratio:.2f}"
                log_decision(decision_row)
                continue
            decision_row["decision"] = "candidate_yes"
            decision_row["entry_price_cents"] = ya
            log_decision(decision_row)
            candidates.append({
                "ticker": ticker, "side": "yes", "variable": var, "city": city,
                "horizon": hz, "strike_type": st, "strike_low": lo, "strike_high": hi,
                "yes_bid": yb, "yes_ask": ya, "model_prob": mp, "market_mid": mid,
                "edge_cents": round(edge_yes, 2), "forecast_f": forecast_f,
                "effective_forecast": effective_forecast, "obs_f": obs_f,
                "sigma_used": round(sigma, 2), "price_cents": ya,
                "reward_ratio": round(reward_ratio, 3), "target_date": m.get("target_date"),
                "book_type": "legacy", "half_size": legacy_half_size,
            })
        elif edge_yes >= min_edge_cents:
            decision_row["decision"] = f"skip:yes_price_oob:{ya}"
            log_decision(decision_row)
        elif edge_no >= min_edge_cents:
            no_ask = 100 - yb
            if no_ask > max_entry_no_cents:
                decision_row["decision"] = f"skip:no_price_block:{no_ask}"
                log_decision(decision_row)
                continue
            if not (min_entry_cents <= no_ask <= max_entry_cents):
                decision_row["decision"] = f"skip:no_price_oob:{no_ask}"
                log_decision(decision_row)
                continue
            reward_ratio = (100 - no_ask) / no_ask if no_ask > 0 else 0
            if reward_ratio < min_reward_ratio:
                decision_row["decision"] = f"skip:reward_ratio_no:{reward_ratio:.2f}"
                log_decision(decision_row)
                continue
            decision_row["decision"] = "candidate_no"
            decision_row["entry_price_cents"] = no_ask
            log_decision(decision_row)
            candidates.append({
                "ticker": ticker, "side": "no", "variable": var, "city": city,
                "horizon": hz, "strike_type": st, "strike_low": lo, "strike_high": hi,
                "yes_bid": yb, "yes_ask": ya, "model_prob": 1 - mp, "market_mid": 1 - mid,
                "edge_cents": round(edge_no, 2), "forecast_f": forecast_f,
                "effective_forecast": effective_forecast, "obs_f": obs_f,
                "sigma_used": round(sigma, 2), "price_cents": no_ask,
                "reward_ratio": round(reward_ratio, 3), "target_date": m.get("target_date"),
                "book_type": "legacy", "half_size": legacy_half_size,
            })
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
            nbm_client = NbmClient()
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
