# Bus Arrival System

A real-time Kuala Lumpur bus arrival system built with a Flask API, GTFS feeds, and ESP32-S3 e-paper displays.

## Components

- `app.py` - Flask server, GTFS loading, live vehicle tracking, API, and web displays.
- `templates/` - Mobile arrival and debug web pages.
- `FYP_bus_arrival_1/` - Base ESP32-S3 e-paper firmware.
- `FYP_bus_arrival_1_battery_voltage/` - Firmware with battery monitoring.
- `FYP_bus_arrival_1_touch_only/` - Firmware for the touch-controlled variant.
- `libraries/QRCode/` - Local QR code library used by the firmware.
- `tools/accuracy_logger.py` - Predictive arrival accuracy logger.
- `render.yaml` - Render deployment configuration.

## Features

- Live bus locations and arrival estimates.
- GTFS static schedule and route information.
- Mobile-friendly stop display.
- Live Leaflet map for approaching buses.
- WebSocket and polling vehicle feeds.
- Debug endpoints for route, stop, and feed diagnostics.
- ESP32-S3 e-paper display support.

## Run Locally

```bash
python -m pip install -r requirements.txt
python app.py
```

The server listens on `http://localhost:5000` by default. Live GTFS data is loaded when the app starts.

### Useful Routes

| Route | Purpose |
| --- | --- |
| `/` | Server status |
| `/bus/<stop_id>` | JSON arrival data |
| `/schedule/<stop_id>` | Static schedule data |
| `/display/<stop_id>` | Mobile arrival display |
| `/display/<stop_id>/map` | Live stop map |
| `/debug/` | Debug dashboard |
| `/stats` | Server statistics |

## Deploy to Render

This repository includes `render.yaml`. Deploy it as a Python Web Service:

- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --worker-class gthread --threads 2 --timeout 120`

The GTFS static feed is downloaded during startup. Internet access is required for live and static feed updates.

## ESP32 Firmware

Open one of the `.ino` folders in Arduino IDE or a compatible ESP32 IDE.

Before uploading:

1. Install `GxEPD2`, `Adafruit GFX`, `ArduinoJson`, and a compatible ESP32 board package.
2. Add the repository's `libraries/QRCode` folder to the Arduino libraries directory or set the sketchbook library path.
3. Replace `YOUR_WIFI_SSID` and `YOUR_WIFI_PASSWORD` in the sketch.
4. Replace the sample API and display URLs with your deployed Render URL.
5. Select the ESP32-S3 board that matches your hardware and upload.

## Accuracy Tool

```bash
python -m pip install pandas openpyxl
python tools/accuracy_logger.py --hours 2
```

Generated CSV and Excel reports should be kept outside source control or reviewed before publishing, as they may contain location observations.

