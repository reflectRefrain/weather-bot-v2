"""
NOAA / NWS client.
Uses api.weather.gov (free, no key). Two-step flow:
1. /points/{lat},{lon} -> returns forecast URL for the grid
2. /gridpoints/{office}/{gridX},{gridY}/forecast -> periods

summary(lat, lon, target_date=None):
- If target_date is provided (YYYY-MM-DD), returns the daytime forecast for that date.
- Otherwise falls back to the first daytime / nighttime periods.
"""
import httpx
import datetime as dt

USER_AGENT = "kalshi-weather-bot (contact: chen@example.com)"
BASE = "https://api.weather.gov"

class NoaaClient:
    def __init__(self):
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            timeout=20.0,
        )
        self._points_cache = {}

    def _points(self, lat, lon):
        key = f"{lat},{lon}"
        if key not in self._points_cache:
            r = self.client.get(f"{BASE}/points/{lat},{lon}")
            r.raise_for_status()
            self._points_cache[key] = r.json()["properties"]
        return self._points_cache[key]

    def forecast(self, lat, lon):
        pts = self._points(lat, lon)
        r = self.client.get(pts["forecast"])
        r.raise_for_status()
        return r.json()["properties"]["periods"]

    def forecast_hourly(self, lat, lon):
        pts = self._points(lat, lon)
        r = self.client.get(pts["forecastHourly"])
        r.raise_for_status()
        return r.json()["properties"]["periods"]

    def _period_date(self, p):
        s = p.get("startTime")
        if not s:
            return None
        try:
            return dt.datetime.fromisoformat(s).date().isoformat()
        except Exception:
            return None

    def summary(self, lat, lon, target_date=None) -> dict:
        periods = self.forecast(lat, lon)
        out = {
            "target_date": target_date,
            "periods_raw": periods[:8],
        }

        if target_date:
            for p in periods:
                pdate = self._period_date(p)
                if p.get("isDaytime") and pdate == target_date:
                    out["high_f"] = p.get("temperature")
                    out["high_period"] = p.get("name")
                    out["precip_prob"] = (p.get("probabilityOfPrecipitation") or {}).get("value")
                    out["wind"] = p.get("windSpeed")
                    out["short"] = p.get("shortForecast")
                    break

            for p in periods:
                pdate = self._period_date(p)
                if (not p.get("isDaytime")) and pdate == target_date:
                    out["low_f"] = p.get("temperature")
                    out["low_period"] = p.get("name")
                    break

        if "high_f" not in out or out.get("high_f") is None:
            for p in periods[:4]:
                if p.get("isDaytime") and "high_f" not in out:
                    out["high_f"] = p.get("temperature")
                    out["high_period"] = p.get("name")
                    out["precip_prob"] = (p.get("probabilityOfPrecipitation") or {}).get("value")
                    out["wind"] = p.get("windSpeed")
                    out["short"] = p.get("shortForecast")
                if (not p.get("isDaytime")) and "low_f" not in out:
                    out["low_f"] = p.get("temperature")
                    out["low_period"] = p.get("name")

        out["fetched_at"] = dt.datetime.utcnow().isoformat()
        return out

    def high_f(self, lat, lon, target_date=None):
        """Convenience: next daytime high in F."""
        return self.summary(lat, lon, target_date=target_date).get("high_f")

    def low_f(self, lat, lon, target_date=None):
        """Convenience: next overnight low in F."""
        return self.summary(lat, lon, target_date=target_date).get("low_f")

    def is_available(self, lat, lon):
        """Quick health check."""
        try:
            self._points(lat, lon)
            return True
        except Exception:
            return False

if __name__ == "__main__":
    c = NoaaClient()
    s = c.summary(40.7789, -73.9692)
    print("NYC:", s)
