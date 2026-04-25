"""
NOAA / NWS client — api.weather.gov.
Free, no API key required.
Two-step: /points/{lat},{lon} -> forecast URL -> periods.
Used for next-day and weekly Gaussian strategy.
"""
import httpx, datetime as dt
from typing import Optional

BASE       = "https://api.weather.gov"
USER_AGENT = "weather-bot-v2 (contact: ckun.general@outlook.com)"

class NoaaClient:
    def __init__(self):
        self.http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            timeout=20.0
        )
        self._points_cache   = {}
        self._forecast_cache = {}
        self._forecast_ts    = {}
        self.cache_ttl       = 1800  # 30 min — NWS updates hourly

    def _points(self, lat: float, lon: float) -> dict:
        key = f"{lat},{lon}"
        if key not in self._points_cache:
            r = self.http.get(f"{BASE}/points/{lat},{lon}")
            r.raise_for_status()
            self._points_cache[key] = r.json()["properties"]
        return self._points_cache[key]

    def forecast_periods(self, lat: float, lon: float) -> list:
        key = f"{lat},{lon}"
        now = dt.datetime.utcnow().timestamp()
        if key in self._forecast_cache and (now - self._forecast_ts.get(key, 0)) < self.cache_ttl:
            return self._forecast_cache[key]
        pts = self._points(lat, lon)
        r   = self.http.get(pts["forecast"])
        r.raise_for_status()
        periods = r.json()["properties"]["periods"]
        self._forecast_cache[key] = periods
        self._forecast_ts[key]    = now
        return periods

    def forecast_hourly(self, lat: float, lon: float) -> list:
        pts = self._points(lat, lon)
        r   = self.http.get(pts["forecastHourly"])
        r.raise_for_status()
        return r.json()["properties"]["periods"]

    def summary(self, lat: float, lon: float) -> dict:
        """
        Returns structured forecast:
          high_f, low_f, precip_prob, wind, short, fetched_at
        Looks at next 4 periods to find daytime high and overnight low.
        """
        try:
            periods = self.forecast_periods(lat, lon)
        except Exception as e:
            return {"error": str(e), "fetched_at": dt.datetime.utcnow().isoformat()}

        out = {"fetched_at": dt.datetime.utcnow().isoformat()}
        for p in periods[:6]:
            name = (p.get("name") or "").lower()
            if p.get("isDaytime") and "high_f" not in out:
                out["high_f"]      = p["temperature"]
                out["high_period"] = p["name"]
                out["precip_prob"] = (p.get("probabilityOfPrecipitation") or {}).get("value")
                out["wind"]        = p.get("windSpeed")
                out["short"]       = p.get("shortForecast")
            if not p.get("isDaytime") and "low_f" not in out:
                out["low_f"]       = p["temperature"]
                out["low_period"]  = p["name"]
        return out

    def high_f(self, lat: float, lon: float) -> Optional[float]:
        """Convenience: next daytime high in F."""
        return self.summary(lat, lon).get("high_f")

    def low_f(self, lat: float, lon: float) -> Optional[float]:
        """Convenience: next overnight low in F."""
        return self.summary(lat, lon).get("low_f")

    def is_available(self, lat: float, lon: float) -> bool:
        """Quick health check — returns True if NWS responds for this location."""
        try:
            self._points(lat, lon)
            return True
        except Exception:
            return False
