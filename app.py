import csv
import requests
import zipfile
import io
import time
import threading
from flask import Flask, jsonify, render_template_string
from google.transit import gtfs_realtime_pb2
from datetime import datetime
import pytz
import math
import base64
import json
import websocket
import gzip
import os

app = Flask(__name__)

# ============================================
# GLOBAL DATA STORAGE
# ============================================
SHAPES = {}
STOPS = {}
ROUTES = {}
TRIPS = {}
STOP_TIMES = {}
SHAPE_CUMULATIVE_DIST = {}  # shape_id -> [0, d1, d1+d2, ...] cumulative distances per vertex
BUS_CACHE = {}          # vehicle_id -> {last_seen, data, visible}
KIOSK_CACHE = {}        # vehicle_id -> {last_seen, data}
CACHE_TIMEOUT = 180
KIOSK_TIMEOUT = 300

# Malaysia timezone
MYT = pytz.timezone('Asia/Kuala_Lumpur')

# Prasarana WebSocket
WS_URL = "wss://rapidbus-socketio-avl.prasarana.com.my/socket.io/?EIO=3&transport=websocket"
POLLING_URL = "https://rapidbus-socketio-avl.prasarana.com.my/socket.io/?EIO=3&transport=polling"

# Routes to track via WebSocket (route_name -> provider)
TRACKED_ROUTES = {
    "T4508": "RKL",   # T450
}

# WebSocket state
ws_thread = None
ws_connected = False
ws_last_message = None

# API configuration
# Possible CATEGORY values: rapid-bus-penang, rapid-bus-kuantan, rapid-bus-mrtfeeder, rapid-bus-kl
CATEGORY = "rapid-bus-kl"  # change this value for debugging other categories
SAFE_CAT_DIR = CATEGORY.replace('-', '_')
GTFS_STATIC_URL = f"https://api.data.gov.my/gtfs-static/prasarana?category={CATEGORY}"
GTFS_REALTIME_URL = f"https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana?category={CATEGORY}"

# ============================================
# GTFS STATIC HELPERS
# ============================================

def find_route_by_name(route_name):
    if route_name in ROUTES:
        return route_name
    clean_name = route_name.replace(" Inbound", "").replace(" Outbound", "").strip()
    for route_id, route_info in ROUTES.items():
        if route_info.get('short_name') == clean_name:
            return route_id
        if route_info.get('long_name') == clean_name:
            return route_id
    return None

def match_trip_id(realtime_trip_id, static_trip_id):
    if realtime_trip_id == static_trip_id:
        return True
    if static_trip_id.endswith(realtime_trip_id):
        return True
    rt_parts = realtime_trip_id.split('_')
    st_parts = static_trip_id.split('_')
    if len(rt_parts) >= 3 and len(st_parts) >= 3:
        if rt_parts[-3:] == st_parts[-3:]:
            return True
    return False

def parse_gtfs_time(value):
    if not value:
        return 0
    parts = value.split(':')
    if len(parts) < 2:
        return 0
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2]) if len(parts) > 2 else 0
    return hours * 3600 + minutes * 60 + seconds

def find_matching_trip(realtime_trip_id):
    if realtime_trip_id in TRIPS:
        return realtime_trip_id
    for static_trip_id in TRIPS.keys():
        if match_trip_id(realtime_trip_id, static_trip_id):
            return static_trip_id
    return None

def download_and_load_gtfs():
    print("=" * 60)
    print("LOADING GTFS STATIC DATA")
    print("=" * 60)
    try:
        print(f"Downloading from: {GTFS_STATIC_URL}")
        response = requests.get(GTFS_STATIC_URL, timeout=30)
        response.raise_for_status()
        print(f"Downloaded {len(response.content):,} bytes")

        zip_file = zipfile.ZipFile(io.BytesIO(response.content))
        STOPS.clear(); SHAPES.clear(); ROUTES.clear(); TRIPS.clear(); STOP_TIMES.clear(); SHAPE_CUMULATIVE_DIST.clear()

        with zip_file.open('stops.txt') as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
            for row in reader:
                STOPS[row['stop_id']] = {
                    'name': row['stop_name'],
                    'lat': float(row['stop_lat']),
                    'lon': float(row['stop_lon'])
                }
        print(f"✓ Loaded {len(STOPS):,} stops")
        # Create category-based directories for debugging/inspection
        try:
            base_dir = os.path.join(os.getcwd(), SAFE_CAT_DIR, 'bus')
            os.makedirs(base_dir, exist_ok=True)
            for stop_id in STOPS.keys():
                os.makedirs(os.path.join(base_dir, stop_id), exist_ok=True)
            print(f"✓ Created local directories under {base_dir} for {len(STOPS):,} stops")
        except Exception as e:
            print(f"✗ ERROR creating category directories: {e}")

        with zip_file.open('routes.txt') as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
            for row in reader:
                ROUTES[row['route_id']] = {
                    'short_name': row['route_short_name'],
                    'long_name': row['route_long_name'],
                    'shapes': set()
                }
        print(f"✓ Loaded {len(ROUTES):,} routes")

        with zip_file.open('shapes.txt') as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
            for row in reader:
                shape_id = row['shape_id']
                if shape_id not in SHAPES:
                    SHAPES[shape_id] = []
                SHAPES[shape_id].append((
                    float(row['shape_pt_lat']),
                    float(row['shape_pt_lon']),
                    int(row['shape_pt_sequence'])
                ))
        for shape_id in SHAPES:
            SHAPES[shape_id].sort(key=lambda p: p[2])
        print(f"✓ Loaded {len(SHAPES):,} shapes")
        
        # Precompute cumulative distances for each shape to avoid repeated calculations
        for shape_id, shape_points in SHAPES.items():
            cumulative = [0.0]  # Start with 0 at first point
            for i in range(len(shape_points) - 1):
                lat1, lon1, _ = shape_points[i]
                lat2, lon2, _ = shape_points[i + 1]
                segment_dist = haversine_distance(lat1, lon1, lat2, lon2)
                cumulative.append(cumulative[-1] + segment_dist)
            SHAPE_CUMULATIVE_DIST[shape_id] = cumulative
        print(f"✓ Precomputed cumulative distances for {len(SHAPE_CUMULATIVE_DIST):,} shapes")


        with zip_file.open('trips.txt') as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
            for row in reader:
                TRIPS[row['trip_id']] = {
                    'route_id': row['route_id'],
                    'shape_id': row['shape_id'],
                    'stops_served': set()
                }
                if row['route_id'] in ROUTES:
                    ROUTES[row['route_id']]['shapes'].add(row['shape_id'])
        print(f"✓ Loaded {len(TRIPS):,} trips")

        count = 0
        with zip_file.open('stop_times.txt') as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
            for row in reader:
                if row['trip_id'] in TRIPS:
                    trip_data = TRIPS[row['trip_id']]
                    trip_data['stops_served'].add(row['stop_id'])
                    route_id = trip_data.get('route_id')
                    route_info = ROUTES.get(route_id, {})
                    stop_time_entry = {
                        'trip_id': row['trip_id'],
                        'arrival_time': row['arrival_time'],
                        'departure_time': row['departure_time'],
                        'stop_id': row['stop_id'],
                        'stop_sequence': int(row['stop_sequence']),
                        'stop_headsign': row.get('stop_headsign', ''),
                        'route_id': route_id,
                        'route_short_name': route_info.get('short_name', route_id),
                        'route_long_name': route_info.get('long_name', '')
                    }
                    STOP_TIMES.setdefault(row['stop_id'], []).append(stop_time_entry)
                count += 1

        for stop_id in STOP_TIMES:
            STOP_TIMES[stop_id].sort(key=lambda item: (parse_gtfs_time(item['arrival_time']), item['stop_sequence'], item['trip_id']))
        print(f"✓ Loaded {count:,} stop times")
        print("=" * 60)
        print("✓ GTFS DATA LOADED")
        print("=" * 60)
        return True
    except Exception as e:
        print(f"✗ ERROR loading GTFS: {e}")
        import traceback; traceback.print_exc()
        return False

# ============================================
# GEOMETRY HELPERS
# ============================================

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat, dlon = lat2_r - lat1_r, lon2_r - lon1_r
    a = math.sin(dlat/2)**2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def project_point_to_segment(lat, lon, seg_lat1, seg_lon1, seg_lat2, seg_lon2):
    """Project a point onto a line segment and return the perpendicular distance.
    
    Uses vector projection on the great-circle arc. Returns distance to the 
    closest point on the segment (which could be an endpoint if the projection 
    falls outside the segment).
    """
    # For short distances (local bus routes), use simplified planar projection
    # Convert to radians
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    seg_lat1_r = math.radians(seg_lat1)
    seg_lon1_r = math.radians(seg_lon1)
    seg_lat2_r = math.radians(seg_lat2)
    seg_lon2_r = math.radians(seg_lon2)
    
    # Use equirectangular approximation for local distances
    # This is accurate enough for city-scale routes
    x = (lon_r - seg_lon1_r) * math.cos(seg_lat1_r)
    y = lat_r - seg_lat1_r
    dx = (seg_lon2_r - seg_lon1_r) * math.cos(seg_lat1_r)
    dy = seg_lat2_r - seg_lat1_r
    
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        # Segment is a point
        return haversine_distance(lat, lon, seg_lat1, seg_lon1)
    
    t = max(0, min(1, (x * dx + y * dy) / seg_len_sq))
    
    # Project point onto segment
    proj_lat = seg_lat1 + t * (seg_lat2 - seg_lat1)
    proj_lon = seg_lon1 + t * (seg_lon2 - seg_lon1)
    
    return haversine_distance(lat, lon, proj_lat, proj_lon)


def find_closest_point_on_shape(lat, lon, shape_points):
    """Find closest point on shape, snapping to both vertices and segments.
    
    Returns (closest_idx, min_dist) where:
    - closest_idx: index of the nearest vertex
    - min_dist: minimum distance to the shape (could be to a vertex or segment projection)
    """
    min_dist = float('inf')
    closest_idx = 0
    
    # Find closest vertex
    for i, (slat, slon, seq) in enumerate(shape_points):
        dist = haversine_distance(lat, lon, slat, slon)
        if dist < min_dist:
            min_dist = dist
            closest_idx = i
    
    # Also check perpendicular projections onto segments adjacent to closest vertex
    # Check segment before (closest_idx-1 to closest_idx)
    if closest_idx > 0:
        seg_lat1, seg_lon1, _ = shape_points[closest_idx - 1]
        seg_lat2, seg_lon2, _ = shape_points[closest_idx]
        proj_dist = project_point_to_segment(lat, lon, seg_lat1, seg_lon1, seg_lat2, seg_lon2)
        if proj_dist < min_dist:
            min_dist = proj_dist
            # Keep closest_idx pointing to the vertex end of the segment
    
    # Check segment after (closest_idx to closest_idx+1)
    if closest_idx < len(shape_points) - 1:
        seg_lat1, seg_lon1, _ = shape_points[closest_idx]
        seg_lat2, seg_lon2, _ = shape_points[closest_idx + 1]
        proj_dist = project_point_to_segment(lat, lon, seg_lat1, seg_lon1, seg_lat2, seg_lon2)
        if proj_dist < min_dist:
            min_dist = proj_dist
            # Keep closest_idx pointing to the vertex start of the segment
    
    return closest_idx, min_dist

def calculate_shape_distance(shape_points, start, end, shape_id=None):
    """Calculate distance along a shape between two indices.
    
    Uses precomputed cumulative distances if shape_id is provided for O(1) lookup.
    Falls back to linear scan if cumulative distances unavailable.
    """
    if start >= end:
        return 0
    
    # Use precomputed cumulative distances if available (O(1) operation)
    if shape_id and shape_id in SHAPE_CUMULATIVE_DIST:
        cumulative = SHAPE_CUMULATIVE_DIST[shape_id]
        if start < len(cumulative) and end <= len(cumulative):
            return cumulative[end] - cumulative[start]
    
    # Fallback: calculate on-the-fly (O(n) operation)
    total = 0
    for i in range(start, end):
        lat1, lon1, _ = shape_points[i]
        lat2, lon2, _ = shape_points[i+1]
        total += haversine_distance(lat1, lon1, lat2, lon2)
    return total

def get_static_schedule_for_stop(stop_id, limit=100):
    entries = STOP_TIMES.get(stop_id, [])
    if not entries:
        return []

    now = datetime.now(MYT)
    current_seconds = now.hour * 3600 + now.minute * 60 + now.second
    upcoming = []

    for entry in entries:
        arrival_seconds = parse_gtfs_time(entry['arrival_time'])
        if arrival_seconds >= current_seconds:
            upcoming.append(entry)

    if not upcoming:
        for entry in entries:
            upcoming.append(entry)

    upcoming.sort(key=lambda item: (parse_gtfs_time(item['arrival_time']), item['stop_sequence'], item['trip_id']))
    if not upcoming:
        return []

    results = []
    for entry in upcoming[:limit]:
        arrival_seconds = parse_gtfs_time(entry['arrival_time'])
        if arrival_seconds < current_seconds:
            arrival_seconds += 86400
        minutes_until = max(1, int(round((arrival_seconds - current_seconds) / 60.0)))
        results.append({
            'trip_id': entry['trip_id'],
            'route_id': entry['route_id'],
            'route': entry['route_short_name'] or entry['route_id'],
            'arrival_time': entry['arrival_time'],
            'departure_time': entry['departure_time'],
            'stop_id': entry['stop_id'],
            'stop_sequence': entry['stop_sequence'],
            'stop_headsign': entry['stop_headsign'],
            'arrival_minutes': minutes_until,
            'distance_km': None
        })

    return results


def calculate_arrival_time_smart(distance_km, current_speed_mps):
    current_speed_kmh = current_speed_mps * 3.6 if current_speed_mps else 0

    if distance_km < 1.0:
        base_speed = 12
    elif distance_km < 3.0:
        base_speed = 18
    elif distance_km < 7.0:
        base_speed = 25
    else:
        base_speed = 30

    if current_speed_kmh > 15:
        estimated_speed = current_speed_kmh
    elif 0 < current_speed_kmh < 8:
        estimated_speed = (current_speed_kmh * 0.3) + (base_speed * 0.7)
    else:
        estimated_speed = base_speed

    estimated_speed = max(estimated_speed, 10)
    time_minutes = (distance_km / estimated_speed) * 60

    if distance_km < 0.5:
        min_minutes, max_minutes = 1, 3
    elif distance_km < 1.0:
        min_minutes, max_minutes = 2, 5
    elif distance_km < 2.0:
        min_minutes, max_minutes = 4, 10
    elif distance_km < 3.0:
        min_minutes, max_minutes = 6, 12
    elif distance_km < 5.0:
        min_minutes, max_minutes = 10, 20
    elif distance_km < 7.0:
        min_minutes, max_minutes = 15, 28
    elif distance_km < 10.0:
        min_minutes, max_minutes = 20, 38
    else:
        min_minutes, max_minutes = 28, 50

    final_minutes = max(min_minutes, min(round(time_minutes), max_minutes))
    return final_minutes, round(estimated_speed, 1), round(current_speed_kmh, 1)

def calculate_bearing(lat1, lon1, lat2, lon2):
    """Calculate bearing in degrees from point 1 to point 2"""
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360

def is_bus_approaching(bus_lat, bus_lon, bus_idx, shape, stop_idx):
    """Check if bus is moving toward the stop using next shape point"""
    # Use next shape point to determine bus direction of travel
    if bus_idx + 1 < len(shape):
        next_lat, next_lon, _ = shape[bus_idx + 1]
    elif bus_idx > 0:
        next_lat, next_lon, _ = shape[bus_idx]
        prev_lat, prev_lon, _ = shape[bus_idx - 1]
        # Extrapolate direction
        next_lat = bus_lat + (bus_lat - prev_lat)
        next_lon = bus_lon + (bus_lon - prev_lon)
    else:
        return True  # Can't determine, assume approaching

    stop_lat, stop_lon, _ = shape[stop_idx]

    # Bearing from bus to next shape point (direction of travel)
    travel_bearing = calculate_bearing(bus_lat, bus_lon, next_lat, next_lon)

    # Bearing from bus to stop
    stop_bearing = calculate_bearing(bus_lat, bus_lon, stop_lat, stop_lon)

    # Angle difference between travel direction and stop direction
    diff = abs(travel_bearing - stop_bearing)
    if diff > 180:
        diff = 360 - diff

    # If bus is moving within 90 degrees toward the stop, it's approaching
    return diff < 90

# ============================================
# GTFS REALTIME (existing buses, non-T450)
# ============================================

def fetch_live_buses():
    try:
        response = requests.get(GTFS_REALTIME_URL, timeout=10, allow_redirects=True)
        response.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)

        current_time = time.time()
        active_vehicle_ids = set()
        buses = []

        for entity in feed.entity:
            if entity.HasField('vehicle'):
                v = entity.vehicle
                vehicle_id = v.vehicle.id if v.HasField('vehicle') else 'unknown'
                active_vehicle_ids.add(vehicle_id)
                speed = 0
                try:
                    speed = v.position.speed
                except:
                    speed = 0

                bus_data = {
                    'vehicle_id': vehicle_id,
                    'trip_id': v.trip.trip_id if v.HasField('trip') else None,
                    'route_id': v.trip.route_id if v.HasField('trip') else 'unknown',
                    'latitude': v.position.latitude if v.HasField('position') else 0,
                    'longitude': v.position.longitude if v.HasField('position') else 0,
                    'speed': speed,
                    'timestamp': current_time,
                    'source': 'gtfs'
                }

                BUS_CACHE[vehicle_id] = {
                    'last_seen': current_time,
                    'data': bus_data,
                    'visible': True
                }
                buses.append(bus_data)

        for vehicle_id in BUS_CACHE:
            if vehicle_id not in active_vehicle_ids:
                age = current_time - BUS_CACHE[vehicle_id]['last_seen']
                if age > CACHE_TIMEOUT:
                    BUS_CACHE[vehicle_id]['visible'] = False

        return buses
    except Exception as e:
        print(f"✗ GTFS fetch error: {e}")
        return []

def get_all_buses_with_cache():
    current_time = time.time()
    all_buses = []

    # GTFS buses
    for vehicle_id, cache_entry in BUS_CACHE.items():
        if not cache_entry['visible']:
            continue
        bus_data = cache_entry['data'].copy()
        age = current_time - cache_entry['last_seen']
        bus_data['cache_age_seconds'] = int(age)
        bus_data['status'] = 'live' if age < 30 else 'cached'
        all_buses.append(bus_data)

    # Kiosk WebSocket buses
    for vehicle_id, cache_entry in KIOSK_CACHE.items():
        age = current_time - cache_entry['last_seen']
        if age > KIOSK_TIMEOUT:
            continue
        bus_data = cache_entry['data'].copy()
        bus_data['cache_age_seconds'] = int(age)
        bus_data['status'] = 'live' if age < 30 else 'cached'
        all_buses.append(bus_data)

    return all_buses

# ============================================
# PRASARANA WEBSOCKET (Socket.IO v2)
# ============================================

def get_fresh_sid():
    """Do HTTP polling handshake to get a fresh session ID"""
    try:
        r = requests.get(POLLING_URL, timeout=10)
        raw = r.text
        json_start = raw.find('{')
        if json_start == -1:
            print("✗ No JSON in handshake")
            return None
        handshake = json.loads(raw[json_start:raw.rfind('}')+1])
        sid = handshake.get('sid')
        print(f"✓ Fresh sid: {sid}")
        return sid
    except Exception as e:
        print(f"✗ Handshake error: {e}")
        return None

def decode_kiosk_payload(raw_data):
    try:
        decoded = base64.b64decode(raw_data)
        decompressed = gzip.decompress(decoded)
        buses = json.loads(decompressed.decode('utf-8'))
        print(f"[WS] Decoded {len(buses)} buses")
        return buses
    except Exception as e:
        print(f"[WS] Decode error: {e}")
        return []

def process_kiosk_buses(buses):
    """Store decoded kiosk buses into KIOSK_CACHE"""
    current_time = time.time()
    count = 0
    for bus in buses:
        vehicle_id = bus.get('bus_no', 'unknown')
        route_name = bus.get('route', 'unknown')

        # Map kiosk route name back to GTFS route_id
        # Prasarana kiosk naming convention: appends '8' to route names (e.g., T4508 -> T450)
        # This is a heuristic based on observed data, not a documented specification.
        # Remove trailing '8' if present, as it appears to be added by the kiosk system.
        short_name = route_name[:-1] if route_name.endswith('8') else route_name
        # Try to find by short_name
        route_id = None
        for rid, rinfo in ROUTES.items():
            if rinfo.get('short_name') == short_name or rinfo.get('short_name') == route_name:
                route_id = rid
                break
        if not route_id:
            route_id = route_name  # fallback

        bus_data = {
            'vehicle_id': vehicle_id,
            'trip_id': bus.get('trip_no', None),
            'route_id': route_id,
            'route_kiosk_name': route_name,
            'latitude': bus.get('latitude', 0),
            'longitude': bus.get('longitude', 0),
            'speed': bus.get('speed', 0) / 3.6,  # km/h to m/s
            'timestamp': current_time,
            'source': 'kiosk_ws'
        }

        KIOSK_CACHE[vehicle_id] = {
            'last_seen': current_time,
            'data': bus_data
        }
        count += 1

    if count > 0:
        print(f"✓ Kiosk WS: updated {count} buses")

def build_subscribe_payload(sid, route, provider):
    """Build Socket.IO v2 emit payload for onFts-reload"""
    data = {"sid": sid, "uid": "", "provider": provider, "route": route}
    msg = f'42["onFts-reload",{json.dumps(data)}]'
    return msg

def run_websocket():
    global ws_connected, ws_last_message
    print("[WS] Thread running...")

    while True:
        try:
            print("[WS] Polling cycle start...")
            
            # Step 1: Get sid
            r = requests.get(POLLING_URL, timeout=15)
            raw = r.text
            json_start = raw.find('{')
            if json_start == -1:
                print("[WS] No handshake JSON")
                time.sleep(30)
                continue
                
            handshake = json.loads(raw[json_start:raw.rfind('}')+1])
            sid = handshake.get('sid')
            if not sid:
                print("[WS] No sid")
                time.sleep(30)
                continue
            
            print(f"[WS] Got sid: {sid[:8]}...")
            ws_url = f"wss://rapidbus-socketio-avl.prasarana.com.my/socket.io/?EIO=3&transport=websocket&sid={sid}"

            result = {"done": False, "buses": []}

            def on_open(ws):
                ws_connected = True
                ws.send("2probe")

            def on_message(ws, message):
                global ws_last_message
                ws_last_message = time.time()

                if message == "3probe":
                    ws.send("5")
                    for route, provider in TRACKED_ROUTES.items():
                        ws.send(build_subscribe_payload(sid, route, provider))
                    return
                if message == "2":
                    ws.send("3")
                    return
                if message in ("6", "40"):
                    return
                if message == "41":
                    ws.close()
                    return
                if "onFts-client" in message:
                    try:
                        arr = json.loads(message[message.find('['):])
                        if len(arr) >= 2:
                            result["buses"] = decode_kiosk_payload(arr[1])
                            result["done"] = True
                            ws.close()
                    except Exception as e:
                        print(f"[WS] Parse error: {e}")

            def on_error(ws, error):
                global ws_connected
                ws_connected = False

            def on_close(ws, c, m):
                global ws_connected
                ws_connected = False

            ws_app = websocket.WebSocketApp(ws_url,
                on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close)
            ws_app.run_forever()

            if result["buses"]:
                process_kiosk_buses(result["buses"])
                print(f"[WS] Cycle complete: {len(result['buses'])} buses cached")
            else:
                print("[WS] Cycle complete: no buses received")

        except Exception as e:
            print(f"[WS] Cycle error: {e}")
            import traceback; traceback.print_exc()

        print("[WS] Sleeping 30s...")
        time.sleep(30)
        print("[WS] Woke up, next cycle")

def start_websocket_thread():
    """Start WebSocket in background thread"""
    global ws_thread
    ws_thread = threading.Thread(target=run_websocket, daemon=True)
    ws_thread.start()
    print("✓ WebSocket thread started")

# ============================================
# ARRIVAL CALCULATION (shared logic)
# ============================================

def calculate_approaching_buses(stop_id):
    """Calculate approaching buses for a stop from all sources"""
    stop_info = STOPS[stop_id]
    fetch_live_buses()
    all_buses = get_all_buses_with_cache()

    approaching = []
    seen_vehicles = set()

    for bus in all_buses:
        vehicle_id = bus['vehicle_id']
        if vehicle_id in seen_vehicles:
            continue

        if not bus['latitude'] or not bus['longitude']:
            continue

        # Find static trip
        static_trip = None
        route_id_to_use = None

        if bus.get('trip_id'):
            static_trip = find_matching_trip(bus['trip_id'])
            if static_trip and static_trip in TRIPS:
                route_id_to_use = TRIPS[static_trip]['route_id']

        if not static_trip:
            realtime_route_id = bus.get('route_id', '')
            matched_route_id = find_route_by_name(realtime_route_id)
            if matched_route_id:
                for trip_id, trip_data in TRIPS.items():
                    if trip_data['route_id'] == matched_route_id and stop_id in trip_data['stops_served']:
                        static_trip = trip_id
                        route_id_to_use = matched_route_id
                        break

        if not static_trip or static_trip not in TRIPS:
            continue

        trip_info = TRIPS[static_trip]
        if stop_id not in trip_info['stops_served']:
            continue
        if trip_info['shape_id'] not in SHAPES:
            continue

        shape = SHAPES[trip_info['shape_id']]
        bus_idx, bus_dist = find_closest_point_on_shape(bus['latitude'], bus['longitude'], shape)
        stop_idx, stop_dist = find_closest_point_on_shape(stop_info['lat'], stop_info['lon'], shape)

        if bus_idx < stop_idx and is_bus_approaching(bus['latitude'], bus['longitude'], bus_idx, shape, stop_idx):
            road_dist = calculate_shape_distance(shape, bus_idx, stop_idx, trip_info['shape_id'])
            total_dist = road_dist + bus_dist + stop_dist

            if total_dist < 20.0:
                arrival_min, estimated_speed, current_speed = calculate_arrival_time_smart(total_dist, bus['speed'])

                if arrival_min <= 90:
                    route_short_name = ROUTES.get(trip_info['route_id'], {}).get('short_name', bus.get('route_id', 'Unknown'))
                    seen_vehicles.add(vehicle_id)

                    route_path = []
                    for point in shape[bus_idx:stop_idx + 1]:
                        route_path.append([point[0], point[1]])
                    if not route_path:
                        route_path = [[bus['latitude'], bus['longitude']], [stop_info['lat'], stop_info['lon']]]
                    elif route_path[0] != [bus['latitude'], bus['longitude']]:
                        route_path.insert(0, [bus['latitude'], bus['longitude']])
                    if route_path[-1] != [stop_info['lat'], stop_info['lon']]:
                        route_path.append([stop_info['lat'], stop_info['lon']])

                    approaching.append({
                        'route': route_short_name,
                        'arrival_minutes': arrival_min,
                        'distance_km': round(total_dist, 2),
                        'vehicle_id': vehicle_id,
                        'latitude': bus['latitude'],
                        'longitude': bus['longitude'],
                        'route_path': route_path,
                        'current_speed_kmh': current_speed,
                        'estimated_speed_kmh': estimated_speed,
                        'status': bus.get('status', 'live'),
                        'last_update_seconds': bus.get('cache_age_seconds', 0),
                        'source': bus.get('source', 'gtfs')
                    })

    approaching.sort(key=lambda x: x['arrival_minutes'])
    return approaching

# ============================================
# MAIN ROUTES
# ============================================

@app.route('/')
def hello():
    global ws_thread
    if ws_thread is None or not ws_thread.is_alive():
        print("[WS] Starting thread from request trigger...")
        start_websocket_thread()
    return (
        f"Bus Arrival Server | "
        f"{len(ROUTES):,} routes | {len(STOPS):,} stops | "
        f"{len(TRIPS):,} trips | {len(BUS_CACHE)+len(KIOSK_CACHE)} buses cached | "
        f"WS: {'connected' if ws_connected else 'disconnected'}"
    )

@app.route('/bus/<stop_id>')
def get_bus_arrivals(stop_id):
    if stop_id not in STOPS:
        return jsonify({"error": "Stop not found"}), 404

    stop_info = STOPS[stop_id]
    approaching = calculate_approaching_buses(stop_id)

    return jsonify({
        "stop_id": stop_id,
        "stop_name": stop_info['name'],
        "stop_location": {"lat": stop_info['lat'], "lon": stop_info['lon']},
        "buses": approaching,
        "total_approaching": len(approaching),
        "timestamp": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route('/schedule/<stop_id>')
def get_schedule(stop_id):
    if stop_id not in STOPS:
        return jsonify({"error": "Stop not found"}), 404

    stop_info = STOPS[stop_id]
    schedule = get_static_schedule_for_stop(stop_id, limit=100)

    if not schedule:
        return jsonify({
            "stop_id": stop_id,
            "stop_name": stop_info['name'],
            "route": None,
            "arrival_minutes": None,
            "distance_km": None,
            "available": False,
            "schedules": [],
            "timestamp": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")
        })

    first_bus = schedule[0]
    return jsonify({
        "stop_id": stop_id,
        "stop_name": stop_info['name'],
        "route": first_bus.get('route'),
        "route_id": first_bus.get('route_id'),
        "arrival_minutes": first_bus.get('arrival_minutes'),
        "arrival_time": first_bus.get('arrival_time'),
        "departure_time": first_bus.get('departure_time'),
        "distance_km": first_bus.get('distance_km'),
        "stop_sequence": first_bus.get('stop_sequence'),
        "stop_headsign": first_bus.get('stop_headsign'),
        "available": True,
        "schedules": schedule,
        "timestamp": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route('/display/<stop_id>')
def display_stop(stop_id):
    """QR code web page — mobile-friendly bus arrival display"""
    if stop_id not in STOPS:
        return "Stop not found", 404

    stop_info = STOPS[stop_id]
    approaching = calculate_approaching_buses(stop_id)
    now = datetime.now(MYT).strftime("%H:%M:%S")

    HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>Bus Arrivals – {{ stop_name }}</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Space+Grotesk:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0f;
    --surface: #13131a;
    --border: #1e1e2e;
    --accent: #00e5a0;
    --accent2: #ff6b35;
    --text: #e8e8f0;
    --muted: #5a5a7a;
    --arriving: #ff6b35;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Space Grotesk', sans-serif;
    min-height: 100vh;
    padding: 16px;
  }

  /* Header */
  .header {
    border-bottom: 1px solid var(--border);
    padding-bottom: 14px;
    margin-bottom: 20px;
  }
  .header-top {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 6px;
  }
  .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--accent);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.8); }
  }
  .label {
    font-size: 10px;
    letter-spacing: 0.2em;
    color: var(--accent);
    text-transform: uppercase;
    font-family: 'DM Mono', monospace;
  }
  .stop-name {
    font-size: 22px;
    font-weight: 700;
    line-height: 1.2;
    color: var(--text);
  }
  .stop-id {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--muted);
    margin-top: 4px;
  }

  /* Bus cards */
  .bus-list { display: flex; flex-direction: column; gap: 10px; }

  .bus-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: relative;
    overflow: hidden;
  }
  .bus-card::before {
    content: '';
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 3px;
    background: var(--accent);
    border-radius: 3px 0 0 3px;
  }
  .bus-card.arriving::before { background: var(--arriving); }

  .bus-left { flex: 1; }
  .route-badge {
    display: inline-block;
    background: rgba(0, 229, 160, 0.1);
    border: 1px solid rgba(0, 229, 160, 0.3);
    color: var(--accent);
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    font-weight: 500;
    padding: 3px 10px;
    border-radius: 6px;
    margin-bottom: 6px;
  }
  .bus-card.arriving .route-badge {
    background: rgba(255, 107, 53, 0.1);
    border-color: rgba(255, 107, 53, 0.3);
    color: var(--arriving);
  }
  .bus-detail {
    font-size: 11px;
    color: var(--muted);
    font-family: 'DM Mono', monospace;
  }

  .bus-right { text-align: right; }
  .arrival-time {
    font-size: 32px;
    font-weight: 700;
    color: var(--accent);
    line-height: 1;
    font-family: 'DM Mono', monospace;
  }
  .bus-card.arriving .arrival-time {
    color: var(--arriving);
    font-size: 22px;
  }
  .arrival-unit {
    font-size: 11px;
    color: var(--muted);
    margin-top: 2px;
  }

  /* Empty state */
  .empty {
    text-align: center;
    padding: 48px 20px;
    color: var(--muted);
  }
  .empty-icon { font-size: 40px; margin-bottom: 12px; }
  .empty-text { font-size: 14px; }

  /* Footer */
  .footer {
    margin-top: 24px;
    padding-top: 14px;
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .footer-text {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: var(--muted);
  }
  .refresh-note {
    font-size: 10px;
    color: var(--muted);
    font-family: 'DM Mono', monospace;
  }
    .map-link {
        display: inline-block;
        margin-top: 12px;
        padding: 8px 12px;
        border: 1px solid var(--accent);
        border-radius: 6px;
        color: var(--accent);
        text-decoration: none;
        font-size: 12px;
    }
</style>
</head>
<body>

<div class="header">
  <div class="header-top">
    <div class="dot"></div>
    <span class="label">Live Bus Arrivals</span>
  </div>
  <div class="stop-name">{{ stop_name }}</div>
  <div class="stop-id">Stop {{ stop_id }}</div>
</div>

<div class="bus-list">
  {% if buses %}
    {% for bus in buses %}
    <div class="bus-card {% if bus.arrival_minutes < 1 %}arriving{% endif %}">
      <div class="bus-left">
        <div class="route-badge">{{ bus.route }}</div>
        <div class="bus-detail">{{ bus.distance_km }} km away &nbsp;·&nbsp; {{ bus.estimated_speed_kmh }} km/h</div>
      </div>
      <div class="bus-right">
        {% if bus.arrival_minutes < 1 %}
          <div class="arrival-time">ARRIVING</div>
        {% else %}
          <div class="arrival-time">{{ bus.arrival_minutes }}</div>
          <div class="arrival-unit">min</div>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="empty">
      <div class="empty-icon">🚌</div>
      <div class="empty-text">No buses approaching this stop right now.</div>
    </div>
  {% endif %}
</div>

<div class="footer">
  <span class="footer-text">Updated {{ now }}</span>
  <span class="refresh-note">Auto-refresh 30s</span>
</div>
<a class="map-link" href="/display/{{ stop_id }}/map">View live bus map</a>

</body>
</html>
"""
    from jinja2 import Template
    tmpl = Template(HTML)
    rendered = tmpl.render(
        stop_name=stop_info['name'],
        stop_id=stop_id,
        buses=approaching[:5],
        now=now
    )
    return rendered

@app.route('/display/<stop_id>/map')
def display_stop_map(stop_id):
        """Map page showing buses currently approaching a stop."""
        if stop_id not in STOPS:
                return "Stop not found", 404

        stop_info = STOPS[stop_id]
        approaching = calculate_approaching_buses(stop_id)

        return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="300">
    <title>Bus Map - {{ stop_info.name }}</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <style>
        :root { --bg: #0a0a0f; --surface: #13131a; --border: #1e1e2e; --text: #e8e8f0; --muted: #9a9ab5; --accent: #00e5a0; }
        * { box-sizing: border-box; }
        body { margin: 0; background: var(--bg); color: var(--text); font-family: Arial, sans-serif; }
        .header { padding: 16px; border-bottom: 1px solid var(--border); }
        .header h1 { margin: 0 0 6px; font-size: 20px; }
        .header p { margin: 0; color: var(--muted); font-size: 13px; }
        #map { height: calc(100vh - 91px); min-height: 420px; width: 100%; }
        .leaflet-popup-content { color: #111; }
        .bus-marker { background: #ff6b35; border: 3px solid white; border-radius: 50%; box-shadow: 0 1px 5px rgba(0,0,0,.45); }
        .stop-marker { background: var(--accent); border: 3px solid white; border-radius: 50%; box-shadow: 0 1px 5px rgba(0,0,0,.45); }
        .status { position: fixed; z-index: 1000; left: 16px; bottom: 16px; padding: 8px 10px; border-radius: 6px; background: rgba(10,10,15,.88); color: var(--muted); font-size: 12px; }
    </style>
</head>
<body>
    <header class="header">
        <h1>{{ stop_info.name }}</h1>
        <p>Stop {{ stop_id }} · Buses approaching this stop</p>
    </header>
    <div id="map"></div>
    <div id="status" class="status">Loading bus locations...</div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        const stopId = {{ stop_id|tojson }};
        const stop = {{ {'lat': stop_info.lat, 'lon': stop_info.lon, 'name': stop_info.name}|tojson }};
        const initialBuses = {{ approaching|tojson }};
        const map = L.map('map', { zoomControl: false }).setView([stop.lat, stop.lon], 14);
        const busMarkers = new Map();
        const routeLines = new Map();
        let hasFittedInitialMarkers = false;

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap contributors'
        }).addTo(map);

        const stopIcon = L.divIcon({ className: 'stop-marker', iconSize: [18, 18], iconAnchor: [9, 9] });
        L.marker([stop.lat, stop.lon], { icon: stopIcon })
            .addTo(map)
            .bindPopup('<strong>' + escapeHtml(stop.name) + '</strong><br>Bus stop');

        function escapeHtml(value) {
            return String(value).replace(/[&<>'"]/g, character => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
            }[character]));
        }

        function updateBuses(buses) {
            const visibleIds = new Set();
            buses.forEach(bus => {
                const latitude = Number(bus.latitude);
                const longitude = Number(bus.longitude);
                if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;

                visibleIds.add(bus.vehicle_id);
                const popup = '<strong>Route ' + escapeHtml(bus.route) + '</strong>' +
                    '<br>Arrival: ' + escapeHtml(bus.arrival_minutes) + ' min' +
                    '<br>Distance: ' + escapeHtml(bus.distance_km) + ' km' +
                    '<br>Bus: ' + escapeHtml(bus.vehicle_id);
                let marker = busMarkers.get(bus.vehicle_id);
                if (!marker) {
                    const busIcon = L.divIcon({ className: 'bus-marker', iconSize: [18, 18], iconAnchor: [9, 9] });
                    marker = L.marker([latitude, longitude], { icon: busIcon }).addTo(map);
                    busMarkers.set(bus.vehicle_id, marker);
                } else {
                    marker.setLatLng([latitude, longitude]);
                }
                marker.bindPopup(popup);

                const linePoints = Array.isArray(bus.route_path) && bus.route_path.length > 1
                    ? bus.route_path
                    : [
                        [latitude, longitude],
                        [stop.lat, stop.lon]
                    ];

                let line = routeLines.get(bus.vehicle_id);
                if (!line) {
                    line = L.polyline(linePoints, {
                        color: '#ff6b35',
                        weight: 3,
                        opacity: 0.9,
                        dashArray: '8 8'
                    }).addTo(map);
                    routeLines.set(bus.vehicle_id, line);
                } else {
                    line.setLatLngs(linePoints);
                }
            });

            busMarkers.forEach((marker, vehicleId) => {
                if (!visibleIds.has(vehicleId)) {
                    map.removeLayer(marker);
                    busMarkers.delete(vehicleId);
                }
            });

            routeLines.forEach((line, vehicleId) => {
                if (!visibleIds.has(vehicleId)) {
                    map.removeLayer(line);
                    routeLines.delete(vehicleId);
                }
            });

            if (!hasFittedInitialMarkers && buses.length > 0) {
                const markerPoints = [[stop.lat, stop.lon], ...buses.map(bus => [Number(bus.latitude), Number(bus.longitude)])];
                map.fitBounds(markerPoints, { padding: [40, 40], maxZoom: 14 });
                hasFittedInitialMarkers = true;
            }

            document.getElementById('status').textContent = buses.length +
                (buses.length === 1 ? ' bus approaching' : ' buses approaching') + ' · Updated just now';
        }

        async function refreshBuses() {
            try {
                const response = await fetch('/bus/' + encodeURIComponent(stopId));
                if (!response.ok) throw new Error('Bus data request failed');
                const data = await response.json();
                updateBuses(data.buses || []);
            } catch (error) {
                document.getElementById('status').textContent = 'Unable to update bus locations';
            }
        }

        updateBuses(initialBuses);
        setInterval(refreshBuses, 30000);
    </script>
</body>
</html>
        ''', stop_info=stop_info, stop_id=stop_id, approaching=approaching)

@app.route('/reload')
def reload_gtfs():
    return jsonify({
        "status": "success" if download_and_load_gtfs() else "error",
        "routes": len(ROUTES),
        "stops": len(STOPS),
        "trips": len(TRIPS)
    })

@app.route('/stats')
def stats():
    current_time = time.time()
    live_count = sum(1 for v in BUS_CACHE.values() if v['visible'] and (current_time - v['last_seen']) < 30)
    cached_count = sum(1 for v in BUS_CACHE.values() if v['visible']) - live_count
    kiosk_count = sum(1 for v in KIOSK_CACHE.values() if (current_time - v['last_seen']) < KIOSK_TIMEOUT)

    return jsonify({
        "routes": len(ROUTES),
        "stops": len(STOPS),
        "shapes": len(SHAPES),
        "trips": len(TRIPS),
        "gtfs_buses_live": live_count,
        "gtfs_buses_cached": cached_count,
        "kiosk_buses_live": kiosk_count,
        "websocket_connected": ws_connected,
        "ws_last_message_seconds_ago": int(time.time() - ws_last_message) if ws_last_message else None,
        "tracked_routes": list(TRACKED_ROUTES.keys()),
        "timestamp": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route('/stops')
def get_stops():
    stops_list = [
        {"stop_id": stop_id, "stop_name": stop_data.get("stop_name", "")}
        for stop_id, stop_data in STOPS.items()
    ]
    return jsonify(stops_list)

@app.route('/debug')
@app.route('/debug/')
def debug_home():
    """Landing page for debugging and monitoring tools."""
    sample_stops = [
        {"stop_id": stop_id, "stop_name": stop_data.get("name", stop_id)}
        for stop_id, stop_data in list(STOPS.items())[:12]
    ]
    debug_links = [
        ("WebSocket Status", "/debug/ws-status"),
        ("Kiosk Test", "/debug/kiosk-test"),
        ("Active Routes", "/debug/active-routes"),
        ("All Buses", "/debug/all-buses"),

        ("Raw Feed", "/debug/raw-feed"),
        ("Stats", "/stats"),
        ("Reload GTFS", "/reload"),
        ("Test Categories", "/debug/check-categories"),
    ]
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bus Arrival Debug</title>
  <style>
    :root { --bg:#0b1020; --card:#121a2d; --text:#f4f7ff; --muted:#8da0c3; --accent:#4fd1c5; --accent2:#7c93ff; --border:#23314d; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Arial, sans-serif; background:var(--bg); color:var(--text); padding:24px; }
    .wrap { max-width:960px; margin:0 auto; }
    h1 { margin:0 0 8px; font-size:28px; }
    p { color:var(--muted); line-height:1.5; }
    .grid { display:grid; grid-template-columns: 1.2fr 0.8fr; gap:20px; margin-top:24px; }
    .card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:18px; }
    .card h2 { margin-top:0; font-size:18px; }
    .links { display:grid; gap:10px; }
    .link-btn { display:block; text-decoration:none; padding:12px 14px; border-radius:10px; background:linear-gradient(135deg, var(--accent2), var(--accent)); color:white; font-weight:600; }
    .link-btn.secondary { background:transparent; border:1px solid var(--border); color:var(--text); }
    .stop-list { display:grid; gap:8px; margin-top:10px; }
    .stop-item { padding:10px 12px; border-radius:10px; background:rgba(255,255,255,0.03); border:1px solid var(--border); }
    .stop-item a { color:var(--accent); text-decoration:none; }
    .badge { color:var(--accent); font-size:12px; text-transform:uppercase; letter-spacing:0.08em; }
    .footer { margin-top:18px; color:var(--muted); font-size:13px; }
    @media (max-width: 700px) { .grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Bus Arrival Debug Center</h1>
    <p>Use these tools to inspect WebSocket activity, GTFS data, live bus feeds, and route matching.</p>

    <div class="grid">
      <div class="card">
        <h2>Debug Tools</h2>
        <div class="links">
          {% for label, url in debug_links %}
            <a class="link-btn {{ 'secondary' if label in ['Stats','Reload GTFS'] else '' }}" href="{{ url }}">{{ label }}</a>
          {% endfor %}
        </div>
      </div>

      <div class="card">
        <h2>Sample Stop Checks</h2>
        <div class="badge">Quick access</div>
        <div class="stop-list">
          {% for stop in sample_stops %}
            <div class="stop-item">
              <strong>{{ stop.stop_name }}</strong><br>
              <a href="/debug/stop/{{ stop.stop_id }}">Inspect stop {{ stop.stop_id }}</a>
            </div>
          {% endfor %}
        </div>
      </div>
    </div>

    <div class="footer">
      Loaded routes: {{ routes }} | stops: {{ stops }} | trips: {{ trips }}
    </div>
  </div>
</body>
</html>
    ''', debug_links=debug_links, sample_stops=sample_stops, routes=len(ROUTES), stops=len(STOPS), trips=len(TRIPS))

# ============================================
# DEBUG ENDPOINTS
# ============================================

@app.route('/debug/ws-status')
def debug_ws_status():
    return jsonify({
        "ws_connected": ws_connected,
        "ws_last_message_seconds_ago": int(time.time() - ws_last_message) if ws_last_message else None,
        "kiosk_cache_count": len(KIOSK_CACHE),
        "kiosk_buses": [
            {
                "vehicle_id": vid,
                "route_kiosk": entry['data'].get('route_kiosk_name'),
                "lat": entry['data'].get('latitude'),
                "lon": entry['data'].get('longitude'),
                "speed_kmh": round(entry['data'].get('speed', 0) * 3.6, 1),
                "age_seconds": int(time.time() - entry['last_seen']),
                "maps": f"https://maps.google.com/?q={entry['data'].get('latitude')},{entry['data'].get('longitude')}"
            }
            for vid, entry in KIOSK_CACHE.items()
        ],
        "timestamp": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route('/debug/kiosk-test')
def debug_kiosk_test():
    results = {}
    try:
        r = requests.get(POLLING_URL, timeout=10)
        raw = r.text
        json_start = raw.find('{')
        sid = None
        if json_start != -1:
            handshake = json.loads(raw[json_start:raw.rfind('}')+1])
            sid = handshake.get('sid')
        results['http_polling'] = {
            'status': r.status_code,
            'sid': sid,
            'response': raw[:200]
        }
    except Exception as e:
        results['http_polling'] = {'error': str(e)}
    return jsonify(results)

@app.route('/debug/stop/<stop_id>')
def debug_stop(stop_id):
    if stop_id not in STOPS:
        return jsonify({"error": "Stop not found"}), 404
    stop_info = STOPS[stop_id]
    serving_trips = []
    for trip_id, trip_data in TRIPS.items():
        if stop_id in trip_data['stops_served']:
            serving_trips.append({
                'trip_id': trip_id,
                'route_id': trip_data['route_id'],
                'route_short_name': ROUTES.get(trip_data['route_id'], {}).get('short_name', 'unknown')
            })
    return jsonify({
        'stop_id': stop_id,
        'name': stop_info['name'],
        'location': {'lat': stop_info['lat'], 'lon': stop_info['lon']},
        'trips_serving': len(serving_trips),
        'sample_trips': serving_trips[:5]
    })

@app.route('/debug/active-routes')
def active_routes():
    live_buses = fetch_live_buses()
    if not live_buses:
        return jsonify({"error": "No live buses"}), 500
    route_counts = {}
    for bus in live_buses:
        rid = bus.get('route_id', 'Unknown')
        matched = find_route_by_name(rid)
        display = ROUTES.get(matched, {}).get('short_name', rid) if matched else rid
        if display not in route_counts:
            route_counts[display] = {'count': 0, 'matched': matched is not None}
        route_counts[display]['count'] += 1
    sorted_routes = sorted(route_counts.items(), key=lambda x: x[1]['count'], reverse=True)
    return jsonify({
        'total_buses': len(live_buses),
        'total_routes': len(route_counts),
        'routes': [{'route_name': n, 'count': d['count'], 'matched': d['matched']} for n, d in sorted_routes]
    })

@app.route('/debug/all-buses')
def debug_all_buses():
    live_buses = fetch_live_buses()
    detailed = []
    for bus in live_buses:
        route_id = bus.get('route_id', 'unknown')
        short_name = ROUTES.get(route_id, {}).get('short_name', route_id)
        detailed.append({
            'number_plate': bus.get('vehicle_id', 'unknown'),
            'route_id_realtime': route_id,
            'route_short_name': short_name,
            'trip_id': bus.get('trip_id', 'unknown'),
            'latitude': bus.get('latitude', 0),
            'longitude': bus.get('longitude', 0),
            'speed_kmh': round(bus.get('speed', 0) * 3.6, 1),
            'source': bus.get('source', 'gtfs'),
            'maps_link': f"https://www.google.com/maps?q={bus.get('latitude', 0)},{bus.get('longitude', 0)}"
        })
    detailed.sort(key=lambda x: x['route_id_realtime'])
    return jsonify({
        'total_gtfs_buses': len(detailed),
        'total_kiosk_buses': len(KIOSK_CACHE),
        'timestamp': datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S"),
        'buses': detailed
    })

@app.route('/debug/t450')
def debug_t450():
    t450_route_id = None
    for rid, rinfo in ROUTES.items():
        if rinfo.get('short_name') == 'T450':
            t450_route_id = rid
            break
    kiosk_buses = [
        {
            'vehicle_id': vid,
            'lat': e['data']['latitude'],
            'lon': e['data']['longitude'],
            'speed_kmh': round(e['data']['speed'] * 3.6, 1),
            'age_seconds': int(time.time() - e['last_seen']),
            'maps': f"https://maps.google.com/?q={e['data']['latitude']},{e['data']['longitude']}"
        }
        for vid, e in KIOSK_CACHE.items()
        if 'T450' in str(e['data'].get('route_kiosk_name', ''))
    ]
    return jsonify({
        't450_route_id_in_static': t450_route_id,
        'ws_connected': ws_connected,
        'kiosk_t450_buses': len(kiosk_buses),
        'buses': kiosk_buses,
        'timestamp': datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route('/debug/raw-feed')
def debug_raw_feed():
    # Placeholder raw-feed debug endpoint
    return jsonify({'message': 'raw-feed debug placeholder'})

@app.route('/debug/check-categories')
def debug_check_categories():
    """Check activity for all known categories and report status."""
    categories = [
        "rapid-bus-penang",
        "rapid-bus-kuantan",
        "rapid-bus-mrtfeeder",
        "rapid-bus-kl",
    ]
    results = {}
    for cat in categories:
        realtime_url = f"https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana?category={cat}"
        try:
            r = requests.get(realtime_url, timeout=10)
            details = {'status_code': r.status_code, 'size': len(r.content)}
            # attempt to parse protobuf feed to verify content and count vehicles
            try:
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(r.content)
                details['entities'] = len(feed.entity)
                vehicle_ids = set()
                for ent in feed.entity:
                    if ent.HasField('vehicle'):
                        try:
                            vid = ent.vehicle.vehicle.id if ent.vehicle.HasField('vehicle') else None
                        except Exception:
                            # fallback: try attribute access
                            vid = getattr(ent.vehicle, 'vehicle', None)
                            if vid:
                                vid = getattr(vid, 'id', None)
                        if vid:
                            vehicle_ids.add(vid)
                details['vehicle_count'] = len(vehicle_ids)
                details['sample_vehicle_ids'] = list(vehicle_ids)[:10]
                results[cat] = {'active': len(vehicle_ids) > 0, 'details': details}
            except Exception as e:
                details['parse_error'] = str(e)
                results[cat] = {'active': False, 'details': details}
        except Exception as e:
            results[cat] = {'active': False, 'details': {'error': str(e)}}

    return jsonify(results)
    try:
        response = requests.get(GTFS_REALTIME_URL, timeout=10, allow_redirects=True)
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)
        vehicle_count = sum(1 for e in feed.entity if e.HasField('vehicle'))
        sample = []
        for entity in feed.entity:
            if entity.HasField('vehicle') and len(sample) < 3:
                v = entity.vehicle
                sample.append({
                    'vehicle_id': v.vehicle.id if v.HasField('vehicle') else 'none',
                    'trip_id': v.trip.trip_id if v.HasField('trip') else 'none',
                    'route_id': v.trip.route_id if v.HasField('trip') else 'none',
                })
        return jsonify({
            'total_entities': len(feed.entity),
            'vehicles': vehicle_count,
            'sample': sample,
            'timestamp': datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================
# STARTUP
# ============================================

print("\n" + "="*60)
print("STARTING BUS ARRIVAL SERVER")
print("="*60)
download_and_load_gtfs()
print("="*60 + "\n")

if __name__ == '__main__':
    app.run(debug=True)