"""Probability model: sigma table + yes_prob + market_mid_prob."""
import datetime as dt
from statistics import NormalDist

SIGMA = {
    "HIGHTEMP":  {"same_day": 5.0, "next_day": 8.0, "weekly": 12.0},
    "LOWTEMP":   {"same_day": 5.0, "next_day": 8.0, "weekly": 12.0},
    "RAIN":      {"same_day": 0.10, "next_day": 0.18, "weekly": 0.35},
    "SNOW":      {"same_day": 0.35, "next_day": 0.60, "weekly": 1.20},
    "WINDSPEED": {"same_day": 5.0,  "next_day": 8.0,  "weekly": 15.0},
}


def sigma_for(variable: str, horizon: str, target_date: str = None) -> float:
    """
    Returns sigma for the given variable and horizon.
    For same_day, shrinks sigma as the day progresses (time-decay).
    target_date: YYYY-MM-DD string (used to check if it's today).
    """
    v = SIGMA.get(variable, {"same_day": 2.0, "next_day": 3.0, "weekly": 5.0})
    base = v.get(horizon, v["next_day"])

    if horizon == "same_day":
        now_utc = dt.datetime.utcnow()
        # CDT = UTC-5; normalize to local hour 0-23
        local_hour = (now_utc.hour - 5) % 24
        # Shrink linearly from full sigma at midnight -> 30% at 6pm (hour 18)
        frac = min(1.0, local_hour / 18.0)
        base = max(1.5, base * (1.0 - 0.6 * frac))

    return base


def yes_prob(forecast, sigma, strike_type, floor_strike, cap_strike):
    """
    Returns P(YES) given forecast, sigma, and strike structure.
    strike_type: 'above' | 'between'
    floor_strike: lower bound (or the threshold for 'above')
    cap_strike:   upper bound (only for 'between')
    """
    if forecast is None or sigma is None or sigma <= 0:
        return None
    nd = NormalDist(mu=forecast, sigma=sigma)
    st = (strike_type or "").lower()

    if st == "above" and floor_strike is not None:
        return max(0.0, min(1.0, 1.0 - nd.cdf(float(floor_strike))))

    if st == "between" and floor_strike is not None and cap_strike is not None:
        return max(0.0, min(1.0, nd.cdf(float(cap_strike)) - nd.cdf(float(floor_strike) - 1e-9)))

    # fallback: treat as above
    strike = floor_strike if floor_strike is not None else cap_strike
    if strike is not None:
        return max(0.0, min(1.0, 1.0 - nd.cdf(float(strike))))

    return None


def market_mid_prob(yes_bid_cents, yes_ask_cents):
    if yes_bid_cents is None or yes_ask_cents is None:
        return None
    return ((yes_bid_cents + yes_ask_cents) / 2.0) / 100.0
