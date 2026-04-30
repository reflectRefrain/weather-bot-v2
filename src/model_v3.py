"""V3 probability model — OBSERVATION-BASED lock-in logic.

Core idea: Don't predict. Observe.
If METAR shows today's running max is already 85F, any strike at <=84F
cannot resolve NO. Buy YES at 95c, collect 100c at settlement.

We only trade when p_yes >= 0.98 (near-certainty).
We skip everything else — uncertain = no trade.
"""
import datetime as dt
from zoneinfo import ZoneInfo

# Physical upper bounds on temperature change
# Extremely conservative — real fronts rarely exceed these
MAX_TEMP_RISE_PER_HOUR = 4.0   # F/hr — extreme upper bound for remaining rise
MAX_TEMP_DROP_PER_HOUR = 3.0   # F/hr — after peak, cooling rate

# "Lock" hours — after these local hours the daily high/low is physically decided
HIGH_LOCK_HOUR_LOCAL = 17  # 5 PM local — daily high essentially locked
LOW_LOCK_HOUR_LOCAL  =  9  # 9 AM local — daily low essentially locked (near sunrise)

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


def hours_until_lock(now_local: dt.datetime, variable: str) -> float:
    """Hours until the daily high/low is no longer physically changeable.

    Returns 0.0 if the lock time has already passed.
    """
    lock_hour = HIGH_LOCK_HOUR_LOCAL if variable == "HIGHTEMP" else LOW_LOCK_HOUR_LOCAL
    lock_dt = now_local.replace(hour=lock_hour, minute=0, second=0, microsecond=0)
    if now_local >= lock_dt:
        return 0.0
    return (lock_dt - now_local).total_seconds() / 3600.0


def lockin_probability(
    running_max_f: float,
    running_min_f: float,
    current_temp_f: float,
    strike_low,
    strike_high,
    strike_type: str,
    variable: str,
    hours_remaining: float,
) -> float:
    """Return 1.0 (certain YES), 0.0 (certain NO), or 0.5 (uncertain/skip).

    Uses physical bounds to determine if outcome is already decided:
      - HIGHTEMP: daily high >= running_max_f and <= running_max_f + rise_headroom
      - LOWTEMP:  daily low  <= running_min_f and >= running_min_f - drop_headroom

    We never return intermediate values — if uncertain, return 0.5 and skip.
    """
    st = (strike_type or "").lower()

    if variable == "HIGHTEMP":
        # High CANNOT be below running_max_f (already observed)
        # High CAN still rise by at most MAX_TEMP_RISE_PER_HOUR * hours_remaining
        floor   = running_max_f
        ceiling = running_max_f + (MAX_TEMP_RISE_PER_HOUR * hours_remaining)
    elif variable == "LOWTEMP":
        # Low CANNOT be above running_min_f (already observed)
        # Low CAN still fall by at most MAX_TEMP_DROP_PER_HOUR * hours_remaining
        floor   = running_min_f - (MAX_TEMP_DROP_PER_HOUR * hours_remaining)
        ceiling = running_min_f
    else:
        return 0.5  # RAIN, SNOW, WINDSPEED not yet supported in v3

    # Evaluate each strike type against the physical range [floor, ceiling]
    if st == "greater":
        # YES = daily extreme > strike_low
        if floor > strike_low:   return 1.0   # already exceeded — locked YES
        if ceiling <= strike_low: return 0.0  # cannot reach — locked NO
        return 0.5                             # boundary straddles — uncertain

    if st == "less":
        # YES = daily extreme < strike_high
        if ceiling < strike_high: return 1.0  # can't exceed — locked YES
        if floor >= strike_high:  return 0.0  # already above — locked NO
        return 0.5

    if st == "between":
        # YES = daily extreme in [strike_low, strike_high]
        if strike_low is None or strike_high is None:
            return 0.5
        if floor > strike_high:  return 0.0   # already above range — locked NO
        if ceiling < strike_low: return 0.0   # can't reach range — locked NO
        if floor >= strike_low and ceiling <= strike_high:
            return 1.0                         # physically trapped in range — locked YES
        return 0.5                             # straddles boundary — uncertain

    return 0.5  # unknown strike type


def market_mid_prob(yes_bid_cents, yes_ask_cents):
    if yes_bid_cents is None or yes_ask_cents is None:
        return None
    return ((yes_bid_cents + yes_ask_cents) / 2.0) / 100.0
