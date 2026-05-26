import csv
import hmac
import io
import re
import time
from datetime import datetime, time as dt_time, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st


# ============================================================
# Catapult OpenField Connect: Period 10Hz Raw Velocity Exporter
# ============================================================
# Purpose:
# - Select region
# - Paste Catapult API token at runtime
# - Select date
# - Select activity to help find the correct period
# - Select period
# - Export all athletes in the period with timestamp + raw velocity only
#
# Security design:
# - App is password protected using APP_PASSWORD stored in Streamlit Secrets
# - Catapult API token is entered at runtime and is not stored in code
# - Token is never printed, written to file, or cached
# - CSV is generated in memory only
# ============================================================


REGION_BASE_URLS = {
    "EMEA / Europe": "https://connect-eu.catapultsports.com/api/v6",
    "Americas": "https://connect-us.catapultsports.com/api/v6",
    "Asia-Pacific": "https://connect-au.catapultsports.com/api/v6",
    "China": "https://connect-cn.catapultsports-cn.com/api/v6",
}

RAW_VELOCITY_PARAMETERS = "ts,cs,rv"
DEFAULT_TIMEOUT_SECONDS = 45


# -----------------------------
# Streamlit page config
# -----------------------------
st.set_page_config(
    page_title="Catapult 10Hz Velocity Exporter",
    page_icon="📈",
    layout="wide",
)


# -----------------------------
# Security / app password
# -----------------------------
def check_app_password() -> bool:
    """Block the app until the user enters the app password.

    The password should be stored in Streamlit Cloud under:
    Settings > Secrets

    Example:
    APP_PASSWORD = "your-strong-password-here"
    """

    if "app_authenticated" not in st.session_state:
        st.session_state["app_authenticated"] = False

    if st.session_state["app_authenticated"]:
        return True

    st.title("Catapult 10Hz Velocity Exporter")
    st.subheader("Private access")

    expected_password = st.secrets.get("APP_PASSWORD", None)

    if not expected_password:
        st.error(
            "APP_PASSWORD has not been configured in Streamlit Secrets. "
            "Add APP_PASSWORD before deploying this app."
        )
        with st.expander("How to fix this"):
            st.code('APP_PASSWORD = "replace-this-with-a-strong-password"', language="toml")
        return False

    entered_password = st.text_input("App password", type="password")

    if st.button("Unlock app"):
        if hmac.compare_digest(entered_password, expected_password):
            st.session_state["app_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect app password.")

    return False


# -----------------------------
# Utility functions
# -----------------------------
def make_headers(api_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "User-Agent": "Catapult10HzVelocityExporter/1.0",
    }


def unix_range_for_date(selected_date) -> Tuple[int, int]:
    start = datetime.combine(selected_date, dt_time.min).replace(tzinfo=timezone.utc)
    end = datetime.combine(selected_date, dt_time.max).replace(tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def extract_list(payload: Any) -> List[Dict[str, Any]]:
    """Normalise common API response shapes into a list of dictionaries."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ["data", "items", "results", "activities", "periods", "athletes"]:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def request_json(
    base_url: str,
    endpoint: str,
    api_token: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """Make a GET request and return decoded JSON.

    Raises requests.HTTPError if the request fails.
    """
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    response = requests.get(
        url,
        headers=make_headers(api_token),
        params=params,
        timeout=timeout,
    )

    if response.status_code == 401:
        raise requests.HTTPError("401 unauthenticated: check the Catapult API token.")
    if response.status_code == 403:
        raise requests.HTTPError("403 forbidden: token may not have access to this data.")
    if response.status_code == 404:
        raise requests.HTTPError(f"404 not found: endpoint or ID not found: {endpoint}")
    if response.status_code == 422:
        raise requests.HTTPError(f"422 unprocessable request: {response.text[:500]}")

    response.raise_for_status()

    if not response.text.strip():
        return []

    return response.json()


def safe_get(item: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for key in keys:
        if key in item and item[key] not in [None, ""]:
            return item[key]
    return default


def parse_epoch(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (ValueError, TypeError):
        return None


def short_id(value: str, length: int = 8) -> str:
    if not value:
        return "unknown"
    return str(value)[:length]


def clean_filename(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_\-]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_") or "catapult_10hz_velocity"


# -----------------------------
# Catapult API functions
# -----------------------------
def get_all_periods(base_url: str, api_token: str) -> List[Dict[str, Any]]:
    payload = request_json(base_url, "/periods", api_token)
    return extract_list(payload)


def get_activities_optional(
    base_url: str,
    api_token: str,
    start_unix: int,
    end_unix: int,
) -> List[Dict[str, Any]]:
    """Try to retrieve activities.

    Catapult Connect commonly exposes /activities, but filtering options can vary.
    This function is intentionally defensive. If activity retrieval fails, the app
    can still proceed using activity_id values from /periods.
    """

    attempts = [
        {"start_time": start_unix, "end_time": end_unix},
        {"from": start_unix, "to": end_unix},
        None,
    ]

    for params in attempts:
        try:
            payload = request_json(base_url, "/activities", api_token, params=params)
            activities = extract_list(payload)
            if activities:
                return activities
        except Exception:
            continue

    return []


def get_athletes_in_period(
    base_url: str,
    api_token: str,
    period_id: str,
) -> List[Dict[str, Any]]:
    payload = request_json(base_url, f"/periods/{period_id}/athletes", api_token)
    return extract_list(payload)


def get_period_sensor_page(
    base_url: str,
    api_token: str,
    period_id: str,
    athlete_id: str,
    stream_type: Optional[str],
    start_time: Optional[int],
    end_time: Optional[int],
    page: int,
    page_size_seconds: int,
    nulls: bool = True,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "parameters": RAW_VELOCITY_PARAMETERS,
        "page": page,
        "page_size_seconds": page_size_seconds,
    }

    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    if stream_type and stream_type != "Default from OpenField":
        params["stream_type"] = stream_type.lower()
    if nulls:
        params["nulls"] = 1

    payload = request_json(
        base_url,
        f"/periods/{period_id}/athletes/{athlete_id}/sensor",
        api_token,
        params=params,
        timeout=90,
    )
    return extract_list(payload)


# -----------------------------
# Data processing
# -----------------------------
def activity_display_name(activity: Dict[str, Any]) -> str:
    activity_id = str(safe_get(activity, ["id", "activity_id"], "unknown"))
    name = safe_get(activity, ["name", "activity_name", "title"], None)
    start = parse_epoch(safe_get(activity, ["start_time", "start", "start_timestamp"], None))

    date_text = "unknown date"
    if start:
        date_text = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d")

    if name:
        return f"{name} | {date_text} | {short_id(activity_id)}"
    return f"Activity {short_id(activity_id)} | {date_text}"


def period_display_name(period: Dict[str, Any]) -> str:
    period_id = str(period.get("id", "unknown"))
    name = str(period.get("name", "Unnamed period"))
    start = parse_epoch(period.get("start_time"))
    end = parse_epoch(period.get("end_time"))

    if start and end:
        start_text = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%H:%M:%S")
        end_text = datetime.fromtimestamp(end, tz=timezone.utc).strftime("%H:%M:%S")
        return f"{name} | {start_text}–{end_text} | {short_id(period_id)}"

    return f"{name} | {short_id(period_id)}"


def build_activity_options(
    periods_for_date: List[Dict[str, Any]],
    activities: List[Dict[str, Any]],
    selected_start_unix: int,
    selected_end_unix: int,
) -> Dict[str, Dict[str, Any]]:
    """Create activity dropdown options.

    Activities are mainly used to help locate the correct period. Extraction is
    period-based.
    """

    activity_map: Dict[str, Dict[str, Any]] = {}

    # Add activities from /activities where available.
    for activity in activities:
        activity_id = str(safe_get(activity, ["id", "activity_id"], ""))
        if not activity_id:
            continue

        start = parse_epoch(safe_get(activity, ["start_time", "start", "start_timestamp"], None))
        if start and not (selected_start_unix <= start <= selected_end_unix):
            continue

        activity_map[activity_id] = activity

    # Add activity IDs from periods, as a fallback or supplement.
    for period in periods_for_date:
        activity_id = str(period.get("activity_id", ""))
        if not activity_id:
            continue
        if activity_id not in activity_map:
            activity_map[activity_id] = {
                "id": activity_id,
                "name": f"Activity ID {short_id(activity_id)}",
                "start_time": period.get("start_time"),
            }

    options = {}
    for activity_id, activity in activity_map.items():
        period_count = sum(1 for p in periods_for_date if str(p.get("activity_id")) == activity_id)
        label = f"{activity_display_name(activity)} | {period_count} period(s)"
        options[label] = {
            "activity_id": activity_id,
            "activity": activity,
            "periods": [p for p in periods_for_date if str(p.get("activity_id")) == activity_id],
        }

    return dict(sorted(options.items(), key=lambda x: x[0].lower()))


def filter_periods_by_date(
    periods: List[Dict[str, Any]],
    start_unix: int,
    end_unix: int,
) -> List[Dict[str, Any]]:
    filtered = []
    for period in periods:
        period_start = parse_epoch(period.get("start_time"))
        period_end = parse_epoch(period.get("end_time"))

        if period_start is None:
            continue

        # Include if the period starts on the selected date or overlaps the date.
        starts_on_date = start_unix <= period_start <= end_unix
        overlaps_date = period_end is not None and period_start <= end_unix and period_end >= start_unix

        if starts_on_date or overlaps_date:
            filtered.append(period)

    return sorted(filtered, key=lambda p: parse_epoch(p.get("start_time")) or 0)


def flatten_sensor_payload(
    sensor_blocks: List[Dict[str, Any]],
    athlete: Dict[str, Any],
    period: Dict[str, Any],
    activity_label: str,
) -> List[Dict[str, Any]]:
    rows = []

    athlete_id = str(safe_get(athlete, ["id", "athlete_id"], ""))
    first_name = str(safe_get(athlete, ["first_name", "athlete_first_name"], "")).strip()
    last_name = str(safe_get(athlete, ["last_name", "athlete_last_name"], "")).strip()
    fallback_athlete_name = " ".join([first_name, last_name]).strip() or athlete_id

    period_id = str(period.get("id", ""))
    period_name = str(period.get("name", ""))
    activity_id = str(period.get("activity_id", ""))

    for block in sensor_blocks:
        block_first = str(block.get("athlete_first_name", "")).strip()
        block_last = str(block.get("athlete_last_name", "")).strip()
        athlete_name = " ".join([block_first, block_last]).strip() or fallback_athlete_name

        stream_type = block.get("stream_type", None)
        device_id = block.get("device_id", None)
        data = block.get("data", [])

        if not isinstance(data, list):
            continue

        for sample in data:
            if not isinstance(sample, dict):
                continue

            ts = sample.get("ts", None)
            cs = sample.get("cs", 0)
            rv = sample.get("rv", None)

            try:
                timestamp_seconds = float(ts) + (float(cs or 0) / 100.0)
                datetime_utc = datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc).isoformat()
            except (ValueError, TypeError, OSError):
                timestamp_seconds = None
                datetime_utc = None

            rows.append(
                {
                    "athlete_name": athlete_name,
                    "athlete_id": athlete_id,
                    "activity_name": activity_label,
                    "activity_id": activity_id,
                    "period_name": period_name,
                    "period_id": period_id,
                    "stream_type": stream_type,
                    "device_id": device_id,
                    "timestamp_unix_s": ts,
                    "centiseconds": cs,
                    "timestamp_s": timestamp_seconds,
                    "datetime_utc": datetime_utc,
                    "raw_velocity_mps": rv,
                }
            )

    return rows


def fetch_all_velocity_for_athlete(
    base_url: str,
    api_token: str,
    period_id: str,
    athlete_id: str,
    stream_type: Optional[str],
    start_time: Optional[int],
    end_time: Optional[int],
    page_size_seconds: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    all_blocks: List[Dict[str, Any]] = []

    for page in range(1, max_pages + 1):
        blocks = get_period_sensor_page(
            base_url=base_url,
            api_token=api_token,
            period_id=period_id,
            athlete_id=athlete_id,
            stream_type=stream_type,
            start_time=start_time,
            end_time=end_time,
            page=page,
            page_size_seconds=page_size_seconds,
            nulls=True,
        )

        rows_on_page = sum(len(block.get("data", [])) for block in blocks if isinstance(block, dict))

        if rows_on_page == 0:
            break

        all_blocks.extend(blocks)

        # Expected number of rows is roughly 10Hz * seconds.
        # If a page returns clearly fewer rows than expected, it is probably the last page.
        expected_rows = page_size_seconds * 10
        if rows_on_page < expected_rows * 0.5:
            break

        time.sleep(0.05)  # small pause to reduce request pressure

    return all_blocks


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    df.to_csv(buffer, index=False, quoting=csv.QUOTE_MINIMAL)
    return buffer.getvalue().encode("utf-8")


# -----------------------------
# Main app
# -----------------------------
if not check_app_password():
    st.stop()

st.title("Catapult OpenField 10Hz Raw Velocity Exporter")
st.caption("Period-level export only: athlete name, timestamp, and raw velocity for R analysis.")

with st.sidebar:
    st.header("Settings")

    region = st.selectbox(
        "Region",
        options=list(REGION_BASE_URLS.keys()),
        index=0,
        help="For UK/Europe, use EMEA / Europe.",
    )
    base_url = REGION_BASE_URLS[region]

    api_token = st.text_input(
        "Catapult API token",
        type="password",
        help="This is used only during the current session. It is not saved in code or written to disk.",
    )

    selected_date = st.date_input("Session date")

    stream_type = st.selectbox(
        "Stream type",
        options=["Default from OpenField", "GPS", "LPS"],
        index=0,
    )

    page_size_seconds = st.selectbox(
        "Page size seconds",
        options=[30, 60, 120, 300],
        index=1,
        help="Smaller values are safer. 60 seconds returns about 600 rows per athlete per page.",
    )

    max_pages = st.number_input(
        "Maximum pages per athlete",
        min_value=1,
        max_value=500,
        value=100,
        step=1,
        help="Safety limit to avoid accidental huge requests.",
    )

    if st.button("Log out of app"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()

st.info(
    "This app does not export full activities. It uses the selected activity only to find the correct period, "
    "then extracts raw 10Hz velocity from that period for all athletes in the period."
)

if not api_token:
    st.warning("Enter your Catapult API token in the sidebar to begin.")
    st.stop()

start_unix, end_unix = unix_range_for_date(selected_date)

col1, col2 = st.columns([1, 2])

with col1:
    load_clicked = st.button("1. Load activities and periods", type="primary")

with col2:
    st.write(
        f"Selected date range: `{datetime.fromtimestamp(start_unix, tz=timezone.utc).isoformat()}` "
        f"to `{datetime.fromtimestamp(end_unix, tz=timezone.utc).isoformat()}`"
    )

if load_clicked:
    with st.spinner("Loading periods and activities from Catapult..."):
        try:
            all_periods = get_all_periods(base_url, api_token)
            periods_for_date = filter_periods_by_date(all_periods, start_unix, end_unix)
            activities = get_activities_optional(base_url, api_token, start_unix, end_unix)
            activity_options = build_activity_options(
                periods_for_date=periods_for_date,
                activities=activities,
                selected_start_unix=start_unix,
                selected_end_unix=end_unix,
            )

            st.session_state["periods_for_date"] = periods_for_date
            st.session_state["activity_options"] = activity_options
            st.session_state["activities_loaded"] = True

            st.success(
                f"Loaded {len(periods_for_date)} period(s) on this date across "
                f"{len(activity_options)} activity option(s)."
            )
        except Exception as exc:
            st.session_state["activities_loaded"] = False
            st.error("Could not load Catapult data.")
            st.exception(exc)

if not st.session_state.get("activities_loaded"):
    st.stop()

activity_options = st.session_state.get("activity_options", {})

if not activity_options:
    st.error("No activities/periods found for the selected date.")
    st.stop()

selected_activity_label = st.selectbox(
    "2. Select activity",
    options=list(activity_options.keys()),
)

selected_activity_bundle = activity_options[selected_activity_label]
activity_periods = selected_activity_bundle["periods"]

if not activity_periods:
    st.error("No periods found for this activity.")
    st.stop()

period_options = {period_display_name(period): period for period in activity_periods}

selected_period_label = st.selectbox(
    "3. Select period",
    options=list(period_options.keys()),
)

selected_period = period_options[selected_period_label]
selected_period_id = str(selected_period.get("id"))
period_start = parse_epoch(selected_period.get("start_time"))
period_end = parse_epoch(selected_period.get("end_time"))

with st.expander("Selected period details"):
    st.json(
        {
            "activity": selected_activity_label,
            "period": selected_period.get("name"),
            "period_id": selected_period_id,
            "activity_id": selected_period.get("activity_id"),
            "period_start_unix": period_start,
            "period_end_unix": period_end,
            "parameters_to_export": RAW_VELOCITY_PARAMETERS,
        }
    )

extract_clicked = st.button("4. Extract raw 10Hz velocity CSV", type="primary")

if extract_clicked:
    if not selected_period_id:
        st.error("Selected period has no period ID.")
        st.stop()

    with st.spinner("Loading athletes in selected period..."):
        try:
            athletes = get_athletes_in_period(base_url, api_token, selected_period_id)
        except Exception as exc:
            st.error("Could not load athletes in the selected period.")
            st.exception(exc)
            st.stop()

    if not athletes:
        st.error("No athletes found in this period.")
        st.stop()

    st.write(f"Found **{len(athletes)} athlete(s)** in this period.")

    progress = st.progress(0)
    status = st.empty()
    all_rows: List[Dict[str, Any]] = []
    failed_athletes: List[str] = []

    for idx, athlete in enumerate(athletes, start=1):
        athlete_id = str(safe_get(athlete, ["id", "athlete_id"], ""))
        first = str(safe_get(athlete, ["first_name", "athlete_first_name"], "")).strip()
        last = str(safe_get(athlete, ["last_name", "athlete_last_name"], "")).strip()
        athlete_name = " ".join([first, last]).strip() or athlete_id

        status.write(f"Extracting {idx}/{len(athletes)}: {athlete_name}")

        try:
            sensor_blocks = fetch_all_velocity_for_athlete(
                base_url=base_url,
                api_token=api_token,
                period_id=selected_period_id,
                athlete_id=athlete_id,
                stream_type=stream_type,
                start_time=period_start,
                end_time=period_end,
                page_size_seconds=int(page_size_seconds),
                max_pages=int(max_pages),
            )

            athlete_rows = flatten_sensor_payload(
                sensor_blocks=sensor_blocks,
                athlete=athlete,
                period=selected_period,
                activity_label=selected_activity_label,
            )
            all_rows.extend(athlete_rows)
        except Exception as exc:
            failed_athletes.append(f"{athlete_name} ({athlete_id}): {exc}")

        progress.progress(idx / len(athletes))
        time.sleep(0.05)

    status.write("Extraction complete.")

    if not all_rows:
        st.error("No raw velocity rows were returned. Check that the period is full-synced and has sensor data.")
        if failed_athletes:
            with st.expander("Failed athlete requests"):
                for item in failed_athletes:
                    st.write(item)
        st.stop()

    df = pd.DataFrame(all_rows)

    # Stable export column order.
    ordered_columns = [
        "athlete_name",
        "athlete_id",
        "activity_name",
        "activity_id",
        "period_name",
        "period_id",
        "stream_type",
        "device_id",
        "timestamp_unix_s",
        "centiseconds",
        "timestamp_s",
        "datetime_utc",
        "raw_velocity_mps",
    ]
    df = df[[col for col in ordered_columns if col in df.columns]]

    st.success(f"Export ready: {len(df):,} rows across {df['athlete_name'].nunique()} athlete(s).")

    if failed_athletes:
        st.warning(f"{len(failed_athletes)} athlete request(s) failed. CSV was still created for successful athletes.")
        with st.expander("Failed athlete requests"):
            for item in failed_athletes:
                st.write(item)

    st.subheader("Preview")
    st.dataframe(df.head(200), use_container_width=True)

    file_stub = clean_filename(
        f"catapult_10hz_velocity_{selected_date}_{selected_period.get('name', 'period')}"
    )
    csv_bytes = dataframe_to_csv_bytes(df)

    st.download_button(
        label="Download CSV",
        data=csv_bytes,
        file_name=f"{file_stub}.csv",
        mime="text/csv",
    )

    with st.expander("Column definitions"):
        st.markdown(
            """
- `athlete_name`: athlete first and last name from Catapult
- `athlete_id`: Catapult athlete ID used internally for the API request
- `activity_name`: selected activity label used to locate the period
- `period_name`: selected period name
- `timestamp_unix_s`: Catapult timestamp in seconds
- `centiseconds`: Catapult centisecond field
- `timestamp_s`: combined timestamp as `ts + cs / 100`
- `datetime_utc`: UTC timestamp converted from `timestamp_s`
- `raw_velocity_mps`: Catapult `rv` field, raw velocity in metres per second
            """
        )
