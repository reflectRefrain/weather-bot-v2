"""METAR Tracker — maintains per-city running daily max/min temperatures.

Called from main loop every cycle.
Writes to the daily_obs SQLite table.
Reads provide the running_max_f / running_min_f used by model_v3.
"""
import datetime as dt
from zoneinfo import ZoneInfo
from db import conn

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

CITY_METAR = {
    "NYC":  "KNYC",
    "LAX":  "KLAX",
    "CHI":  "KORD",
    "MIA":  "KMIA",
    "DEN":  "KDEN",
    "AUS":  "KAUS",
    "PHIL": "KPHL",
    "BOS":  "KBOS",
    "HOU":  "KHOU",
}


def ensure_table():
    """Create daily_obs table if it doesn't exist."""
    with conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_obs (
                city         TEXT,
                local_date   TEXT,
                running_max_f REAL,
                running_min_f REAL,
                current_f    REAL,
                obs_count    INTEGER DEFAULT 1,
                updated_at   TEXT,
                PRIMARY KEY (city, local_date)
            )
        """)


def local_date_for(city: str) -> str:
    """Return today's local date string (YYYY-MM-DD) for a given city."""
    tz = ZoneInfo(CITY_TZ.get(city, "America/New_York"))
    return dt.datetime.now(tz).date().isoformat()


def update_obs(city: str, temp_f: float):
    """Update running max/min for city with a new METAR temperature reading."""
    ensure_table()
    local_date = local_date_for(city)
    now_iso    = dt.datetime.utcnow().isoformat()
    with conn() as c:
        row = c.execute(
            "SELECT running_max_f, running_min_f, obs_count FROM daily_obs "
            "WHERE city=? AND local_date=?",
            (city, local_date),
        ).fetchone()
        if row:
            new_max   = max(row["running_max_f"], temp_f)
            new_min   = min(row["running_min_f"], temp_f)
            new_count = row["obs_count"] + 1
            c.execute(
                "UPDATE daily_obs SET running_max_f=?, running_min_f=?, current_f=?, "
                "obs_count=?, updated_at=? WHERE city=? AND local_date=?",
                (new_max, new_min, temp_f, new_count, now_iso, city, local_date),
            )
        else:
            c.execute(
                "INSERT INTO daily_obs VALUES (?,?,?,?,?,?,?)",
                (city, local_date, temp_f, temp_f, temp_f, 1, now_iso),
            )


def get_obs(city: str) -> dict | None:
    """Return the latest daily obs dict for a city, or None if not yet seen today."""
    ensure_table()
    local_date = local_date_for(city)
    with conn() as c:
        row = c.execute(
            "SELECT running_max_f, running_min_f, current_f, obs_count, updated_at "
            "FROM daily_obs WHERE city=? AND local_date=?",
            (city, local_date),
        ).fetchone()
    return dict(row) if row else None


def refresh_all_cities(metar_client) -> dict:
    """Pull fresh METAR for every configured city and update daily_obs.

    Returns dict of city -> temp_f for logging.
    Called once per main loop cycle.
    """
    results = {}
    for city, station in CITY_METAR.items():
        try:
            obs = metar_client.latest(station)
            temp_f = obs.get("temp_f")
            if temp_f is not None:
                update_obs(city, float(temp_f))
                results[city] = float(temp_f)
        except Exception as e:
            results[city] = None
    return results
