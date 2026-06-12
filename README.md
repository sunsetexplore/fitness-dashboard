# Strava Weather Fitness Tracker

Pulls your runs from Strava, joins each one with historical weather
(temperature, dew point, humidity, wind speed/direction, precipitation) from
Open-Meteo, and computes a weather-adjusted **fitness score**. Runs daily on
GitHub Actions, hands-off, and commits the results back to this repo as
`runs.csv` (and `runs.db`).

---

## 1. Get your Strava API credentials

1. Go to <https://www.strava.com/settings/api> and create an app.
   - **Authorization Callback Domain:** `localhost`
   - Category: anything reasonable (e.g. "Data Importer").
2. Note your **Client ID** and **Client Secret** (click *show* for the secret).
3. In your browser, visit this URL (replace `YOUR_CLIENT_ID`):
   ```
   https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=read,activity:read
   ```
4. Click **Authorize**. Your browser will fail to load a `localhost` page —
   that's expected. Copy the `code` value out of the address bar:
   ```
   http://localhost/?code=THE_CODE_YOU_NEED&scope=read,activity:read
   ```
5. Exchange that code for tokens (terminal):
   ```bash
   curl -X POST https://www.strava.com/oauth/token \
     -F client_id=YOUR_CLIENT_ID \
     -F client_secret=YOUR_CLIENT_SECRET \
     -F code=THE_CODE \
     -F grant_type=authorization_code
   ```
   Copy the **`refresh_token`** from the JSON response. (The access token
   expires every 6 hours; the refresh token is the durable one the script uses.)

You now have three secrets: **client_id**, **client_secret**, **refresh_token**.

---

## 2. Set up the GitHub repo

1. Create a new repo (private is fine) and add these files:
   `strava_weather_sync.py`, `requirements.txt`, `.github/workflows/sync.yml`.
2. Add your secrets: **Settings → Secrets and variables → Actions → New
   repository secret**. Add all three:
   - `STRAVA_CLIENT_ID`
   - `STRAVA_CLIENT_SECRET`
   - `STRAVA_REFRESH_TOKEN`
3. Allow the workflow to commit data back: **Settings → Actions → General →
   Workflow permissions → Read and write permissions → Save**.

---

## 3. Run it

- **First run (full backfill):** go to the **Actions** tab → *Strava Weather
  Sync* → **Run workflow**. The first run pulls your entire history, so it may
  take a few minutes (one Open-Meteo lookup per run).
- **After that:** it runs automatically every day and only fetches new runs.

The data lands in `runs.csv` in the repo. The raw URL
(`https://raw.githubusercontent.com/<you>/<repo>/main/runs.csv`) is what the
dashboard will read from later.

---

## Tuning the fitness score

All knobs live at the top of `strava_weather_sync.py`:

| Constant       | Meaning                                                        |
|----------------|----------------------------------------------------------------|
| `K_HEAT`       | Degradation per point of (temp°F + dew°F) above `NEUTRAL_SUM`.  |
| `K_WIND`       | Degradation per mph of wind.                                    |
| `NEUTRAL_SUM`  | temp°F + dew°F at/below which heat impact is ~zero (default 100).|
| `SCORE_SCALE`  | Cosmetic multiplier so scores land in a readable range.        |

The score = `(speed_kmh / avg_HR) ÷ (1 − degradation) × SCORE_SCALE`. Holding
the same speed/HR in harder conditions yields a **higher** score, because it
implies more underlying fitness.

---

## Notes & caveats

- **Strava terms:** showing your own data to yourself and computing a formula
  score are within the current API terms. The 2024 changes restrict displaying
  others' data and *training AI models* on API data — neither applies here.
- **Rate limits:** the script uses summary activity objects only (no
  per-activity detail calls), so even a full-history backfill stays under
  Strava's limits (200 req/15 min, 2,000/day). It auto-pauses if it ever hits a
  429.
- **Wind direction:** stored, plus a rough run bearing (start→end). True
  per-segment headwind/tailwind needs the full GPS stream — a later upgrade.
- **Recent runs:** weather for the last ~10 days comes from Open-Meteo's
  forecast endpoint; older runs from the ERA5 archive (which lags a few days).
- **Refresh token rotation:** Strava tokens are normally stable. If one ever
  rotates, the job logs the new value so you can update the secret.
