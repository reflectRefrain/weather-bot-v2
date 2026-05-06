"""
METAR client using aviationweather.gov.
Parses sky cover and dewpoint spread to detect socked-in conditions.
Projects same-day high via quadratic fit on hourly obs trajectory.
"""
import httpx, datetime as dt, re
from zoneinfo import ZoneInfo

BASE = "https://aviationweather.gov/api/data/metar"

# Same-day peak local hour assumed for high-temp projection.
# Most stations peak between 3-5 PM local; 5 PM is conservative (slightly late).
PEAK_HOUR_LOCAL = 17.0

# Safety: never project the high more than this many degrees F above the latest
# observed temperature. Caps quadratic-extrapolation blow-ups.
MAX_PROJECTION_DELTA_F = 5.0

# City -> tz, mirrored from model.py to avoid a circular import.
_CITY_TZ_PROJECTION = {
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


def _parse_sky(raw: str):
    """Return highest-rank sky coverage and height (ft AGL) from raw METAR."""
    covers = re.findall(r'(FEW|SCT|BKN|OVC|VV)(\d{3})', raw or "")
    if not covers:
        return None, None
    rank = {"FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 4}
    covers.sort(key=lambda x: rank.get(x[0], 0), reverse=True)
    top = covers[0]
    return top[0], int(top[1]) * 100


def is_socked_in(obs: dict) -> bool:
    """
    Return True if obs suggests afternoon high will be suppressed:
      - BKN or OVC at or below 3000 ft AGL, OR
      - temp/dewpoint spread <= 3F (fog / near-saturation)
    """
    cover = obs.get("sky_cover")
    height = obs.get("sky_height_ft")
    if cover in ("BKN", "OVC", "VV") and height is not None and height <= 3000:
        return True
    temp = obs.get("temp_f")
    dew = obs.get("dewpoint_f")
    if temp is not None and dew is not None and (temp - dew) <= 3:
        return True
    return False


def _parse_report_time(s: str):
    """Parse 'YYYY-MM-DDTHH:MM:SS.000Z' into a UTC-aware datetime. None on failure."""
    if not s:
        return None
    try:
        # aviationweather returns trailing 'Z'
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _quadratic_fit(xs, ys):
    """Fit y = a*x^2 + b*x + c via normal equations on 3+ points.
    Returns (a, b, c) or None if singular / underdetermined.
    Pure-stdlib so we don't add numpy as a dependency.
    """
    n = len(xs)
    if n < 3:
        return None
    sx  = sum(xs); sx2 = sum(x*x for x in xs); sx3 = sum(x**3 for x in xs); sx4 = sum(x**4 for x in xs)
    sy  = sum(ys); sxy = sum(x*y for x, y in zip(xs, ys)); sx2y = sum(x*x*y for x, y in zip(xs, ys))
    # 3x3 system: [[sx4 sx3 sx2],[sx3 sx2 sx],[sx2 sx n]] @ [a,b,c] = [sx2y, sxy, sy]
    A = [[sx4, sx3, sx2], [sx3, sx2, sx], [sx2, sx, n]]
    B = [sx2y, sxy, sy]
    try:
        return _solve3(A, B)
    except Exception:
        return None


def _solve3(A, B):
    """Cramer's rule for a 3x3 system. Returns (x0, x1, x2) or raises if singular."""
    def det3(m):
        return (
            m[0][0]*(m[1][1]*m[2][2] - m[1][2]*m[2][1])
          - m[0][1]*(m[1][0]*m[2][2] - m[1][2]*m[2][0])
          + m[0][2]*(m[1][0]*m[2][1] - m[1][1]*m[2][0])
        )
    D = det3(A)
    if abs(D) < 1e-12:
        raise ValueError("singular matrix")
    out = []
    for i in range(3):
        Ai = [row[:] for row in A]
        for r in range(3):
            Ai[r][i] = B[r]
        out.append(det3(Ai) / D)
    return tuple(out)


class MetarClient:
    def __init__(self):
        self.client = httpx.Client(timeout=15.0)

    def _fetch(self, station: str, hours: int):
        params = {"ids": station, "format": "json", "taf": "false", "hours": hours}
        r = self.client.get(BASE, params=params)
        r.raise_for_status()
        return r.json() or []

    def latest(self, station: str) -> dict:
        data = self._fetch(station, hours=2)
        if not data:
            return {"station": station, "error": "no data"}
        m = data[0]
        temp_c = m.get("temp")
        dew_c = m.get("dewp")
        wspd_kt = m.get("wspd")
        raw = m.get("rawOb", "")
        sky_cover, sky_height_ft = _parse_sky(raw)
        obs = {
            "station":       station,
            "obs_time":      m.get("reportTime"),
            "temp_f":        (temp_c * 9 / 5 + 32) if temp_c is not None else None,
            "dewpoint_f":    (dew_c * 9 / 5 + 32) if dew_c is not None else None,
            "wind_mph":      (wspd_kt * 1.15078) if wspd_kt is not None else None,
            "wind_dir":      m.get("wdir"),
            "visibility":    m.get("visib"),
            "sky_cover":     sky_cover,
            "sky_height_ft": sky_height_ft,
            "raw":           raw,
            "fetched_at":    dt.datetime.utcnow().isoformat(),
            "error":         None,
        }
        obs["socked_in"] = is_socked_in(obs)
        return obs

    def history(self, station: str, hours: int = 6):
        """Return list of (utc_datetime, temp_f) tuples for last `hours`,
        oldest first. Skips records with missing temp."""
        data = self._fetch(station, hours=hours)
        out = []
        for m in data:
            t_utc = _parse_report_time(m.get("reportTime"))
            tc = m.get("temp")
            if t_utc is None or tc is None:
                continue
            out.append((t_utc, tc * 9 / 5 + 32))
        out.sort(key=lambda r: r[0])
        return out

    def project_high(self, station: str, city: str, peak_hour_local: float = PEAK_HOUR_LOCAL):
        """Project the daily high in F using a quadratic fit on hourly obs.

        Returns a dict:
            {
              'projected_high_f': float | None,
              'observed_max_f':   float | None,  # max obs seen so far today
              'latest_temp_f':    float | None,
              'method':           'quadratic' | 'observed_max' | 'latest' | None,
              'n_points':         int,
              'reason':           short string describing why
            }

        Decision rules:
          - <3 obs in window: return None (caller falls back to NWS forecast)
          - If we're already past peak_hour_local: return observed_max (high is in)
          - If quadratic fit's a >= 0 (still concave up): can't trust extrapolation,
            return latest observed temp
          - Otherwise project to peak_hour_local, but cap at
            latest_temp + MAX_PROJECTION_DELTA_F
          - Result is also floored to observed_max (high cannot be below max seen)
        """
        result = {
            "projected_high_f": None, "observed_max_f": None, "latest_temp_f": None,
            "method": None, "n_points": 0, "reason": "",
        }
        try:
            tz_name = _CITY_TZ_PROJECTION.get(city, "America/New_York")
            tz = ZoneInfo(tz_name)
            history = self.history(station, hours=6)
            result["n_points"] = len(history)
            if not history:
                result["reason"] = "no_obs"
                return result

            now_local = dt.datetime.now(tz)
            today_local_date = now_local.date()

            # Only use observations from today's local date — yesterday's obs
            # would corrupt the trajectory.
            today_obs = [(t, f) for t, f in history if t.astimezone(tz).date() == today_local_date]
            if not today_obs:
                result["reason"] = "no_obs_today"
                return result

            latest_t, latest_f = today_obs[-1]
            obs_max_f = max(f for _, f in today_obs)
            result["latest_temp_f"] = round(latest_f, 2)
            result["observed_max_f"] = round(obs_max_f, 2)

            current_local_hour = now_local.hour + now_local.minute / 60.0

            # Past peak — high is essentially in. Trust observed max.
            if current_local_hour >= peak_hour_local:
                result["projected_high_f"] = round(obs_max_f, 2)
                result["method"] = "observed_max"
                result["reason"] = f"past_peak({peak_hour_local}h)"
                return result

            # Need 3+ points for a quadratic fit
            if len(today_obs) < 3:
                result["projected_high_f"] = round(max(latest_f, obs_max_f), 2)
                result["method"] = "latest"
                result["reason"] = f"insufficient_obs({len(today_obs)})"
                return result

            # Build (hours-since-first-obs, temp_f) and fit
            base = today_obs[0][0]
            xs = [(t - base).total_seconds() / 3600.0 for t, _ in today_obs]
            ys = [f for _, f in today_obs]
            fit = _quadratic_fit(xs, ys)
            if fit is None:
                result["projected_high_f"] = round(max(latest_f, obs_max_f), 2)
                result["method"] = "latest"
                result["reason"] = "fit_failed"
                return result

            a, b, c = fit
            # If a >= 0 the parabola is concave-up — extrapolation is unreliable.
            # Fall back to latest observed temp (or obs_max if higher).
            if a >= 0:
                result["projected_high_f"] = round(max(latest_f, obs_max_f), 2)
                result["method"] = "latest"
                result["reason"] = f"concave_up(a={a:.3f})"
                return result

            # Project to peak local hour (translate via base->local)
            base_local = base.astimezone(tz)
            base_local_hour = base_local.hour + base_local.minute / 60.0
            # Hours from base to peak_hour_local (same calendar day in tz)
            x_peak = peak_hour_local - base_local_hour
            projected = a * x_peak**2 + b * x_peak + c

            # Cap upside vs latest temp
            cap = latest_f + MAX_PROJECTION_DELTA_F
            if projected > cap:
                projected = cap
                result["reason"] = f"capped(+{MAX_PROJECTION_DELTA_F}F)"
            # Floor at observed max — high can't be below max seen
            if projected < obs_max_f:
                projected = obs_max_f
                result["reason"] = (result["reason"] + "|floored_to_obs_max").strip("|")

            result["projected_high_f"] = round(projected, 2)
            result["method"] = "quadratic"
            if not result["reason"]:
                result["reason"] = f"a={a:.3f},b={b:.2f}"
            return result
        except Exception as e:
            result["reason"] = f"exc:{str(e)[:60]}"
            return result


if __name__ == "__main__":
    c = MetarClient()
    obs = c.latest("KAUS")
    print("KAUS latest:", obs)
    print("Socked in:", obs["socked_in"])
    proj = c.project_high("KAUS", "AUS")
    print("KAUS projection:", proj)
