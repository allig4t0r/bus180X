import requests
import urllib3
import time

from google.transit import gtfs_realtime_pb2
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict
from os import environ

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://transport.orgp.spb.ru/Portal/transport/internalapi/gtfs/realtime"

# stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,wheelchair_boarding,transport_type
# 3426,3426,"УЛ. ПРОКОФЬЕВА, УГОЛ УЛ. КОМПОЗИТОРОВ",60.062532,30.321748,0,2,bus
# 4604,4604,"УЛ. СИМОНОВА, УГ. УЛ. ПРОКОФЬЕВА",60.061265,30.326373,0,2,bus
# 35807,35807,ПОЛИКЛИНИКА №117,60.056488,30.322662,0,2,bus
# 4572,4572,ПР. ПРОСВЕЩЕНИЯ УГ. УЛ. СИМОНОВА,60.055241,30.322768,0,2,bus
# 23234,23234,"УЛ. ХОШИМИНА, СТ. МЕТРО ""ПРОСПЕКТ ПРОСВЕЩЕНИЯ""",60.053430,30.329378,0,2,bus
# 3573,3573,"УЛ. КОМПОЗИТОРОВ, УГОЛ УЛ. ХОШИМИНА",60.051803,30.316150,0,2,bus
# 3425,3425,"УЛ. КОМПОЗИТОРОВ, УГОЛ ПР. ПРОСВЕЩЕНИЯ",60.057460,30.314892,0,2,bus
# 2245,2245,"УЛ. КОМПОЗИТОРОВ,29",60.060999,30.317766,0,2,bus
# 30129,30129,"СУЗДАЛЬСКИЙ ПР., УГОЛ УЛ. КОМПОЗИТОРОВ, ПО ТРЕБОВАНИЮ",60.065432,30.319365,0,2,bus
# 30130,30130,"СУЗДАЛЬСКИЙ ПР., УГОЛ УЛ. ЖЕНИ ЕГОРОВОЙ, ПО ТРЕБОВАНИЮ",60.066473,30.314230,0,2,bus

# route_id,agency_id,route_short_name,route_long_name,route_type,transport_type,circular,urban,night
# 3219,orgp,180,"АВТОБУСНАЯ СТАНЦИЯ ""УЛ. ЖЕНИ ЕГОРОВОЙ"" - КАМЫШОВАЯ УЛ.",3,bus,0,1,0

# for yandex maps: "LATITUDE, LONGITUDE"

ROUTE_ID = "3219" #bus №180
STOP_EGOROVOY = "30130"
STOP_PROSVET = "4572"
TZ = ZoneInfo("Europe/Moscow")
GRACE_SECONDS = 30
TIMEOUT = 15
CHECK_INTERVAL = 2
BUS_STUCK_SECONDS = 300  # 5 minutes
TELEGRAM_BOT_TOKEN = environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = environ.get('TELEGRAM_CHANNEL_ID')

BOUNDING_BOX_15MIN = (
    30.319648,  # min longitude
    60.061188,  # min latitude
    30.327351,  # max longitude
    60.063287   # max latitude
)

BOUNDING_BOX_BIG = (
    30.315120,  # min longitude
    60.054887,  # min latitude
    30.327598,  # max longitude
    60.064232   # max latitude
)

def fetch_vehicle_positions(route_id: str) -> List[Dict]:
    """
    Step 1:
    Get all vehicles currently running on a specific route.
    """
    url = f"{BASE_URL}/vehicle"
    params = {
        "transports": "bus",
        "routeIDs": route_id
    }

    response = requests.get(url, params=params, timeout=TIMEOUT, verify=False)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    
    vehicles = []

    for entity in feed.entity:
        if entity.HasField("vehicle"):
            vehicle = entity.vehicle
            vehicles.append({
                "vehicle_id": vehicle.vehicle.id,
                "license_plate": vehicle.vehicle.license_plate,
                "label": vehicle.vehicle.label,
                "route_id": vehicle.trip.route_id,
                "latitude": vehicle.position.latitude,
                "longitude": vehicle.position.longitude,
                "bearing": vehicle.position.bearing,
                "speed": vehicle.position.speed,
                "timestamp": vehicle.timestamp
            })

    return vehicles

def is_in_bounding_box(lat: float, lon: float, bbox: tuple) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

def bearing_to_direction(bearing: float) -> str | None:
    """"
    Returns di"rection name if bearing matches required sectors.
    
    GTFS bearing is defined as:
    0° = North
    90° = East
    180° = South
    270° = West
    """
    bearing = bearing % 360

    if 67.5 <= bearing < 112.5:
        return "E"
    if 112.5 <= bearing < 157.5:
        return "SE"
    if 157.5 <= bearing < 202.5:
        return "S"
    if 202.5 <= bearing < 247.5:
        return "SW"

    return None

def get_vehicles_in_bbox_heading_target_directions(
    route_id: str,
    bbox: tuple,
    vehicles_snapshot: list[dict] | None = None
) -> List[Dict]:
    if vehicles_snapshot is None:
        vehicles = fetch_vehicle_positions(route_id)
    else:
        vehicles = vehicles_snapshot
    result = []

    for v in vehicles:
        if not is_in_bounding_box(v["latitude"], v["longitude"], bbox):
            continue

        direction = bearing_to_direction(v["bearing"])
        if direction is None:
            continue

        v_filtered = v.copy()
        v_filtered["direction"] = direction
        result.append(v_filtered)

    return result

def fetch_trip_updates(vehicle_ids):
    """
    Step 2:
    For a list of vehicle IDs, get detailed stop-by-stop predictions.
    """
    if not vehicle_ids:
        return []

    url = f"{BASE_URL}/vehicletrips"
    params = {
        "vehicleIDs": ",".join(vehicle_ids)
    }

    response = requests.get(url, params=params, timeout=TIMEOUT, verify=False)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    trip_updates = []

    for entity in feed.entity:
        if entity.HasField("trip_update"):
            trip_updates.append(entity.trip_update)

    return trip_updates

def find_bus_at_stop(route_id: str, stop_id: str):
    """
    Step 3:
    Combine vehicle positions + trip updates
    and extract arrival info for a specific stop.
    """
    vehicles = fetch_vehicle_positions(route_id)
    vehicle_ids = [v["vehicle_id"] for v in vehicles]

    trip_updates = fetch_trip_updates(vehicle_ids)

    results = []

    for update in trip_updates:
        vehicle_id = update.vehicle.id

        for stop_time in update.stop_time_update:
            if stop_time.stop_id == stop_id:
                arrival = stop_time.arrival.time if stop_time.HasField("arrival") else None

                results.append({
                    "vehicle_id": vehicle_id,
                    "route_id": route_id,
                    "stop_id": stop_id,
                    "arrival_ts": arrival,
                    "arrival_time_local": (
                        datetime.fromtimestamp(arrival, tz=timezone.utc)
                        .astimezone(TZ)
                        .isoformat()
                        if arrival else None
                    )
                })

    now_ts = int(time.time())

    # print(f"GTFS arrivals count: {len(results)}")
    results = [
        r for r in results
        if r["arrival_ts"] is not None and r["arrival_ts"] >= now_ts - GRACE_SECONDS
    ]

    results.sort(key=lambda r: r["arrival_ts"])

    return results

def send_telegram_message(text: str):
    print(f"Telegram message sent:\n\n{text}\n")
    if TELEGRAM_BOT_TOKEN is None:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True
    }
    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()

class MonitorState:
    def __init__(self):
        self.active_buses: Dict[str, Dict] = {}
        self.sent_arrival_predictions: set[int] = set()
        self.last_prediction_ts: int | None = None

    # ---------------------
    # Formatting helpers
    # ---------------------

    @staticmethod
    def format_bus_info(bus: Dict) -> str:
        return (
            f"\n"
            f"Код бусика: {bus['label']}\n"
            f"Номерной знак: *{bus['license_plate']}*"
        )

    @staticmethod
    def format_arrival_hhmm(arrival_ts: int) -> str:
        return (
            datetime.fromtimestamp(arrival_ts, tz=timezone.utc)
            .astimezone(TZ)
            .strftime("%H:%M")
        )

    # ---------------------
    # Core loop
    # ---------------------

    def run(self):
        while True:
            cycle_start = time.time()

            # -------------------------
            # 1. Fetch vehicles ONCE
            # -------------------------
            all_vehicles = fetch_vehicle_positions(ROUTE_ID)

            vehicles_by_id = {
                v["vehicle_id"]: v
                for v in all_vehicles
            }

            bbox_vehicles = get_vehicles_in_bbox_heading_target_directions(
                ROUTE_ID,
                BOUNDING_BOX_15MIN,
                vehicles_snapshot=all_vehicles
            )
            
            big_bbox_vehicles = get_vehicles_in_bbox_heading_target_directions(
                ROUTE_ID,
                BOUNDING_BOX_BIG,
                vehicles_snapshot=all_vehicles
            )
            
            bbox_ids = {v["vehicle_id"] for v in bbox_vehicles}
            big_bbox_labels = {v["label"] for v in big_bbox_vehicles}

            # -------------------------
            # 2. New bus entry
            # -------------------------
            for bus in bbox_vehicles:
                vid = bus["vehicle_id"]

                if vid not in self.active_buses:
                    self.active_buses[vid] = {
                        "bus": bus,
                        "entered_at": time.time()
                    }

                    print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses total: {len(all_vehicles)}")
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses in bbox: {len(bbox_vehicles)}")
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses in big bbox: {len(big_bbox_labels)}. Bus labels: {big_bbox_labels}")
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] {bus}\n")

                    send_telegram_message(
                        f"🌼 Анечка, солнышко, пора выходить 😊\n"
                        f"{self.format_bus_info(bus)}"
                    )

            # -------------------------
            # 3. Exit or stuck buses
            # -------------------------
            for vid in list(self.active_buses.keys()):
                record = self.active_buses[vid]
                bus = record["bus"]
                entered_at = record["entered_at"]

                if vid not in bbox_ids:
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses total: {len(all_vehicles)}")
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses in bbox: {len(bbox_vehicles)}")
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses in big bbox: {len(big_bbox_labels)}. Bus labels: {big_bbox_labels}")
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] {bus}\n")

                    send_telegram_message(
                        f"‼️ АНЕЧКА❗ ПОСЛЕДНИЙ ШАНС❗ Выходи сейчас❗\n"
                        f"{self.format_bus_info(bus)}"
                    )
                    del self.active_buses[vid]
                    continue

                if time.time() - entered_at > BUS_STUCK_SECONDS:
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses total: {len(all_vehicles)}")
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses in bbox: {len(bbox_vehicles)}")
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses in big bbox: {len(big_bbox_labels)}. Bus labels: {big_bbox_labels}")
                    print(f"[{self.format_arrival_hhmm(cycle_start)}] {bus}\n")

                    send_telegram_message(
                        f"Чот случилось с бусиком 😥\n"
                        f"{self.format_bus_info(bus)}"
                    )
                    del self.active_buses[vid]

            # -------------------------
            # 4. Arrival predictions
            # -------------------------
            predictions = find_bus_at_stop(ROUTE_ID, STOP_EGOROVOY)

            for p in predictions:
                arrival_ts = p["arrival_ts"]
                vehicle_id = p["vehicle_id"]

                if arrival_ts is None:
                    continue

                if arrival_ts == self.last_prediction_ts:
                    continue

                if arrival_ts in self.sent_arrival_predictions:
                    continue

                bus = vehicles_by_id.get(vehicle_id)

                self.sent_arrival_predictions.add(arrival_ts)
                self.last_prediction_ts = arrival_ts

                print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses total: {len(all_vehicles)}")
                print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses in bbox: {len(bbox_vehicles)}")
                print(f"[{self.format_arrival_hhmm(cycle_start)}] Buses in big bbox: {len(big_bbox_labels)}. Bus labels: {big_bbox_labels}")
                print(f"[{self.format_arrival_hhmm(cycle_start)}] GTFS arrivals count: {len(predictions)}")
                print(f"[{self.format_arrival_hhmm(cycle_start)}] sent_arrival_predictions: {len(self.sent_arrival_predictions)}")
                print(f"[{self.format_arrival_hhmm(cycle_start)}] {p}")
                print(f"[{self.format_arrival_hhmm(cycle_start)}] {bus}\n")

                send_telegram_message(
                    f"🪄 🔮: 🚌 будет в "
                    f"*{self.format_arrival_hhmm(arrival_ts)}*\n"
                    f"{self.format_bus_info(bus)}"
                    # f"\\(может показывать для следующего бусика, не текущего\\)"
                )

            # -------------------------
            # 5. Sleep
            # -------------------------
            elapsed = time.time() - cycle_start
            time.sleep(max(0, CHECK_INTERVAL - elapsed))

if __name__ == "__main__":
    print("========= Starting bus180X project =========")
    print(
        "Starting time: "
        f"{datetime.now(TZ).strftime("%a %d %b %H:%M:%S %Z %Y")}"
    )
    MonitorState().run()
    print("========= Stopping bus180X project =========")
