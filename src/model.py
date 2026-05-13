"""Probability model — time-adjusted sigma + METAR obs anchor.

Key improvements over v1:
  1. sigma shrinks during same_day as the day progresses (less uncertainty at 3PM vs 7AM)
  2. METAR obs anchors the effective forecast floor/ceil
     (if it's already 82F, the daily high CANNOT be below 82F)
  3. Obs-trajectory projection trust ramps up through the morning.
     Before 7 AM local it is zero — NWS forecast used directly.
     By 10 AM it reaches full weight (0.70). This prevents the early-morning
     quadratic fit (only 2-3 obs) from dragging the forecast down to near
     current temperature when the day hasn't warmed yet.
"""
import datetime as dt
from statistics import NormalDist
from zoneinfo import ZoneInfo

SIGMA = {
    "HIGHTEMP":  {"same_day": 3.5, "next_day": 4.5, "weekly": 6.5},
    "LOWTEMP":   {"same_day": 3.0, "next_day": 4.0, "weekly": 6.0},
    "RAIN":      {"same_day": 0.15, "next_day": 0.22, "weekly": 0.38},
    "SNOW":      {"same_day": 0.40, "next_day": 0.65, "weekly": 1.30},
    "WINDSPEED": {"same_day": 3.0,  "next_day": 4.5,  "weekly": 7.0},
}

# Full 18-city CITY_TZ — covers every Kalshi HIGHTEMP market.
# Audit fix 2026-05-12: added the 9 cities that defaulted to America/New_York.
CITY_TZ = {
    # Eastern
    "NYC":  "America/New_York",
    "BOS":  "America/New_York",
    "PHIL": "America/New_York",
    "MIA":  "America/New_York",
    "DC":   "America/New_York",
    "ATL":  "America/New_York",
    # Central
    "CHI":  "America/Chicago",
    "HOU":  "America/Chicago",
    "AUS":  "America/Chicago",
    "DAL":  "America/Chicago",
    "SAT":  "America/Chicago",
    "MIN":  "America/Chicago",
    "OKC":  "America/Chicago",
    # Mountain
    "DEN":  "America/Denver",
    "PHX":  "America/Phoenix",   # MST year-round, no DST
    # Pacific
    "LAX":  "America/Los_Angeles",
    "LAS":  "America/Los_Angeles",
    "SF":   "America/Los_Angeles",
}

# Projection ramp: before PROJ_START_H the obs-trajectory projection
# gets zero weight. Between PROJ_START_H and PROJ_FULL_H it ramps
# linearly to PROJ_MAX_WEIGHT. After PROJ_FULL_H it holds at max.
# Rationale: at 4-6 AM only 2-3 hourly obs exist — quadratic fit has
# no predictive power and was causing -11 to -19F forecast errors.
PROJ_START_H   = 7.0    # before this hour (local), projection weight = 0
PROJ_FULL_H    = 10.0   # at this hour and after, full weight is applied
PROJ_MAX_WEIGHT = 0.70  # maximum blend weight once fully ramped

NBM_BLEND_WEIGHT = 0.50

DISAGREEMENT_THRESHOLD_F = 3.0
DISAGREEMENT_SIGMA_GAIN_F = 0.40
DISAGREEMENT_SIGMA_CAP_F = 2.5


def sigma_for(variable, horizon):
    v = SIGMA.get(variable, {"same_day": 3.5, "next_day": 4.5, "weekly": 6.5})
    return v.get(horizon, v["next_day"])


def _local_hour(city: str) -> float:
    """Return fractional local hour for the given city right now."""
    try:
        tz = ZoneInfo(CITY_TZ.get(city, "America/New_York"))
        now = dt.datetime.now(tz)
        return now.hour + now.minute / 60.0
    except Exception:
        return 12.0  # safe fallback — midday, full trust


def projection_trust_weight(city: str) -> float:
    """Return the blend weight to give the obs-trajectory projection.

    Ramps linearly from 0.0 at PROJ_START_H to PROJ_MAX_WEIGHT at
    PROJ_FULL_H, then holds. Before PROJ_START_H returns 0.0 so that
    early-morning scans (4-7 AM) use NWS directly with only an obs floor.

    Examples (PROJ_START_H=7, PROJ_FULL_H=10, PROJ_MAX_WEIGHT=0.70):
      4:00 AM -> 0.00  (no projection trust)
      6:59 AM -> 0.00  (no projection trust)
      7:00 AM -> 0.00  (threshold, just turning on)
      8:30 AM -> 0.35  (halfway)
      10:00 AM-> 0.70  (full weight)
      2:00 PM -> 0.70  (full weight)
    """
    hour = _local_hour(city)
    if hour < PROJ_START_H:
        return 0.0
    if hour >= PROJ_FULL_H:
        return PROJ_MAX_WEIGHT
    frac = (hour - PROJ_START_H) / (PROJ_FULL_H - PROJ_START_H)
    return round(frac * PROJ_MAX_WEIGHT, 4)


def time_adjusted_sigma(base_sigma: float, horizon: str, city: str = "NYC") -> float:
    """Shrink same_day sigma as the afternoon progresses.
    Sigma floors at 1.0F (irreducible observation error).
    """
    if horizon != "same_day":
        return base_sigma
    try:
        hour = _local_hour(city)
        SUNRISE_H = 6.0
        PEAK_H = 17.0
        total = PEAK_H - SUNRISE_H
        elapsed = max(0.0, min(hour - SUNRISE_H, total))
        fraction_remaining = 1.0 - (elapsed / total)
        adjusted = base_sigma * (max(fraction_remaining, 0.0) ** 0.5)
        return max(1.0, adjusted)
    except Exception:
        return base_sigma


def ensemble_forecast(nws_f, nbm_f):
    """Combine NWS and NBM into a single deterministic forecast."""
    if nws_f is None and nbm_f is None:
        return None, None
    if nws_f is None:
        return nbm_f, None
    if nbm_f is None:
        return nws_f, None
    blended = NBM_BLEND_WEIGHT * nbm_f + (1.0 - NBM_BLEND_WEIGHT) * nws_f
    return blended, abs(nws_f - nbm_f)


def disagreement_sigma_bonus(disagreement_f):
    """Return extra sigma (F) to add when NWS and NBM forecasts diverge."""
    if disagreement_f is None:
        return 0.0
    excess = max(0.0, disagreement_f - DISAGREEMENT_THRESHOLD_F)
    return min(DISAGREEMENT_SIGMA_CAP_F, excess * DISAGREEMENT_SIGMA_GAIN_F)


def adjusted_forecast(forecast_f, obs_f, variable, horizon,
                      projection=None, nbm_high_f=None, city: str = "NYC"):
    """Compute the effective model forecast for a city/variable/horizon.

    For same_day HIGHTEMP the logic is:
      1. Establish a hard floor: day's high cannot be below the observed max so far.
      2. Compute the projection blend weight for the current local hour.
         Before 7 AM local the weight is 0 — projection is ignored.
      3. If weight > 0 and a projection exists, blend:
           effective = weight * projected_high + (1 - weight) * NWS_forecast
         and floor at max(obs_floor, NWS_forecast).
         The NWS floor ensures we never output below the NWS value during
         early ramp — this was the root cause of the -11.93F mean error.
      4. If weight == 0 (pre-7 AM) or no projection: use NWS directly,
         floored at obs_f (physical floor only, not a blend anchor).
      5. NBM ensemble applied only when projection is absent or weight == 0.

    The key invariant: effective_forecast >= forecast_f always for HIGHTEMP.
    The NWS daytime high is always a lower bound on the effective forecast.
    """
    if horizon != "same_day":
        return forecast_f

    if variable == "HIGHTEMP":
        # Physical floor: day's high can't be below what's been observed.
        obs_max = obs_f
        if projection and projection.get("observed_max_f") is not None:
            om = projection["observed_max_f"]
            obs_max = max(obs_max or om, om)

        # NWS is always a floor — the effective forecast must be >= NWS.
        # This prevents the projection from dragging us below the model forecast.
        nws_floor = forecast_f

        # Determine time-dependent projection weight.
        w = projection_trust_weight(city) if projection else 0.0

        if w > 0.0 and projection and projection.get("projected_high_f") is not None and forecast_f is not None:
            proj = projection["projected_high_f"]
            blended = w * proj + (1.0 - w) * forecast_f
            # Double floor: never below NWS, never below observed max.
            if nws_floor is not None:
                blended = max(blended, nws_floor)
            if obs_max is not None:
                blended = max(blended, obs_max)
            return blended

        # No projection (or pre-7 AM suppression): try NBM ensemble.
        if nbm_high_f is not None and forecast_f is not None:
            ens, _ = ensemble_forecast(forecast_f, nbm_high_f)
            if ens is not None:
                if nws_floor is not None:
                    ens = max(ens, nws_floor)
                if obs_max is not None:
                    ens = max(ens, obs_max)
            return ens

        # Fallback: NWS with obs floor.
        if obs_f is not None and forecast_f is not None:
            return max(forecast_f, obs_f)
        return forecast_f if forecast_f is not None else obs_f

    if variable == "LOWTEMP":
        eff = forecast_f
        if nbm_high_f is not None and forecast_f is not None:
            ens, _ = ensemble_forecast(forecast_f, nbm_high_f)
            if ens is not None:
                eff = ens
        if obs_f is not None and eff is not None:
            return min(eff, obs_f)
        return eff

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
