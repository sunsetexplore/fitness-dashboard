#!/usr/bin/env python3
"""
Strava -> Open-Meteo weather join -> weather-adjusted fitness score.

- Pulls run activities from the Strava API (summary objects only -> stays well
  under rate limits, no per-activity detail calls needed).
- For each run, fetches historical weather at the start location/time from
  Open-Meteo (open data, no key, no terms restrictions).
- Computes a weather-adjusted fitness score.
- Stores everything in a CSV (easy for the dashboard to read from the repo's
  raw URL) and a SQLite DB (handy for ad-hoc queries).
- Incremental: on each run it only fetches activities newer than the latest
  one already stored.

Required environment variables (set as GitHub Actions repo secrets):
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  STRAVA_REFRESH_TOKEN
"""

import os
import csv
import math
import time
import sqlite3
import datetime as dt
from typing import Optional

import requests

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
CSV_PATH = "runs.csv"
DB_PATH = "runs.db"

# Fitness-score tuning constants. Adjust these to calibrate against how hard
# runs actually felt for you.
K_HEAT = 0.0006   # degradation per (temp_F + dewpoint_F) point above NEUTRAL_SUM
K_WIND = 0.0020   # degradation per mph of wind
NEUTRAL_SUM = 100.0   # temp_F + dewpoint_F at/below which heat impact ~ 0
SCORE_SCALE = 1000.0  # cosmetic multiplier so scores land in a readable range
MAX_DEGRADATION = 0.5  # safety cap so we never divide by a tiny number

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
OM_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OM_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Fields we keep, in CSV column order.
FIELDS = [
    "id", "name", "start_date_local", "distance_km", "moving_time_s",
    "pace_min_per_km", "avg_hr", "max_hr", "elev_gain_m",
    "start_lat", "start_lng", "run_bearing_deg",
    "temp_f", "dewpoint_f", "humidity_pct", "wind_mph", "wind_dir_deg",
    "wind_vs_run_deg", "precip_mm", "fitness_score",
]


# --------------------------------------------------------------------------
# Strava
# --------------------------------------------------------------------------
def refresh_access_token() -> str:
    """Exchange the long-lived refresh token for a fresh 6-hour access token."""
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Strava *usually* returns the same refresh token, but if it ever rotates,
    # surface it so you can update the GitHub secret.
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != os.environ["STRAVA_REFRESH_TOKEN"]:
        print("NOTICE: Strava issued a NEW refresh token. Update the "
              "STRAVA_REFRESH_TOKEN secret to:", new_refresh)
    return data["access_token"]


def fetch_activities(access_token: str, after_epoch: Optional[int]) -> list:
    """Page through /athlete/activities. Summary objects only."""
    headers = {"Authorization": f"Bearer {access_token}"}
    activities, page = [], 1
    while True:
        params = {"per_page": 200, "page": page}
        if after_epoch:
            params["after"] = after_epoch
        r = requests.get(STRAVA_ACTIVITIES_URL, headers=headers,
                         params=params, timeout=30)
        if r.status_code == 429:
            print("Rate limited by Strava; pausing 15 min...")
            time.sleep(15 * 60 + 5)
            continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        activities.extend(batch)
        page += 1
    return activities


# --------------------------------------------------------------------------
# Weather (Open-Meteo)
# --------------------------------------------------------------------------
HOURLY_VARS = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m",
    "wind_speed_10m", "wind_direction_10m", "precipitation",
]


def _parse_when(start_local: Optional[str]) -> Optional[dt.datetime]:
    if not start_local:
        return None
    try:
        return dt.datetime.fromisoformat(start_local.replace("Z", ""))
    except ValueError:
        return None


def _bucket(lat: float, lng: float):
    """Round to ~0.1 degree (~11 km). ERA5's grid is coarser than this anyway,
    so nearby trailheads share one weather pull with no loss of accuracy."""
    return (round(lat, 1), round(lng, 1))


def _request_hourly(url: str, params: dict, retries: int = 5) -> dict:
    """
    Robust GET: retries on read timeouts, connection drops, and 429s with
    escalating backoff. Returns the 'hourly' block, or {} if it ultimately
    fails (so one bad pull never kills the job).
    """
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=60)
        except (requests.Timeout, requests.ConnectionError) as e:
            wait = 5 * (attempt + 1)
            print(f"  weather request {type(e).__name__}; retry in {wait}s "
                  f"(attempt {attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        if r.status_code == 429:
            wait = 15 * (attempt + 1)
            print(f"  rate limited; waiting {wait}s (attempt {attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json().get("hourly", {})
    print(f"  GAVE UP on weather pull: {params.get('start_date')}..{params.get('end_date')}")
    return {}


def prefetch_archive_weather(runs: list) -> dict:
    """
    Batch the historical weather. Instead of one API call per run, we group
    runs by (rounded location, year) and pull each location's full year of
    hourly data in a single request. A few thousand runs collapses to a few
    dozen requests, which the archive server handles without timing out.

    Returns: cache[bucket] = {"time": [...], "hourly": {var: [...]}, "index": {iso: i}}
    Recent runs (<= 10 days) are skipped here and fetched live later.
    """
    now = dt.datetime.now()
    needed: dict = {}  # bucket -> set(years)
    for a in runs:
        when = _parse_when(a.get("start_date_local"))
        ll = a.get("start_latlng")
        if not when or not ll or len(ll) < 2:
            continue
        if (now - when).days <= 10:
            continue
        needed.setdefault(_bucket(ll[0], ll[1]), set()).add(when.year)

    total_pulls = sum(len(yrs) for yrs in needed.values())
    print(f"Prefetching weather: {len(needed)} locations, {total_pulls} location-year pulls")

    cache, done = {}, 0
    for b, years in needed.items():
        merged_time: list = []
        merged = {k: [] for k in HOURLY_VARS}
        for year in sorted(years):
            start = f"{year}-01-01"
            if year == now.year:
                end_dt = now - dt.timedelta(days=7)  # ERA5 archive lags a few days
                if end_dt.year < year:
                    continue  # too early in the year for any archive data yet
                end = end_dt.strftime("%Y-%m-%d")
            else:
                end = f"{year}-12-31"
            params = {
                "latitude": b[0], "longitude": b[1],
                "start_date": start, "end_date": end,
                "hourly": ",".join(HOURLY_VARS),
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "mm",
                "timezone": "auto",
            }
            hourly = _request_hourly(OM_ARCHIVE_URL, params)
            merged_time.extend(hourly.get("time", []))
            for k in HOURLY_VARS:
                merged[k].extend(hourly.get(k, []))
            done += 1
            if done % 10 == 0 or done == total_pulls:
                print(f"  ...{done}/{total_pulls} pulls done")
            time.sleep(0.5)  # gentle spacing between the (few) batched calls
        cache[b] = {
            "time": merged_time,
            "hourly": merged,
            "index": {t: i for i, t in enumerate(merged_time)},
        }
    return cache


def fetch_weather_live(lat: float, lng: float, when: dt.datetime) -> dict:
    """Single forecast-endpoint call for a recent run (last ~10 days)."""
    date_str = when.strftime("%Y-%m-%d")
    params = {
        "latitude": lat, "longitude": lng,
        "start_date": date_str, "end_date": date_str,
        "hourly": ",".join(HOURLY_VARS),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "mm",
        "timezone": "auto",
    }
    hourly = _request_hourly(OM_FORECAST_URL, params)
    return _extract_hour(hourly, hourly.get("time", []), when)


def weather_for_run(a: dict, cache: dict) -> dict:
    """Look up a run's weather: from the batched cache, or live if it's recent."""
    when = _parse_when(a.get("start_date_local"))
    ll = a.get("start_latlng")
    if not when or not ll or len(ll) < 2:
        return {}
    if (dt.datetime.now() - when).days <= 10:
        return fetch_weather_live(ll[0], ll[1], when)
    data = cache.get(_bucket(ll[0], ll[1]))
    if not data or not data["time"]:
        return {}
    target = when.strftime("%Y-%m-%dT%H:00")
    idx = data["index"].get(target)
    if idx is None:
        idx = _nearest_hour_idx(data["time"], when)
    return _extract_hour(data["hourly"], data["time"], when, idx)


def _extract_hour(hourly: dict, times: list, when: dt.datetime,
                  idx: Optional[int] = None) -> dict:
    if not times:
        return {}
    if idx is None:
        target = when.strftime("%Y-%m-%dT%H:00")
        idx = times.index(target) if target in times else _nearest_hour_idx(times, when)
    return {
        "temp_f": _at(hourly, "temperature_2m", idx),
        "humidity_pct": _at(hourly, "relative_humidity_2m", idx),
        "dewpoint_f": _at(hourly, "dew_point_2m", idx),
        "wind_mph": _at(hourly, "wind_speed_10m", idx),
        "wind_dir_deg": _at(hourly, "wind_direction_10m", idx),
        "precip_mm": _at(hourly, "precipitation", idx),
    }


def _at(hourly: dict, key: str, idx: int):
    arr = hourly.get(key) or []
    return arr[idx] if 0 <= idx < len(arr) else None


def _nearest_hour_idx(times: list, when: dt.datetime) -> int:
    best_i, best_diff = 0, None
    for i, t in enumerate(times):
        ts = dt.datetime.fromisoformat(t)
        diff = abs((ts - when.replace(tzinfo=None)).total_seconds())
        if best_diff is None or diff < best_diff:
            best_i, best_diff = i, diff
    return best_i


# --------------------------------------------------------------------------
# Fitness score
# --------------------------------------------------------------------------
def compute_fitness_score(speed_kmh: float, avg_hr: Optional[float],
                          temp_f: Optional[float], dewpoint_f: Optional[float],
                          wind_mph: Optional[float]) -> Optional[float]:
    """
    Weather-adjusted aerobic-efficiency index.

    Base = speed/HR. We estimate how much the conditions degraded performance
    and divide it out, so the score reflects what the effort implies about
    fitness independent of the weather. Higher = fitter performance.
    """
    if not avg_hr or avg_hr <= 0 or not speed_kmh or speed_kmh <= 0:
        return None
    raw_eff = speed_kmh / avg_hr

    d_heat = 0.0
    if temp_f is not None and dewpoint_f is not None:
        d_heat = K_HEAT * max(0.0, (temp_f + dewpoint_f) - NEUTRAL_SUM)
    d_wind = K_WIND * max(0.0, wind_mph) if wind_mph is not None else 0.0

    degradation = min(MAX_DEGRADATION, d_heat + d_wind)
    adjusted_eff = raw_eff / (1.0 - degradation)
    return round(adjusted_eff * SCORE_SCALE, 2)


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------
def bearing(lat1, lng1, lat2, lng2) -> Optional[float]:
    """Initial compass bearing from start to end point, in degrees."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    if lat1 == lat2 and lng1 == lng2:
        return None  # loop / same point -> bearing undefined
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return round((math.degrees(math.atan2(x, y)) + 360) % 360, 1)


def angle_between(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Smallest angle (0-180) between run bearing and wind direction."""
    if a is None or b is None:
        return None
    d = abs(a - b) % 360
    return round(min(d, 360 - d), 1)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def load_existing_ids() -> set:
    if not os.path.exists(CSV_PATH):
        return set()
    with open(CSV_PATH, newline="") as f:
        return {row["id"] for row in csv.DictReader(f)}


def latest_start_epoch() -> Optional[int]:
    if not os.path.exists(CSV_PATH):
        return None
    latest = None
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts = dt.datetime.fromisoformat(row["start_date_local"])
            except (ValueError, KeyError):
                continue
            if latest is None or ts > latest:
                latest = ts
    return int(latest.timestamp()) if latest else None


def append_rows(rows: list):
    # CSV first — this is the file the dashboard reads, so it's the priority.
    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        for row in rows:
            w.writerow(row)

    # SQLite is a bonus for ad-hoc queries. Wrap it so a DB hiccup can NEVER
    # take down the job after the CSV has already been written safely.
    try:
        con = sqlite3.connect(DB_PATH)
        # "id" is the primary key; the remaining columns are everything else.
        other_cols = ", ".join(f'"{c}" TEXT' for c in FIELDS if c != "id")
        con.execute(f'CREATE TABLE IF NOT EXISTS runs ("id" TEXT PRIMARY KEY, {other_cols})')
        placeholders = ", ".join("?" for _ in FIELDS)
        for row in rows:
            con.execute(
                f'INSERT OR REPLACE INTO runs VALUES ({placeholders})',
                [row.get(c) for c in FIELDS],
            )
        con.commit()
        con.close()
    except Exception as e:
        print(f"NOTE: CSV written fine; SQLite write skipped due to: {e}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    token = refresh_access_token()
    existing = load_existing_ids()
    after = latest_start_epoch()
    print(f"Existing runs: {len(existing)}. Fetching activities"
          + (f" after {dt.datetime.fromtimestamp(after)}" if after else " (full history)"))

    activities = fetch_activities(token, after)

    runs, skipped = [], {"manual": 0, "treadmill": 0, "no_gps": 0, "not_run": 0}
    for a in activities:
        if str(a["id"]) in existing:
            continue
        if a.get("type") != "Run":
            skipped["not_run"] += 1
            continue
        if a.get("manual"):                      # hand-typed entries (incl. missed miles)
            skipped["manual"] += 1
            continue
        if a.get("trainer"):                     # treadmill / indoor
            skipped["treadmill"] += 1
            continue
        latlng = a.get("start_latlng")
        if not latlng or len(latlng) < 2:        # no usable GPS start point
            skipped["no_gps"] += 1
            continue
        runs.append(a)

    print(f"New outdoor GPS runs to process: {len(runs)}")
    print(f"Skipped -> manual: {skipped['manual']}, treadmill: {skipped['treadmill']}, "
          f"no GPS: {skipped['no_gps']}, non-run: {skipped['not_run']}")

    # Batch-pull all historical weather up front (few dozen calls, not thousands).
    weather_cache = prefetch_archive_weather(runs)

    rows = []
    for a in runs:
      try:
        start = a.get("start_latlng") or [None, None]
        end = a.get("end_latlng") or [None, None]
        slat, slng = (start + [None, None])[:2]
        elat, elng = (end + [None, None])[:2]

        dist_m = a.get("distance") or 0.0
        move_s = a.get("moving_time") or 0.0
        speed_kmh = (dist_m / move_s) * 3.6 if move_s else 0.0
        pace = (move_s / 60.0) / (dist_m / 1000.0) if dist_m else None

        start_local = a.get("start_date_local")

        wx = {}
        try:
            wx = weather_for_run(a, weather_cache)
        except Exception as e:  # don't let one bad lookup kill the whole run
            print(f"Weather lookup failed for activity {a['id']}: {e}")

        run_bear = bearing(slat, slng, elat, elng)
        score = compute_fitness_score(
            speed_kmh, a.get("average_heartrate"),
            wx.get("temp_f"), wx.get("dewpoint_f"), wx.get("wind_mph"),
        )

        rows.append({
            "id": str(a["id"]),
            "name": a.get("name"),
            "start_date_local": start_local,
            "distance_km": round(dist_m / 1000.0, 3),
            "moving_time_s": int(move_s),
            "pace_min_per_km": round(pace, 3) if pace else None,
            "avg_hr": a.get("average_heartrate"),
            "max_hr": a.get("max_heartrate"),
            "elev_gain_m": a.get("total_elevation_gain"),
            "start_lat": slat,
            "start_lng": slng,
            "run_bearing_deg": run_bear,
            "temp_f": wx.get("temp_f"),
            "dewpoint_f": wx.get("dewpoint_f"),
            "humidity_pct": wx.get("humidity_pct"),
            "wind_mph": wx.get("wind_mph"),
            "wind_dir_deg": wx.get("wind_dir_deg"),
            "wind_vs_run_deg": angle_between(run_bear, wx.get("wind_dir_deg")),
            "precip_mm": wx.get("precip_mm"),
            "fitness_score": score,
        })
      except Exception as e:
        print(f"Skipping activity {a.get('id')} due to error: {e}")

    if rows:
        rows.sort(key=lambda r: r["start_date_local"] or "")
        append_rows(rows)
        print(f"Wrote {len(rows)} new runs to {CSV_PATH} and {DB_PATH}")
    else:
        print("Nothing new to write.")


if __name__ == "__main__":
    import sys
    import traceback
    try:
        sys.stdout.reconfigure(line_buffering=True)  # live logs in CI
    except Exception:
        pass
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
