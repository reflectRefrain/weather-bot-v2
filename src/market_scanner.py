"""
Market Scanner — finds Kalshi weather markets and scores them.
Returns a list of candidate trades with edge calculations.
"""
import datetime as dt, yaml, math
from typing import Optional

SERIES = [
    "HIGHTEMP", "LOWTEMP",
    "KXHIGH", "KXLOW",
]

def load_config(path="/app/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

def mid(yes_bid, yes_ask) -> Optional[float]:
    if yes_bid is None or yes_ask is None:
        return None
    return (yes_bid + yes_ask) / 2.0

def spread(yes_bid, yes_ask) -> Optional[float]:
    if yes_bid is None or yes_ask is None:
        return None
    return yes_ask - yes_bid

def cents(m: dict, key: str) -> Optional[int]:
    """
    Kalshi may return either cent fields (yes_bid) or dollar-string fields
    (yes_bid_dollars). Normalize both to integer cents.
    """
    v = m.get(key)
    if v is not None:
        return int(round(float(v)))
    dv = m.get(key + "_dollars")
    if dv is not None:
        return int(round(float(dv) * 100))
    return None

def gaussian_prob(forecast_f: float, strike: float, sigma: float = 4.0) -> float:
    """
    P(actual >= strike) using normal distribution around forecast.
    sigma=4.0 is a conservative default (typical NWS MAE is 3-5F).
    """
    from statistics import NormalDist
    dist = NormalDist(mu=forecast_f, sigma=sigma)
    return 1.0 - dist.cdf(strike)

def edge_cents(model_prob: float, market_mid_cents: float, side: str) -> float:
    """
    Edge = model probability - market implied probability, in cents.
    side='yes': we buy YES if model says higher prob than market
    side='no':  we buy NO  if model says lower  prob than market
    """
    market_prob = market_mid_cents / 100.0
    if side == "yes":
        return (model_prob - market_prob) * 100
    else:
        return ((1 - model_prob) - (1 - market_prob)) * 100

def kelly_size(edge_c: float, price_c: float, bankroll: float,
               kelly_fraction: float = 0.20,
               min_usd: float = 1.0, max_usd: float = 2.0) -> float:
    """
    Fractional Kelly sizing.
    Returns dollar amount to bet, clamped to [min_usd, max_usd].
    """
    if price_c <= 0 or price_c >= 100:
        return 0.0
    p   = price_c / 100.0
    q   = 1.0 - p
    b   = (100 - price_c) / price_c   # net odds
    k   = (b * p - q) / b
    raw = bankroll * kelly_fraction * max(k, 0)
    return max(min_usd, min(max_usd, raw))

def parse_strike(ticker: str) -> dict:
    """
    Extract strike info from ticker string.
    e.g. KXHIGHNY-26APR25-B65.5 -> {type:'below', value:65.5}
         KXHIGHNY-26APR25-T65.5 -> {type:'above', value:65.5}
         KXHIGHNY-26APR25-R64T66 -> {type:'range', low:64, high:66}
    """
    try:
        parts = ticker.split("-")
        strike_part = parts[-1]
        if strike_part.startswith("B"):
            return {"type": "below", "value": float(strike_part[1:])}
        elif strike_part.startswith("T"):
            return {"type": "above", "value": float(strike_part[1:])}
        elif "T" in strike_part and strike_part[0] == "R":
            lo, hi = strike_part[1:].split("T")
            return {"type": "range", "low": float(lo), "high": float(hi)}
    except Exception:
        pass
    return {"type": "unknown"}

def target_date_from_ticker(ticker: str) -> Optional[str]:
    """Extract YYYY-MM-DD from ticker like KXHIGHNY-26APR25-B65"""
    try:
        parts = ticker.split("-")
        raw = parts[1]   # e.g. 26APR25
        return dt.datetime.strptime(raw, "%y%b%d").strftime("%Y-%m-%d")
    except Exception:
        return None

def scan(kalshi_client, noaa_client, metar_client,
         config_path="/app/config.yaml") -> list:
    """
    Main scan — returns list of candidate dicts, best edge first.
    Each candidate:
      ticker, series, city, side, price_cents, model_prob,
      market_prob, edge_cents, kelly_usd, strike, forecast_f,
      obs_f, reason
    """
    cfg       = load_config(config_path)
    risk      = cfg["risk"]
    cities    = {c["code"]: c for c in cfg["cities"]}
    candidates = []

    min_edge   = risk["min_edge_cents"]
    max_spread = risk["max_spread_cents"]
    min_price  = risk["min_entry_cents"]
    max_price  = risk["max_entry_cents"]
    bankroll   = cfg["bankroll_usd"]
    kelly_f    = risk["kelly_fraction"]
    min_usd    = risk["min_trade_usd"]
    max_usd    = risk["max_trade_usd"]

    # Fetch weather markets directly by Kalshi weather series.
    # Global /markets pages are often dominated by sports and may not include weather.
    series_list = [
        "KXHIGHNY",
        "KXHIGHLAX",
        "KXHIGHCHI",
        "KXHIGHMIA",
        "KXHIGHDEN",
        "KXHIGHAUS",
        "KXHIGHPHIL",
        "KXHIGHBOS",
    ]

    weather = []
    try:
        for series in series_list:
            resp = kalshi_client.get_markets(series_ticker=series, status="open", limit=200)
            weather.extend(resp.get("markets", []))
    except Exception as e:
        return [{"error": str(e)}]

    for m in weather:
        ticker     = m.get("ticker", "")
        yes_bid    = cents(m, "yes_bid")
        yes_ask    = cents(m, "yes_ask")
        tgt_date   = target_date_from_ticker(ticker)
        strike_info = parse_strike(ticker)

        if strike_info["type"] == "unknown":
            continue
        if yes_bid is None or yes_ask is None:
            continue
        if spread(yes_bid, yes_ask) > max_spread:
            continue

        # Match city from ticker
        city_code = None
        city_cfg  = None
        for code, cfg_city in cities.items():
            if code.upper() in ticker.upper():
                city_code = code
                city_cfg  = cfg_city
                break
        if not city_cfg:
            continue

        # Get forecast
        try:
            forecast = noaa_client.high_f(city_cfg["lat"], city_cfg["lon"])
            if forecast is None:
                continue
        except Exception:
            continue

        # Get live METAR obs
        try:
            obs_f = metar_client.temp_f(city_cfg["metar"])
        except Exception:
            obs_f = None

        # Score both YES and NO sides
        for side in ("yes", "no"):
            if side == "yes":
                price_c = yes_ask   # we'd pay the ask to buy YES
            else:
                price_c = 100 - yes_bid  # NO price = 100 - yes_bid

            if not (min_price <= price_c <= max_price):
                continue

            strike_val = strike_info.get("value") or strike_info.get("high")
            if strike_val is None:
                continue

            if strike_info["type"] == "above":
                model_p = gaussian_prob(forecast, strike_val)
            elif strike_info["type"] == "below":
                model_p = 1.0 - gaussian_prob(forecast, strike_val)
            else:
                continue   # skip range for now

            ec = edge_cents(model_p, mid(yes_bid, yes_ask), side)
            if ec < min_edge:
                continue

            kelly_usd = kelly_size(ec, price_c, bankroll, kelly_f, min_usd, max_usd)
            if kelly_usd <= 0:
                continue

            candidates.append({
                "ticker":     ticker,
                "side":       side,
                "price_cents": price_c,
                "model_prob":  round(model_p, 4),
                "market_prob": round(mid(yes_bid, yes_ask) / 100, 4),
                "edge_cents":  round(ec, 2),
                "kelly_usd":   round(kelly_usd, 2),
                "strike":      strike_info,
                "forecast_f":  forecast,
                "obs_f":       obs_f,
                "tgt_date":    tgt_date,
                "spread_c":    spread(yes_bid, yes_ask),
            })

    # Best edge first
    candidates.sort(key=lambda x: x["edge_cents"], reverse=True)
    return candidates
