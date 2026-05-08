"""
GHCN station IDs for our 18 cities — verified via NCEI station lookup.
Each entry is the airport METAR station Kalshi resolves on, and its
matching GHCN-Daily ID for historical TMAX/TMIN.

Verified against:
  https://www.ncei.noaa.gov/cdo-web/datasets/GHCND/stations/GHCND:<id>/detail

Notes:
  - NYC = Central Park (USW00094728), per Kalshi's KXHIGHNY resolution.
  - CHI = Midway (USW00014819), NOT O'Hare — Kalshi's choice.
  - DC  = Reagan National (USW00013743), NOT Dulles.
  - HOU = Houston Hobby (USW00012918), NOT IAH.
"""

CITIES = [
    # (city_code, metar, ghcn_station, name)
    ("NYC",  "KNYC", "USW00094728", "New York Central Park"),
    ("CHI",  "KMDW", "USW00014819", "Chicago Midway"),
    ("MIA",  "KMIA", "USW00012839", "Miami Intl"),
    ("LAX",  "KLAX", "USW00023174", "Los Angeles Intl"),
    ("DEN",  "KDEN", "USW00003017", "Denver Intl"),
    ("BOS",  "KBOS", "USW00014739", "Boston Logan"),
    ("AUS",  "KAUS", "USW00013904", "Austin-Bergstrom"),
    ("PHIL", "KPHL", "USW00013739", "Philadelphia Intl"),
    ("HOU",  "KHOU", "USW00012918", "Houston Hobby"),
    ("ATL",  "KATL", "USW00013874", "Atlanta Hartsfield"),
    ("PHX",  "KPHX", "USW00023183", "Phoenix Sky Harbor"),
    ("DC",   "KDCA", "USW00013743", "DC Reagan National"),
    ("LAS",  "KLAS", "USW00023169", "Las Vegas McCarran/Reid"),
    ("SAT",  "KSAT", "USW00012921", "San Antonio Intl"),
    ("MIN",  "KMSP", "USW00014922", "Minneapolis-St Paul"),
    ("DAL",  "KDFW", "USW00003927", "Dallas-Fort Worth"),
    ("SF",   "KSFO", "USW00023234", "San Francisco Intl"),
    ("OKC",  "KOKC", "USW00013967", "Oklahoma City Will Rogers"),
]
