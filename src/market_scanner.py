"""Scanner v2: pulls Kalshi weather markets, persists strike_type, prices, target_date."""
import datetime as dt, re, yaml
from db import conn
from kalshi_client import KalshiClient

CITY_COORDS = {
    "NYC":  (40.7789, -73.9692),
    "LAX":  (33.9425, -118.4081),
    "CHI":  (41.9742, -87.9073),
    "MIA":  (25.7959, -80.2870),
    "DEN":  (39.8561, -104.6737),
    "AUS":  (30.1975, -97.6664),
    "PHIL": (39.8729, -75.2437),
    "BOS":  (42.3606, -71.0106),
}

CITY_METAR = {
    "NYC":  "KNYC",
    "LAX":  "KLAX",
    "CHI":  "KORD",
    "MIA":  "KMIA",
    "DEN":  "KDEN",
    "AUS":  "KAUS",
    "PHIL": "KPHL",
    "BOS":  "KBOS",
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

MIN_EDGE_CENTS = 10
MIN_ENTRY_CENTS = 5
MAX_ENTRY_CENTS = 90


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


def horizon_of(target_date_iso):
    if not target_date_iso:
        return "unknown"
    try:
        td = dt.date.fromisoformat(target_date_iso)
        today = dt.datetime.utcnow().date()
        delta = (td - today).days
        if delta <= 0:
            return "same_day"
        if delta == 1:
            return "next_day"
        return "weekly"
    except Exception:
        return "unknown"


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
    """Score each market dict against NOAA forecast and return sorted candidates."""
    from model import sigma_for, yes_prob, market_mid_prob
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

        try:
            periods = noaa_client.forecast(coords[0], coords[1])
            tdate   = m.get("target_date")
            forecast_f = None
            for p in periods:
                if p["startTime"][:10] == tdate and p["isDaytime"]:
                    forecast_f = p["temperature"]
                    break
            if forecast_f is None:
                # fallback: first daytime period
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

        sigma = sigma_for(var, hz)
        mp    = yes_prob(forecast_f, sigma, st, lo, hi)
        if mp is None:
            continue

        mid   = market_mid_prob(yb, ya)
        if mid is None:
            continue

        edge_yes = (mp - mid) * 100
        edge_no  = ((1 - mp) - (1 - mid)) * 100

        if edge_yes >= MIN_EDGE_CENTS and MIN_ENTRY_CENTS <= ya <= MAX_ENTRY_CENTS:
            candidates.append({
                "ticker":      ticker,
                "side":        "yes",
                "variable":    var,
                "city":        city,
                "horizon":     hz,
                "strike_type": st,
                "strike_low":  lo,
                "strike_high": hi,
                "yes_bid":     yb,
                "yes_ask":     ya,
                "model_prob":  mp,
                "market_mid":  mid,
                "edge_cents":  round(edge_yes, 2),
                "forecast_f":  forecast_f,
                "obs_f":       obs_f,
                "price_cents": ya,
            })
        elif edge_no >= MIN_EDGE_CENTS and MIN_ENTRY_CENTS <= (100 - yb) <= MAX_ENTRY_CENTS:
            no_ask = 100 - yb
            candidates.append({
                "ticker":      ticker,
                "side":        "no",
                "variable":    var,
                "city":        city,
                "horizon":     hz,
                "strike_type": st,
                "strike_low":  lo,
                "strike_high": hi,
                "yes_bid":     yb,
                "yes_ask":     ya,
                "model_prob":  1 - mp,
                "market_mid":  1 - mid,
                "edge_cents":  round(edge_no, 2),
                "forecast_f":  forecast_f,
                "obs_f":       obs_f,
                "price_cents": no_ask,
            })

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
            hz = horizon_of(tdate)
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
    """Main entry point called by main.py. Fetches markets, scores, returns candidates."""
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
