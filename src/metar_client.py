"""
METAR client using aviationweather.gov.
Parses sky cover and dewpoint spread to detect socked-in conditions.
"""
import httpx, datetime as dt, re

BASE = "https://aviationweather.gov/api/data/metar"


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


class MetarClient:
    def __init__(self):
        self.client = httpx.Client(timeout=15.0)

    def latest(self, station: str) -> dict:
        params = {"ids": station, "format": "json", "taf": "false", "hours": 2}
        r = self.client.get(BASE, params=params)
        r.raise_for_status()
        data = r.json()
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


if __name__ == "__main__":
    c = MetarClient()
    obs = c.latest("KAUS")
    print("KAUS:", obs)
    print("Socked in:", obs["socked_in"])
