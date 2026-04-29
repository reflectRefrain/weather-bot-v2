"""Probability model — time-adjusted sigma + METAR obs anchor.

Key improvements over v1:
  1. sigma shrinks during same_day as the day progresses (less uncertainty at 3PM vs 7AM)
  2. METAR obs anchors the effective forecast floor/ceil
     (if it's already 82F, the daily high CANNOT be below 82F)
  3. min_model_prob gate: don't trade unless model is >= MIN_MODEL_PROB confident
"""
import datetime as dt
from statistics import NormalDist
from zoneinfo import ZoneInfo

# Base sigmas — represent OVERNIGHT / early-morning uncertainty
# same_day sigma shrinks via time_adjusted_sigma() as day progresses
SIGMA = {
    "HIGHTEMP":  {"same_day": 3.5, "next_day": 4.5, "weekly": 6.5},
    "LOWTEMP":   {"same_day": 3.0, "next_day": 4.0, "weekly": 6.0},
    "RAIN":      {"same_day": 0.15, "next_day": 0.22, "weekly": 0.38},
    "SNOW":      {"same_day": 0.40, "next_day": 0.65, "weekly": 1.30},
    "WINDSPEED": {"same_day": 3.0,  "next_day": 4.5,  "weekly": 7.0},
}

# City timezone map — used for time-adjusted sigma
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


def sigma_for(variable, horizon):
    v = SIGMA.get(variable, {"same_day": 3.5, "next_day": 4.5, "weekly": 6.5})
    return v.get(horizon, v["next_day"])


def time_adjusted_sigma(base_sigma: float, horizon: str, city: str = "NYC") -> float:
    """Shrink same_day sigma as the afternoon progresses.

    Physics: by mid-afternoon, most of the day's temperature evolution has
    already happened. Remaining uncertainty is proportional to sqrt(time_remaining).

    High-temp window assumed: sunrise 6 AM -> peak 5 PM local (11 hours).
    Sigma floors at 1.0F (irreducible observation error).
    """
    if horizon != "same_day":
        return base_sigma
    try:
        tz = ZoneInfo(CITY_TZ.get(city, "America/New_York"))
        now_local = dt.datetime.now(tz)
        hour = now_local.hour + now_local.minute / 60.0
        SUNRISE_H = 6.0
        PEAK_H = 17.0
        total = PEAK_H - SUNRISE_H  # 11 hours
        elapsed = max(0.0, min(hour - SUNRISE_H, total))
        fraction_remaining = 1.0 - (elapsed / total)
        adjusted = base_sigma * (max(fraction_remaining, 0.0) ** 0.5)
        return max(1.0, adjusted)
    except Exception:
        return base_sigma


def adjusted_forecast(forecast_f, obs_f, variable, horizon):
    """Anchor the effective model forecast using the current METAR observation.

    For same_day HIGHTEMP: the daily high cannot be below what's already been
    observed. Use max(forecast, obs) as the model center.

    For same_day LOWTEMP: the daily low cannot be above what's already been
    observed. Use min(forecast, obs) as the model center.

    For next_day / weekly: obs has no anchoring power — return forecast unchanged.
    """
    if obs_f is None or horizon != "same_day":
        return forecast_f
    if variable == "HIGHTEMP":
        return max(forecast_f, obs_f)
    if variable == "LOWTEMP":
        return min(forecast_f, obs_f)
    return forecast_f


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
