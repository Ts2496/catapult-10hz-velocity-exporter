# Catapult 10Hz Raw Velocity Exporter

A secure Streamlit app for exporting period-level Catapult OpenField Connect 10Hz raw velocity data to CSV for R analysis.

## What this app exports

The app exports period-level 10Hz data only.

CSV columns include:

- athlete_name
- athlete_id
- activity_name
- activity_id
- period_name
- period_id
- stream_type
- device_id
- timestamp_unix_s
- centiseconds
- timestamp_s
- datetime_utc
- raw_velocity_mps

The Catapult sensor parameters requested are:

```text
ts,cs,rv
```

Where:

- `ts` = timestamp in seconds
- `cs` = centiseconds
- `rv` = raw velocity

## What this app does not export

This app deliberately does not export:

- full activity sensor files
- acceleration/deceleration events
- IMA events
- efforts
- general sensor data beyond timestamp and raw velocity

## Security model

The app has two separate layers:

1. **App password**  
   Stored in Streamlit Secrets as `APP_PASSWORD`.

2. **Catapult API token**  
   Entered manually at runtime in a password-style input field. It is not stored in GitHub, Streamlit Secrets, or any local file.

CSV exports are generated in memory and downloaded by the user. The app does not intentionally write athlete data to disk.

## Streamlit Cloud setup

After deploying the app, go to:

```text
App > Settings > Secrets
```

Add:

```toml
APP_PASSWORD = "replace-this-with-a-strong-password"
```

Do not add your Catapult API token here if you want to enter it manually at runtime.

## Deployment files

The repository should contain:

```text
app.py
requirements.txt
.gitignore
README.md
```

Do not upload `.streamlit/secrets.toml` to GitHub.

## User flow

1. Open the Streamlit app
2. Enter the app password
3. Select Catapult region
4. Paste Catapult API token
5. Select session date
6. Load activities and periods
7. Select activity
8. Select period
9. Extract raw 10Hz velocity CSV
10. Download CSV and analyse in R

## Notes

The app extracts from periods only. Activity selection is only used to help find the correct period.

Internally, the app retrieves all athletes in the selected period and loops through them because the Catapult period sensor endpoint requires both `period_id` and `athlete_id`.
