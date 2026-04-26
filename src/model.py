"""Probability model — realistic sigmas based on NWS forecast error studies."""
from statistics import NormalDist

# same_day ~3.5F error by morning, next_day ~4.5F, weekly ~6.5F
SIGMA = {
    "HIGHTEMP":  {"same_day": 3.5, "next_day": 4.5, "weekly": 6.5},
    "LOWTEMP":   {"same_day": 3.0, "next_day": 4.0, "weekly": 6.0},
    "RAIN":      {"same_day": 0.15, "next_day": 0.22, "weekly": 0.38},
    "SNOW":      {"same_day": 0.40, "next_day": 0.65, "weekly": 1.30},
    "WINDSPEED": {"same_day": 3.0,  "next_day": 4.5,  "weekly": 7.0},
}

def sigma_for(variable, horizon):
    v = SIGMA.get(variable, {"same_day": 3.5, "next_day": 4.5, "weekly": 6.5})
    return v.get(horizon, v["next_day"])

def yes_prob(forecast, sigma, strike_type, floor_strike, cap_strike):
    if forecast is None or sigma is None or sigma <= 0:
        return None
    nd = NormalDist(mu=forecast, sigma=sigma)
    st = (strike_type or "").lower()
    if st == "greater" and floor_strike is not None:
        return max(0.0, min(1.0, 1.0 - nd.cdf(float(floor_strike))))
    if st == "less" and cap_strike is not None:
        return max(0.0, min(1.0, nd.cdf(float(cap_strike))))
    if st == "between" and floor_strike is not None and cap_strike is not None:
        return max(0.0, min(1.0, nd.cdf(float(cap_strike)) - nd.cdf(float(floor_strike) - 1e-9)))
    return None

def market_mid_prob(yes_bid_cents, yes_ask_cents):
    if yes_bid_cents is None or yes_ask_cents is None:
        return None
    return ((yes_bid_cents + yes_ask_cents) / 2.0) / 100.0
