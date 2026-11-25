"""
Requirements: requests, pandas, openpyxl, and gtfs-realtime-bindings
Install with: pip install requests pandas openpyxl gtfs-realtime-bindings

Example:
    python tools/accuracy_logger.py --hours 2
"""

import argparse
import csv
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2
from openpyxl.styles import Font, PatternFill


MYT = timezone(timedelta(hours=8), "Asia/Kuala_Lumpur")
RAW_FEED_URL = (
    "https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana"
    "?category=rapid-bus-kl"
)
REQUEST_TIMEOUT_SECONDS = 20
MISSING_POLLS_TO_CLOSE = 3
SAVE_INTERVAL_SECONDS = 10 * 60
LOST_ARRIVAL_GRACE_SECONDS = 5 * 60
RAW_FEED_LOG_DISTANCE_KM = 2.0
DISTANCE_BRACKETS = (
    ("<0.5", 0.0, 0.5),
    ("0.5-1", 0.5, 1.0),
    ("1-2", 1.0, 2.0),
    ("2-3", 2.0, 3.0),
    ("3-5", 3.0, 5.0),
    ("5-7", 5.0, 7.0),
    ("7-10", 7.0, 10.0),
    ("10+", 10.0, math.inf),
)
RAW_FIELDS = (
    "timestamp",
    "vehicle_id",
    "route",
    "predicted_minutes",
    "distance_km",
    "latitude",
    "longitude",
    "source",
    "websocket_connected",
    "gtfs_buses_live",
)
RAW_LOG_FIELDS = RAW_FIELDS + (
    "actual_remaining_min",
    "error_min",
    "abs_error_min",
    "within_5_min",
    "distance_bracket",
)
RAW_FEED_FIELDS = (
    "timestamp",
    "vehicle_id",
    "latitude",
    "longitude",
    "distance_to_stop_km",
)


def malaysia_now():
    """Return the current time in Malaysia Standard Time (UTC+8)."""
    return datetime.now(MYT)


def excel_datetime(value):
    """Excel does not support timezone-aware datetimes; store local wall time."""
    if value is None:
        return None
    return value.astimezone(MYT).replace(tzinfo=None)


def parse_number(value):
    """Convert optional numeric API fields to floats without raising."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def distance_bracket(distance_km):
    """Use the distance thresholds applied by the server."""
    if distance_km is None:
        return None
    for label, lower, upper in DISTANCE_BRACKETS:
        if lower <= distance_km < upper:
            return label
    return None


def haversine_m(latitude1, longitude1, latitude2, longitude2):
    """Calculate the great-circle distance between two coordinates in metres."""
    earth_radius_m = 6371000
    lat1, lat2 = math.radians(latitude1), math.radians(latitude2)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(longitude2 - longitude1)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(math.sqrt(min(haversine, 1)))


def log_event(events, message, timestamp=None):
    events.append({
        "timestamp": timestamp or malaysia_now(),
        "event": message,
    })


def score_observation(observation, arrival_time):
    predicted = observation["predicted_minutes"]
    if predicted is None:
        return
    actual_remaining = (arrival_time - observation["timestamp"]).total_seconds() / 60
    error = actual_remaining - predicted
    observation.update({
        "actual_remaining_min": actual_remaining,
        "error_min": error,
        "abs_error_min": abs(error),
        "within_5_min": abs(error) <= 5,
        "distance_bracket": distance_bracket(observation["distance_km"]),
    })


def register_arrival(track, arrival_time):
    """Set the first raw-feed arrival time and score all predictions so far."""
    if track["arrival_time"] is not None:
        return
    track["arrival_time"] = arrival_time
    track["status"] = "Arrived"
    for observation in track["observations"]:
        score_observation(observation, arrival_time)


def close_track(track):
    """Finalize a track as Lost unless a raw-feed arrival was already detected."""
    if track["arrival_time"] is None:
        track["status"] = "Lost"
        track["arrival_time"] = None
    else:
        track["status"] = "Arrived"
        for observation in track["observations"]:
            score_observation(observation, track["arrival_time"])
    track["number_of_observations"] = len(track["observations"])


def metric_values(observations):
    errors = [item["error_min"] for item in observations if item["error_min"] is not None]
    absolute_errors = [abs(error) for error in errors]
    return {
        "observations": len(errors),
        "mean_absolute_error": sum(absolute_errors) / len(errors) if errors else None,
        "mean_signed_error": sum(errors) / len(errors) if errors else None,
        "largest_absolute_error": max(absolute_errors) if errors else None,
        "within_5_minutes_percent": (
            sum(1 for item in observations if item["within_5_min"] is True) / len(errors) * 100
            if errors else None
        ),
    }


def format_min_seconds(minutes, signed=False):
    if minutes is None:
        return ""
    total_seconds = round(minutes * 60)
    sign = "-" if total_seconds < 0 else ("+" if signed and total_seconds > 0 else "")
    total_seconds = abs(total_seconds)
    whole_minutes, seconds = divmod(total_seconds, 60)
    return "{}{}:{:02d}".format(sign, whole_minutes, seconds)


def build_report_table(tracks):
    rows = []
    targets = (7, 5, 3, 1)
    for track in tracks:
        if track["status"] != "Arrived":
            continue
        observations = track["observations"]
        for target in targets:
            nearby = [
                observation for observation in observations
                if observation["distance_km"] is not None
                and abs(observation["distance_km"] - target) <= 0.5
                and observation["actual_remaining_min"] is not None
            ]
            if not nearby:
                continue
            observation = min(nearby, key=lambda item: abs(item["distance_km"] - target))
            rows.append({
                "No.": len(rows) + 1,
                "Route": track["route"],
                "Vehicle": track["vehicle_id"],
                "Observation time": excel_datetime(observation["timestamp"]),
                "Distance (km)": observation["distance_km"],
                "Predicted (min)": observation["predicted_minutes"],
                "Actual (min:s)": format_min_seconds(observation["actual_remaining_min"]),
                "Error (min:s)": format_min_seconds(observation["error_min"], signed=True),
                "Within +/-5 min": "Yes" if observation["within_5_min"] else "No",
            })
    return rows


def prepare_workbook_sheets(run_start, run_end, config, poll_count, failed_polls,
                            raw_poll_count, raw_rows, raw_feed_rows, tracks, events):
    arrived_tracks = [track for track in tracks if track["status"] == "Arrived"]
    scored_observations = [
        observation
        for track in arrived_tracks
        for observation in track["observations"]
        if observation["error_min"] is not None
    ]
    overall = metric_values(scored_observations)

    summary = [
        ("Run start", excel_datetime(run_start)),
        ("Run end", excel_datetime(run_end)),
        ("Number of polls", poll_count),
        ("Number of raw feed polls", raw_poll_count),
        ("Failed polls", failed_polls),
        ("Number of tracks", len(tracks)),
        ("Number arrived", len(arrived_tracks)),
        ("Number lost", sum(1 for track in tracks if track["status"] == "Lost")),
        ("Number of observations used", overall["observations"]),
        ("Overall mean absolute error (min)", overall["mean_absolute_error"]),
        ("Mean signed error (min)", overall["mean_signed_error"]),
        ("Largest absolute error (min)", overall["largest_absolute_error"]),
        ("Percentage within +/-5 minutes", overall["within_5_minutes_percent"]),
    ]

    by_distance = []
    for label, _, _ in DISTANCE_BRACKETS:
        values = metric_values([
            observation for observation in scored_observations
            if observation["distance_bracket"] == label
        ])
        by_distance.append({
            "Distance bracket (km)": label,
            "Number of observations": values["observations"],
            "Mean absolute error (min)": values["mean_absolute_error"],
            "Mean signed error (min)": values["mean_signed_error"],
            "Largest absolute error (min)": values["largest_absolute_error"],
            "Percentage within +/-5 minutes": values["within_5_minutes_percent"],
        })

    arrivals = []
    per_bus = []
    for track in tracks:
        track_metrics = metric_values([
            observation for observation in track["observations"]
            if observation["error_min"] is not None
        ])
        arrivals.append({
            key: excel_datetime(value) if key in ("first_seen", "last_seen", "arrival_time") else value
            for key, value in track.items()
            if not key.startswith("_") and key != "observations"
        })
        per_bus.append({
            "track_id": track["track_id"],
            "vehicle_id": track["vehicle_id"],
            "route": track["route"],
            "first_seen": excel_datetime(track["first_seen"]),
            "last_seen": excel_datetime(track["last_seen"]),
            "arrival_time": excel_datetime(track["arrival_time"]),
            "status": track["status"],
            "first_distance_km": track["first_distance_km"],
            "last_distance_km": track["last_distance_km"],
            "number_of_observations": track["number_of_observations"],
            "observations_scored": track_metrics["observations"],
            "mean_absolute_error_min": track_metrics["mean_absolute_error"],
            "mean_signed_error_min": track_metrics["mean_signed_error"],
            "largest_absolute_error_min": track_metrics["largest_absolute_error"],
            "within_5_minutes_percent": track_metrics["within_5_minutes_percent"],
        })

    raw_log = []
    for row in raw_rows:
        exported = dict(row)
        exported["timestamp"] = excel_datetime(exported["timestamp"])
        raw_log.append(exported)

    assumptions = [
        ("Base URL", config.base_url),
        ("Stop ID", config.stop_id),
        ("Raw GTFS Realtime URL", RAW_FEED_URL),
        ("Interval (seconds)", config.interval),
        ("Duration (hours)", config.hours),
        ("Raw feed interval (seconds)", config.raw_interval),
        ("Stop latitude", config.stop_lat),
        ("Stop longitude", config.stop_lon),
        ("Arrival radius (m)", config.arrival_radius_m),
        (
            "Arrival-time method",
            "The reference arrival time comes from the raw GTFS Realtime feed "
            "(the same data source as the server's prediction), so the test checks "
            "the estimation method and not the feed itself.",
        ),
    ]

    return {
        "Summary": pd.DataFrame(summary, columns=("Metric", "Value")),
        "Per_Bus": pd.DataFrame(per_bus, columns=(
            "track_id", "vehicle_id", "route", "first_seen", "last_seen", "arrival_time",
            "status", "first_distance_km", "last_distance_km", "number_of_observations",
            "observations_scored", "mean_absolute_error_min", "mean_signed_error_min",
            "largest_absolute_error_min", "within_5_minutes_percent",
        )),
        "By_Distance": pd.DataFrame(by_distance),
        "Arrivals": pd.DataFrame(arrivals, columns=(
            "track_id", "vehicle_id", "route", "first_seen", "last_seen", "arrival_time",
            "status", "first_distance_km", "last_distance_km", "number_of_observations",
        )),
        "Report_Table": pd.DataFrame(build_report_table(tracks), columns=(
            "No.", "Route", "Vehicle", "Observation time", "Distance (km)",
            "Predicted (min)", "Actual (min:s)", "Error (min:s)", "Within +/-5 min",
        )),
        "Raw_Log": pd.DataFrame(raw_log, columns=RAW_LOG_FIELDS),
        "Raw_Feed": pd.DataFrame(
            [{
                **row,
                "timestamp": excel_datetime(row["timestamp"]),
            } for row in raw_feed_rows],
            columns=RAW_FEED_FIELDS,
        ),
        "Events": pd.DataFrame(
            [{
                "timestamp": excel_datetime(event["timestamp"]),
                "event": event["event"],
            } for event in events],
            columns=("timestamp", "event"),
        ),
        "Assumptions": pd.DataFrame(assumptions, columns=("Setting", "Value")),
    }


def save_workbook(path, run_start, run_end, config, poll_count, failed_polls,
                  raw_poll_count, raw_rows, raw_feed_rows, tracks, events):
    sheets = prepare_workbook_sheets(
        run_start, run_end, config, poll_count, failed_polls, raw_poll_count,
        raw_rows, raw_feed_rows, tracks, events
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, dataframe in sheets.items():
            dataframe.to_excel(writer, sheet_name=sheet_name, index=False)

    from openpyxl import load_workbook

    workbook = load_workbook(path)
    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    for worksheet in workbook.worksheets:
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.fill = header_fill
            cell.font = header_font
        for column_cells in worksheet.columns:
            column_letter = column_cells[0].column_letter
            width = max(
                (len(str(cell.value)) for cell in column_cells if cell.value is not None),
                default=0,
            )
            worksheet.column_dimensions[column_letter].width = min(max(width + 2, 12), 55)
    workbook.save(path)


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def nonnegative_float(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than or equal to zero")
    return number


def latitude_value(value):
    number = float(value)
    if not math.isfinite(number) or not -90 <= number <= 90:
        raise argparse.ArgumentTypeError("latitude must be between -90 and 90")
    return number


def longitude_value(value):
    number = float(value)
    if not math.isfinite(number) or not -180 <= number <= 180:
        raise argparse.ArgumentTypeError("longitude must be between -180 and 180")
    return number


def parse_arguments():
    parser = argparse.ArgumentParser(description="Measure bus-arrival prediction accuracy.")
    parser.add_argument("--base-url", default="https://bus-time-arrival.onrender.com")
    parser.add_argument("--stop-id", default="1001512")
    parser.add_argument("--interval", type=positive_float, default=30)
    parser.add_argument("--hours", type=positive_float, default=2)
    parser.add_argument("--raw-interval", type=positive_float, default=10)
    parser.add_argument("--stop-lat", type=latitude_value, default=3.134)
    parser.add_argument("--stop-lon", type=longitude_value, default=101.6867)
    parser.add_argument("--arrival-radius-m", type=nonnegative_float, default=100)
    return parser.parse_args()


def make_track(track_id, vehicle_id, route, timestamp, distance_km):
    return {
        "track_id": track_id,
        "vehicle_id": vehicle_id,
        "route": route,
        "first_seen": timestamp,
        "last_seen": timestamp,
        "first_distance_km": distance_km,
        "last_distance_km": distance_km,
        "arrival_time": None,
        "status": None,
        "server_closed_at": None,
        "number_of_observations": 0,
        "missed_polls": 0,
        "observations": [],
    }


def get_stop_coordinates(base_url, stop_id, fallback_lat, fallback_lon, events):
    try:
        response = requests.get(
            "{}/debug/stop/{}".format(base_url.rstrip("/"), stop_id),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("location"), dict):
            raise ValueError("stop response does not contain a location object")
        location = payload["location"]
        latitude = parse_number(location.get("lat"))
        longitude = parse_number(location.get("lon"))
        if (
            latitude is None or longitude is None
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise ValueError("stop response contains invalid coordinates")
        return latitude, longitude
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        log_event(events, "Stop coordinate lookup failed; using command-line fallback: {}".format(error))
        return fallback_lat, fallback_lon


def poll_raw_feed(stop_lat, stop_lon, active_tracks, pending_tracks,
                  raw_feed_rows, poll_timestamp):
    """Fetch one GTFS Realtime vehicle-position feed and detect stop arrivals."""
    response = requests.get(RAW_FEED_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    positions_within_2km = 0
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle
        vehicle_id = vehicle.vehicle.id
        if not vehicle_id or not vehicle.HasField("position"):
            continue
        latitude = parse_number(vehicle.position.latitude)
        longitude = parse_number(vehicle.position.longitude)
        if (
            latitude is None or longitude is None
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            continue

        distance_m = haversine_m(stop_lat, stop_lon, latitude, longitude)
        if distance_m <= RAW_FEED_LOG_DISTANCE_KM * 1000:
            raw_feed_rows.append({
                "timestamp": poll_timestamp,
                "vehicle_id": vehicle_id,
                "latitude": latitude,
                "longitude": longitude,
                "distance_to_stop_km": distance_m / 1000,
            })
            positions_within_2km += 1

        track = active_tracks.get(vehicle_id)
        if track is None:
            candidates = [
                candidate for candidate in pending_tracks
                if candidate["vehicle_id"] == vehicle_id and candidate["status"] is None
            ]
            if candidates:
                track = max(candidates, key=lambda candidate: candidate["track_id"])
        if track is not None and track["status"] is None and distance_m <= track["arrival_radius_m"]:
            register_arrival(track, poll_timestamp)
            pending_tracks[:] = [
                candidate for candidate in pending_tracks
                if candidate["track_id"] != track["track_id"]
            ]
    return positions_within_2km


def run_logger(config):
    run_start = malaysia_now()
    file_stamp = run_start.strftime("%Y%m%d_%H%M")
    output_dir = Path(__file__).resolve().parent
    excel_path = output_dir / "accuracy_results_{}.xlsx".format(file_stamp)
    csv_path = output_dir / "accuracy_results_{}.csv".format(file_stamp)

    raw_rows = []
    raw_feed_rows = []
    tracks = []
    active_tracks = {}
    pending_tracks = []
    events = []
    poll_count = 0
    failed_polls = 0
    raw_poll_count = 0
    duration_seconds = config.hours * 60 * 60
    base_url = config.base_url.rstrip("/")
    run_started_monotonic = None
    run_deadline = None
    last_save = None
    stop_lat, stop_lon = config.stop_lat, config.stop_lon
    csv_file = None
    interrupted = False

    try:
        stop_lat, stop_lon = get_stop_coordinates(
            base_url, config.stop_id, config.stop_lat, config.stop_lon, events
        )
        config.stop_lat = stop_lat
        config.stop_lon = stop_lon
        run_started_monotonic = time.monotonic()
        run_deadline = run_started_monotonic + duration_seconds
        last_save = run_started_monotonic
        csv_exists = csv_path.exists() and csv_path.stat().st_size > 0
        csv_file = csv_path.open("a", newline="", encoding="utf-8")
        csv_writer = csv.DictWriter(csv_file, fieldnames=RAW_FIELDS)
        if not csv_exists:
            csv_writer.writeheader()
            csv_file.flush()

        print("Writing Excel results to {}".format(excel_path))
        next_bus_poll = run_started_monotonic
        next_raw_poll = run_started_monotonic
        finalized = False
        while True:
            now = time.monotonic()
            if now >= run_deadline and not finalized:
                close_active_tracks(active_tracks, pending_tracks, malaysia_now())
                finalized = True
            if finalized and not pending_tracks:
                break

            if not finalized and now >= next_bus_poll:
                poll_started = time.monotonic()
                poll_timestamp = malaysia_now()
                poll_count += 1
                buses = None
                stats = {}
                try:
                    response = requests.get(
                        "{}/bus/{}".format(base_url, config.stop_id),
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("buses"), list):
                        raise ValueError("response JSON must contain a buses list")
                    buses = payload["buses"]
                except (requests.RequestException, ValueError) as error:
                    failed_polls += 1
                    log_event(events, "Poll {} failed: {}".format(poll_count, error), poll_timestamp)

                if buses is not None:
                    try:
                        stats_response = requests.get(
                            "{}/stats".format(base_url),
                            timeout=REQUEST_TIMEOUT_SECONDS,
                        )
                        stats_response.raise_for_status()
                        stats_payload = stats_response.json()
                        if isinstance(stats_payload, dict):
                            stats = {
                                "websocket_connected": stats_payload.get("websocket_connected"),
                                "gtfs_buses_live": stats_payload.get("gtfs_buses_live"),
                            }
                    except (requests.RequestException, ValueError):
                        pass

                    seen_vehicle_ids = set()
                    for bus in buses:
                        if not isinstance(bus, dict) or bus.get("vehicle_id") is None:
                            log_event(events, "Poll {} contained a bus without a vehicle_id.".format(poll_count), poll_timestamp)
                            continue

                        vehicle_id = str(bus["vehicle_id"])
                        route = bus.get("route")
                        distance = parse_number(bus.get("distance_km"))
                        observation = {
                            "timestamp": poll_timestamp,
                            "vehicle_id": vehicle_id,
                            "route": route,
                            "predicted_minutes": parse_number(bus.get("arrival_minutes")),
                            "distance_km": distance,
                            "latitude": parse_number(bus.get("latitude")),
                            "longitude": parse_number(bus.get("longitude")),
                            "source": bus.get("source"),
                            "websocket_connected": stats.get("websocket_connected"),
                            "gtfs_buses_live": stats.get("gtfs_buses_live"),
                            "actual_remaining_min": None,
                            "error_min": None,
                            "abs_error_min": None,
                            "within_5_min": None,
                            "distance_bracket": None,
                        }
                        raw_rows.append(observation)
                        try:
                            csv_writer.writerow({key: observation[key] for key in RAW_FIELDS})
                            csv_file.flush()
                        except (OSError, csv.Error) as error:
                            log_event(events, "CSV backup write failed: {}".format(error), poll_timestamp)

                        track = active_tracks.get(vehicle_id)
                        if track is None:
                            track = make_track(len(tracks) + 1, vehicle_id, route, poll_timestamp, distance)
                            track["arrival_radius_m"] = config.arrival_radius_m
                            tracks.append(track)
                        elif route is not None:
                            track["route"] = route
                        active_tracks[vehicle_id] = track
                        track["server_closed_at"] = None
                        track["last_seen"] = poll_timestamp
                        track["last_distance_km"] = distance
                        track["missed_polls"] = 0
                        track["observations"].append(observation)
                        track["number_of_observations"] = len(track["observations"])
                        if track["arrival_time"] is not None:
                            score_observation(observation, track["arrival_time"])
                        seen_vehicle_ids.add(vehicle_id)

                    tracks_to_close = []
                    for vehicle_id, track in active_tracks.items():
                        if vehicle_id not in seen_vehicle_ids:
                            track["missed_polls"] += 1
                            if track["missed_polls"] >= MISSING_POLLS_TO_CLOSE:
                                tracks_to_close.append(vehicle_id)
                    for vehicle_id in tracks_to_close:
                        track = active_tracks.pop(vehicle_id)
                        if track["arrival_time"] is None:
                            track["server_closed_at"] = poll_timestamp
                            pending_tracks.append(track)

                next_bus_poll = poll_started + config.interval
                print(
                    "[{}] Server poll {}: {} buses, {} open tracks; raw polls {}, arrivals {}.".format(
                        poll_timestamp.strftime("%H:%M:%S"),
                        poll_count,
                        len(buses) if buses is not None else 0,
                        len(active_tracks) + len(pending_tracks),
                        raw_poll_count,
                        sum(1 for track in tracks if track["status"] == "Arrived"),
                    ),
                    flush=True,
                )

            now = time.monotonic()
            if now >= next_raw_poll:
                raw_poll_timestamp = malaysia_now()
                raw_poll_count += 1
                try:
                    near_stop_count = poll_raw_feed(
                        stop_lat, stop_lon, active_tracks, pending_tracks,
                        raw_feed_rows, raw_poll_timestamp,
                    )
                except (requests.RequestException, DecodeError, ValueError) as error:
                    log_event(
                        events,
                        "Raw feed poll {} failed: {}".format(raw_poll_count, error),
                        raw_poll_timestamp,
                    )
                    near_stop_count = 0

                for track in list(pending_tracks):
                    if (
                        raw_poll_timestamp - track["server_closed_at"]
                    ).total_seconds() >= LOST_ARRIVAL_GRACE_SECONDS:
                        close_track(track)
                        pending_tracks.remove(track)

                next_raw_poll = time.monotonic() + config.raw_interval
                print(
                    "[{}] Raw poll {}: {} positions within 2 km, {} arrivals detected "
                    "so far.".format(
                        raw_poll_timestamp.strftime("%H:%M:%S"),
                        raw_poll_count,
                        near_stop_count,
                        sum(1 for track in tracks if track["status"] == "Arrived"),
                    ),
                    flush=True,
                )

            now = time.monotonic()
            if now - last_save >= SAVE_INTERVAL_SECONDS:
                try:
                    save_workbook(
                        excel_path, run_start, malaysia_now(), config,
                        poll_count, failed_polls, raw_poll_count,
                        raw_rows, raw_feed_rows, tracks, events,
                    )
                    last_save = time.monotonic()
                except (OSError, PermissionError, ValueError) as error:
                    log_event(events, "Excel save failed: {}".format(error))
                    print("Excel save failed: {}".format(error))
                    last_save = time.monotonic()

            now = time.monotonic()
            next_due = min(next_raw_poll, now + 1)
            if not finalized:
                next_due = min(next_due, next_bus_poll, run_deadline)
            until_save = SAVE_INTERVAL_SECONDS - (now - last_save)
            time.sleep(max(0, min(next_due - now, until_save)))
    except KeyboardInterrupt:
        print("\nCtrl+C received; closing tracks and saving collected data.")
        interrupted = True
    except OSError as error:
        log_event(events, "Output file error: {}".format(error))
        print("Output file error: {}".format(error))
    finally:
        if csv_file is not None:
            csv_file.close()

    close_active_tracks(active_tracks, pending_tracks, malaysia_now())
    if interrupted:
        for track in pending_tracks:
            log_event(
                events,
                "Track {} marked Lost because Ctrl+C ended its raw-arrival grace period.".format(
                    track["vehicle_id"]
                ),
            )
            close_track(track)
        pending_tracks.clear()

    run_end = malaysia_now()
    try:
        save_workbook(
            excel_path, run_start, run_end, config,
            poll_count, failed_polls, raw_poll_count,
            raw_rows, raw_feed_rows, tracks, events,
        )
    except (OSError, PermissionError, ValueError) as error:
        log_event(events, "Final Excel save failed: {}".format(error), run_end)
        print("Final Excel save failed: {}".format(error))

    arrived_count = sum(1 for track in tracks if track["status"] == "Arrived")
    lost_count = sum(1 for track in tracks if track["status"] == "Lost")
    accuracy = metric_values([
        observation
        for track in tracks if track["status"] == "Arrived"
        for observation in track["observations"]
        if observation["error_min"] is not None
    ])
    print(
        "Finished: {} server polls ({} failed), {} raw polls, {} tracks "
        "({} arrived, {} lost), {} observations scored.".format(
            poll_count, failed_polls, raw_poll_count, len(tracks), arrived_count, lost_count,
            accuracy["observations"],
        )
    )
    if accuracy["mean_absolute_error"] is not None:
        print(
            "Mean absolute error: {:.2f} min; mean signed error: {:.2f} min; "
            "within +/-5 min: {:.1f}%.".format(
                accuracy["mean_absolute_error"],
                accuracy["mean_signed_error"],
                accuracy["within_5_minutes_percent"],
            )
        )
    print("Excel: {}".format(excel_path))
    print("CSV backup: {}".format(csv_path))


def close_active_tracks(active_tracks, pending_tracks, timestamp):
    """Start the raw-feed grace period for tracks still active at a run boundary."""
    for vehicle_id, track in list(active_tracks.items()):
        del active_tracks[vehicle_id]
        if track["arrival_time"] is None:
            track["server_closed_at"] = timestamp
            pending_tracks.append(track)


def main():
    config = parse_arguments()
    config.base_url = config.base_url.rstrip("/")
    run_logger(config)


if __name__ == "__main__":
    main()
