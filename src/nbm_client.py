"""
NBM / Pirate Weather client.

Pirate Weather (https://pirateweather.net/) is a free Dark Sky-compatible API
whose primary data source is NOAA's National Blend of Models (NBM) plus HRRR
near-term. We use it as a SECOND independent forecast source alongside NWS
api.weather.gov, so we can:

  1. Detect model disagreement (NWS vs Pirate/NBM diverge -> noisier truth ->
     back off / widen sigma in higher tiers).
  2. Blend the two as a primitive ensemble for the model's effective forecast.

Pirate Weather does NOT expose true NBM percentiles (10/25/75/90); it only
serves deterministic temperatureHigh / temperatureLow point forecasts. To get
true probabilistic quantiles we'd need to read NBM GRIB directly — deferred.

Auth: requires PIRATE_WEATHER_API_KEY env var. If unset, all calls return
None and the caller falls back to NWS-only behavior. NEVER blocks the loop.

Free tier: 10k calls/day. We call once per scan cycle per city, plenty.

Endpoint:
    https://api.pirateweather.net/forecast/{key}/{lat},{lon}?units=us&exclude=minutely,hourly,alerts

Daily block fields used:
    daily.data[i].time             (unix seconds, UTC midnight of forecast day)
    daily.data[i].temperatureHigh  (F, with units=us)
    daily.data[i].temperatureLow   (F, with units=us)
"""
import os
import datetime as dt
import httpx

BASE = "https://api.pirateweather.net/forecast"
TIMEOUT_S = 12.0
USER_AGENT = "kalshi-weather-bot-nbm/1.0"


class NbmClient:
    """Thin Pirate Weather (NBM-backed) client.

    Methods are best-effort: any failure (no key, network, parse) returns
    None / empty rather than raising, so the scanner never breaks because
    a secondary forecast source is down.
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("PIRATE_WEATHER_API_KEY", "").strip()
        self._cache: dict[str, tuple[float, dict]] = {}  # key -> (ts, payload)
        self._cache_ttl_s = 600  # 10 minutes — daily block changes slowly
        try:
            self.client = httpx.Client(
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT_S,
            )
        except Exception:
            self.client = None

    def enabled(self) -> bool:
        return bool(self.api_key) and self.client is not None

    def _fetch(self, lat: float, lon: float) -> dict | None:
        if not self.enabled():
            return None
        key = f"{lat:.4f},{lon:.4f}"
        now = dt.datetime.utcnow().timestamp()
        cached = self._cache.get(key)
        if cached and (now - cached[0]) < self._cache_ttl_s:
            return cached[1]
        url = f"{BASE}/{self.api_key}/{lat},{lon}"
        params = {"units": "us", "exclude": "minutely,hourly,alerts"}
        try:
            r = self.client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        self._cache[key] = (now, data)
        return data

    def forecast_high(self, lat: float, lon: float, target_date_iso: str | None = None) -> dict | None:
        """Return {high_f, low_f, source_day} for target_date_iso (YYYY-MM-DD).

        If target_date is None, returns today's daily block.
        Returns None if disabled, network failed, or no matching day.

        Pirate's `daily.data[i].time` is unix seconds at LOCAL midnight of
        the forecast day. We compare ISO-date strings to find the match.
        """
        data = self._fetch(lat, lon)
        if not data:
            return None
        try:
            days = (data.get("daily") or {}).get("data") or []
        except Exception:
            return None
        if not days:
            return None

        # Resolve the day we want.
        target = target_date_iso
        if target is None:
            # Use timezone offset from response if present, else UTC.
            tz_offset = data.get("offset")  # hours; may be None
            now_utc = dt.datetime.utcnow()
            if tz_offset is not None:
                local = now_utc + dt.timedelta(hours=float(tz_offset))
                target = local.date().isoformat()
            else:
                target = now_utc.date().isoformat()

        # Match by date string of the day's start time.
        match = None
        for d in days:
            t = d.get("time")
            if t is None:
                continue
            try:
                # time is unix seconds. Convert with timezone offset if available
                # so the date matches local-day semantics.
                tz_offset = data.get("offset", 0) or 0
                day_dt = dt.datetime.utcfromtimestamp(int(t)) + dt.timedelta(hours=float(tz_offset))
                if day_dt.date().isoformat() == target:
                    match = d
                    break
            except Exception:
                continue

        if match is None:
            # Fallback: first day in the daily block (often "today").
            match = days[0]

        try:
            high = match.get("temperatureHigh")
            low = match.get("temperatureLow")
            if high is None and low is None:
                return None
            return {
                "high_f": float(high) if high is not None else None,
                "low_f": float(low) if low is not None else None,
                "source_day": dt.datetime.utcfromtimestamp(int(match.get("time", 0))).date().isoformat()
                if match.get("time") else None,
                "source": "pirateweather/nbm",
            }
        except Exception:
            return None


# Module-level helper for consumers that want a one-shot call.
def forecast_high(lat: float, lon: float, target_date_iso: str | None = None,
                  api_key: str | None = None) -> dict | None:
    """One-shot convenience wrapper. Builds a client per call; for scanner use,
    instantiate NbmClient once and reuse."""
    return NbmClient(api_key=api_key).forecast_high(lat, lon, target_date_iso)
