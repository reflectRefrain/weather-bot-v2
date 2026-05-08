"""
Verified Kalshi KXHIGH series tickers for our 18 cities.
Discovered by enumerating all KXHIGH* series via /trade-api/v2/series.
"""

CITY_SERIES = {
    "NYC":  "KXHIGHNY",
    "CHI":  "KXHIGHCHI",
    "MIA":  "KXHIGHMIA",
    "LAX":  "KXHIGHLAX",
    "DEN":  "KXHIGHDEN",
    "BOS":  "KXHIGHTBOS",
    "AUS":  "KXHIGHAUS",
    "PHIL": "KXHIGHPHIL",
    "HOU":  "KXHIGHTHOU",   # KXHIGHTHOU is the active series; KXHIGHHOU and KXHIGHOU are stale
    "ATL":  "KXHIGHTATL",
    "PHX":  "KXHIGHTPHX",
    "DC":   "KXHIGHTDC",
    "LAS":  "KXHIGHTLV",
    "SAT":  "KXHIGHTSATX",
    "MIN":  "KXHIGHTMIN",
    "DAL":  "KXHIGHTDAL",
    "SF":   "KXHIGHTSFO",
    "OKC":  "KXHIGHTOKC",
}
