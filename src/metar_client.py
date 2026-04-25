"""
METAR client — Aviation Weather Center API.
Free, no API key. Updates every 20-60 minutes per station.
Used for METAR lock detection (Strategy 1).
"""
import httpx, datetime as dt
from typing import Optional

BASE = "https://aviationweather.gov/api/data/metar"

class MetarClient:
    def __init__(self):
        self.http = httpx.Client(timeout=15.0)
        self._cache = {}
        self._cache_ts = {}
        self.cache_ttl = 300  # 5 min cache — METAR updates every 20-60 min

    def latest(self, station: str) -> dict:
        now = dt.datetime.utcnow().timestamp()
        if station in self._cache and (now - self._cache_ts.get(station, 0)) < self.cache_ttl:
            return self._cache[station]
        try:
            r = self.http.get(BASE, params={
                "ids": station, "format": "json", "taf": "false", "hours": 2
            })
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return {"station": station, "error": str(e), "fetched_at": dt.datetime.utcnow().isoformat()}

        if not data:
            return {"station": station, "error": "no data", "fetched_at": dt.datetime.utcnow().isoformat()}

        m        = data[0]
        temp_c   = m.get("temp")
        dew_c    = m.get("dewp")
        wspd_kt  = m.get("wspd")
        obs_time = m.get("reportTime") or m.get("obsTime", "")

        result = {
            "station":    station,
            "obs_time":   obs_time,
            "temp_f":     round(temp_c * 9/5 + 32, 1) if temp_c is not None else None,
            "dewpoint_f": round(dew_c * 9/5 + 32, 1)  if dew_c  is not None else None,
            "wind_mph":   round(wspd_kt * 1.15078, 1)  if wspd_kt is not None else None,
            "wind_dir":   m.get("wdir"),
            "visibility": m.get("visib"),
            "raw":        m.get("rawOb"),
            "fetched_at": dt.datetime.utcnow().isoformat(),
            "error":      None,
        }

        self._cache[station]    = result
        self._cache_ts[station] = now
        return result

    def temp_f(self, station: str) -> Optional[float]:
        """Convenience: just return current temp in F, or None."""
        return self.latest(station).get("temp_f")

    def is_stale(self, station: str, max_age_minutes: int = 90) -> bool:
        """Return True if observation is older than max_age_minutes."""
        obs = self.latest(station)
        if obs.get("error") or not obs.get("obs_time"):
            return True
        try:
            obs_dt = dt.datetime.fromisoformat(obs["obs_time"].replace("Z", "+00:00")).replace(tzinfo=None)
            age    = (dt.datetime.utcnow() - obs_dt).total_seconds() / 60
            return age > max_age_minutes
        except Exception:
            return True
