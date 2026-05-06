"""
Kalshi trading-day helper (Tier 3.3 from PROFITABLE_TRADER_REVIEW.md).

Why this exists
---------------
Kalshi resolves CLI weather contracts on the National Weather Service's
official daily climate report, which runs from local *midnight to midnight*
on the *Local Standard Time* (LST) clock — NOT the local clock during
Daylight Saving Time.

In practice this means during DST months (typically March → November in
the US):
    Kalshi "day" = 1 AM local clock to 12:59 AM next-day local clock
    (i.e. midnight LST = 1 AM EDT/CDT/MDT/PDT)

Outside DST (December–March in most years), local clock and LST coincide,
so the trading day matches the local civil date.

Day-trading HIGHTEMP we don't notice: the daily high happens mid-afternoon,
nowhere near the boundary. But for overnight LOWTEMP or any 11 PM – 1 AM
market quote, using `dt.datetime.now(tz).date()` blindly puts an event
into the WRONG Kalshi day half the year. This helper anchors to LST so
we always agree with Kalshi's settlement calendar.

Usage
-----
    from trading_day import kalshi_trading_day

    # Pass the *moment* and the city; get back the Kalshi-resolution date.
    today_kalshi = kalshi_trading_day("CHI", dt.datetime.now(tz=...))
    # -> 'YYYY-MM-DD'

Implementation
--------------
LST is the *base* (non-DST) UTC offset for the city's tzdata zone. We get
it by asking the IANA tzdata what offset would apply on a winter day
(Jan 15) — that's always standard time. Then we apply that fixed offset
to the input datetime to get the LST wall clock, and take its date.

Pure stdlib, no extra deps.
"""
import datetime as dt
from zoneinfo import ZoneInfo

# Same map as model.py / market_scanner.py — kept here as the single source
# of truth for trading_day so callers don't need to thread one through.
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


def _lst_offset_for_zone(tzname: str) -> dt.timedelta:
    """Return the city's Local Standard Time UTC offset.

    Picks January 15 (always non-DST) and asks the zone for its UTC offset
    on that date. We deliberately use noon to dodge any historic offset
    transitions that happen at 2 AM.
    """
    tz = ZoneInfo(tzname)
    # A winter day, fixed across all years' tzdata.
    winter_noon = dt.datetime(2024, 1, 15, 12, 0, tzinfo=tz)
    return winter_noon.utcoffset() or dt.timedelta(0)


def kalshi_trading_day(city: str, when: dt.datetime | None = None) -> str:
    """Return the YYYY-MM-DD Kalshi trading day for `city` at `when`.

    Args:
        city: City code in CITY_TZ (e.g. "CHI", "NYC").
        when: A timezone-aware datetime. If naive, treated as UTC.
              Defaults to current UTC time.

    Returns:
        ISO date string (YYYY-MM-DD) of the LST-anchored civil day that
        Kalshi will settle the contract on.

    Examples:
        # 12:30 AM CDT June 1st  -> still LST June 1st (LST = 11:30 PM May 31)
        # Wait, math: CDT = UTC-5, CST = UTC-6
        #   12:30 AM CDT = 5:30 UTC = 11:30 PM CST (May 31).
        # So Kalshi day = 2024-05-31. That's the entire point of this helper.
    """
    if when is None:
        when = dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)

    tzname = CITY_TZ.get(city)
    if not tzname:
        # Unknown city — fall back to local-tz date (best we can do).
        return when.date().isoformat()

    lst_offset = _lst_offset_for_zone(tzname)
    # Convert the moment to LST wall-clock by ADDING the LST offset to UTC.
    utc_dt = when.astimezone(dt.timezone.utc)
    lst_wall = utc_dt + lst_offset
    return lst_wall.date().isoformat()


def dst_aware_horizon_offset_hours(city: str, when: dt.datetime | None = None) -> float:
    """How many hours of DST is currently active in `city`?

    Returns 1.0 during DST, 0.0 during standard time. Useful for diagnostic
    logging; not used in the normal scan path because kalshi_trading_day()
    encapsulates everything you need.
    """
    if when is None:
        when = dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    tzname = CITY_TZ.get(city)
    if not tzname:
        return 0.0
    tz = ZoneInfo(tzname)
    local_offset = when.astimezone(tz).utcoffset() or dt.timedelta(0)
    lst_offset = _lst_offset_for_zone(tzname)
    delta = local_offset - lst_offset
    return delta.total_seconds() / 3600.0


if __name__ == "__main__":
    # Quick smoke check.
    import sys
    chi = ZoneInfo("America/Chicago")
    test_cases = [
        # (label, city, dt, expected)
        ("CHI 12:30 AM CDT June 1 (DST active)",
         "CHI", dt.datetime(2024, 6, 1, 0, 30, tzinfo=chi), "2024-05-31"),
        ("CHI 2:00 AM CDT June 1 (DST active, well past LST midnight)",
         "CHI", dt.datetime(2024, 6, 1, 2, 0, tzinfo=chi), "2024-06-01"),
        ("CHI 12:30 AM CST Jan 5 (no DST)",
         "CHI", dt.datetime(2024, 1, 5, 0, 30, tzinfo=chi), "2024-01-05"),
        ("NYC 12:30 AM EDT July 4 (DST)",
         "NYC", dt.datetime(2024, 7, 4, 0, 30, tzinfo=ZoneInfo("America/New_York")),
         "2024-07-03"),
    ]
    fail = 0
    for label, city, dtv, expected in test_cases:
        got = kalshi_trading_day(city, dtv)
        ok = "OK" if got == expected else "FAIL"
        if got != expected:
            fail += 1
        print(f"[{ok}] {label}: got={got} expected={expected}")
    print(f"\nDST offset CHI summer: {dst_aware_horizon_offset_hours('CHI', dt.datetime(2024, 7, 1, tzinfo=dt.timezone.utc))}")
    print(f"DST offset CHI winter: {dst_aware_horizon_offset_hours('CHI', dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc))}")
    sys.exit(0 if fail == 0 else 1)
