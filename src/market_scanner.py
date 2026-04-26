"""Scanner v2: pulls Kalshi weather markets, persists strike_type, prices, target_date."""
import datetime as dt, re, yaml
from db import conn
from kalshi_client import KalshiClient
from model import sigma_for, yes_prob, market_mid_prob

CITY_CODES = {
    "NYC": ["NY", "NYC"], "LAX": ["LAX", "LA"], "CHI": ["CHI"],
    "MIA": ["MIA"], "DEN": ["DEN"], "AUS": ["AUS"],
    "PHIL": ["PHIL"], "BOS": ["BOS"],
}

VAR_PREFIX = {
    "HIGHTEMP": ["KXHIGH"], "LOWTEMP": ["KXLOW"],
    "RAIN": ["KXRAIN"], "SNOW": ["KXSNOW"],
    "WINDSPEED": ["KXHIGHWIND", "KXWIND"],
}

MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

def log_event(level, module, message):
    with conn() as c:
        c.execute(
            "INSERT INTO events(ts,level,module,message) VALUES(?,?,?,?)",
            (dt.datetime.utcnow().isoformat(), level, module, message),
        )

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

def parse_strike(ticker: str) -> dict:
    """
    Returns dict with keys: type, value, floor, cap
    type: 'above' | 'between' | 'unknown'
    Examples:
      KXHIGHNY-26APR25-T65     -> above, value=65
      KXHIGHNY-26APR25-B65     -> above (NO side = below), value=65
      KXHIGHNY-26APR25-R64T66  -> between, floor=64, cap=66
    """
    try:
        parts = ticker.split("-")
        sp = parts[-1]
        # Between: starts with digit, contains T separator, e.g. 64T66 or R64T66
        m = re.match(r'^R?(\d+\.?\d*)T(\d+\.?\d*)$', sp)
        if m:
            return {"type": "between", "floor": float(m.group(1)), "cap": float(m.group(2)), "value": None}
        # Above threshold: T65 or just 65 (Kalshi uses T for >=)
        m = re.match(r'^T(\d+\.?\d*)$', sp)
        if m:
            return {"type": "above", "value": float(m.group(1)), "floor": None, "cap": None}
        # Below threshold: B65
        m = re.match(r'^B(\d+\.?\d*)$', sp)
        if m:
            # YES = below; we treat as "above" the complement for model
            return {"type": "above", "value": float(m.group(1)), "floor": None, "cap": None, "_below": True}
        # Bare number
        m = re.match(r'^(\d+\.?\d*)$', sp)
        if m:
            return {"type": "above", "value": float(m.group(1)), "floor": None, "cap": None}
    except Exception:
        pass
    return {"type": "unknown"}

def target_date_from_ticker(ticker: str):
    """Extract YYYY-MM-DD from e.g. KXHIGHNY-26APR25-T65"""
    try:
        parts = ticker.split("-")
        raw = parts[1]
        return dt.datetime.strptime(raw, "%d%b%y").strftime("%Y-%m-%d")
    except Exception:
        pass
    try:
        # alternate: YYYYMMDD
        parts = ticker.split("-")
        raw = parts[1]
        return dt.datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return None

def cents(m: dict, key: str):
    v = m.get(key)
    if v is not None:
        return int(round(float(v)))
    dv = m.get(key + "_dollars")
    if dv is not None:
        return int(round(float(dv) * 100))
    return None

def spread_c(yes_bid, yes_ask):
    if yes_bid is None or yes_ask is None:
        return None
    return yes_ask - yes_bid

def upsert_market(ticker, series_ticker, variable, city, strike_type,
                  floor_strike, cap_strike, value_strike, tgt_date,
                  yes_bid, yes_ask, status="open"):
    with conn() as c:
        c.execute("""
            INSERT INTO markets
                (ticker, series_ticker, variable, city,
                 strike_type, floor_strike, cap_strike, value_strike,
                 target_date, yes_bid, yes_ask, status, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
                yes_bid=excluded.yes_bid,
                yes_ask=excluded.yes_ask,
                status=excluded.status,
                updated_at=excluded.updated_at
        """, (
            ticker, series_ticker, variable, city,
            strike_type, floor_strike, cap_strike, value_strike,
            tgt_date, yes_bid, yes_ask, status,
            dt.datetime.utcnow().isoformat()
        ))

def scan(kalshi_client, noaa_client, metar_client,
         config_path="/app/config.yaml") -> list:
    """
    Scanner v2 main entry.
    Returns list of candidate dicts sorted by edge_cents desc.
    """
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    risk       = cfg["risk"]
    cities_cfg = {c["code"]: c for c in cfg["cities"]}
    min_edge   = risk["min_edge_cents"]
    max_spread = risk["max_spread_cents"]
    min_price  = risk["min_entry_cents"]
    max_price  = risk["max_entry_cents"]

    series_list = build_series_list(cfg)
    log_event("INFO", "scanner", f"Scanning {len(series_list)} series")

    collected = []
    for (variable, city_code, series_ticker) in series_list:
        try:
            resp = kalshi_client.get_markets(
                series_ticker=series_ticker, status="open", limit=200
            )
            mlist = resp.get("markets", [])
        except Exception as e:
            log_event("WARN", "scanner", f"{series_ticker}: {e}")
            continue
        for m in mlist:
            m["_variable"]   = variable
            m["_city_code"]  = city_code
            m["_series_ticker"] = series_ticker
        collected.extend(mlist)

    log_event("INFO", "scanner", f"Collected {len(collected)} raw markets")

    # Enrich with strike info, prices, target date
    enriched = 0
    for m in collected:
        ticker = m.get("ticker", "")
        yb = cents(m, "yes_bid")
        ya = cents(m, "yes_ask")
        tgt_date = target_date_from_ticker(ticker)
        si = parse_strike(ticker)

        upsert_market(
            ticker         = ticker,
            series_ticker  = m["_series_ticker"],
            variable       = m["_variable"],
            city           = m["_city_code"],
            strike_type    = si["type"],
            floor_strike   = si.get("floor"),
            cap_strike     = si.get("cap"),
            value_strike   = si.get("value"),
            tgt_date       = tgt_date,
            yes_bid        = yb,
            yes_ask        = ya,
        )
        enriched += 1

    log_event("INFO", "scanner", f"Enriched {enriched} markets")

    # Score candidates
    candidates = []
    for m in collected:
        ticker     = m.get("ticker", "")
        variable   = m["_variable"]
        city_code  = m["_city_code"]
        city_cfg   = cities_cfg.get(city_code)
        if not city_cfg:
            continue

        yes_bid = cents(m, "yes_bid")
        yes_ask = cents(m, "yes_ask")
        tgt_date = target_date_from_ticker(ticker)
        strike_info = parse_strike(ticker)

        if strike_info["type"] == "unknown":
            continue
        if yes_bid is None or yes_ask is None:
            continue
        sp = spread_c(yes_bid, yes_ask)
        if sp is not None and sp > max_spread:
            continue

        # Get forecast
        try:
            if variable == "HIGHTEMP":
                forecast = noaa_client.high_f(city_cfg["lat"], city_cfg["lon"])
            elif variable == "LOWTEMP":
                forecast = noaa_client.low_f(city_cfg["lat"], city_cfg["lon"])
            else:
                forecast = noaa_client.high_f(city_cfg["lat"], city_cfg["lon"])
            if forecast is None:
                continue
        except Exception:
            continue

        # Get live obs
        try:
            obs_f = metar_client.temp_f(city_cfg["metar"])
        except Exception:
            obs_f = None

        # Score both YES and NO sides
        for side in ("yes", "no"):
            if side == "yes":
                price_c = yes_ask
            else:
                price_c = 100 - yes_bid

            if not (min_price <= price_c <= max_price):
                continue

            strike_val = strike_info.get("value") or strike_info.get("cap")
            if strike_val is None:
                continue

            # Determine horizon
            horizon = "next_day"
            if tgt_date:
                delta = (dt.date.fromisoformat(tgt_date) - dt.datetime.utcnow().date()).days
                if delta <= 0:
                    horizon = "same_day"
                elif delta == 1:
                    horizon = "next_day"
                else:
                    horizon = "weekly"

            sigma = sigma_for(variable, horizon)
            st    = strike_info["type"]
            floor = strike_info.get("floor")
            cap   = strike_info.get("cap")
            val   = strike_info.get("value")
            fs    = floor if floor is not None else val
            cs    = cap

            model_p = yes_prob(forecast, sigma, st, fs, cs)
            if model_p is None:
                continue

            # For B-type (below) tickers, YES means below => invert
            if strike_info.get("_below"):
                model_p = 1.0 - model_p

            market_p = market_mid_prob(yes_bid, yes_ask)
            if market_p is None:
                continue

            if side == "yes":
                ec = (model_p - market_p) * 100
            else:
                ec = ((1 - model_p) - (1 - market_p)) * 100

            if ec < min_edge:
                continue

            candidates.append({
                "ticker":      ticker,
                "variable":    variable,
                "city":        city_code,
                "side":        side,
                "price_cents": price_c,
                "model_prob":  round(model_p, 4),
                "market_prob": round(market_p, 4),
                "edge_cents":  round(ec, 2),
                "forecast_f":  forecast,
                "obs_f":       obs_f,
                "tgt_date":    tgt_date,
                "spread_c":    sp,
                "horizon":     horizon,
                "yes_bid":     yes_bid,
                "yes_ask":     yes_ask,
            })

    candidates.sort(key=lambda x: x["edge_cents"], reverse=True)
    log_event("INFO", "scanner", f"Done: {len(collected)} markets, {enriched} enriched, {len(candidates)} candidates")
    return candidates
