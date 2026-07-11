// Solar-Powered Real-Time Bus Arrival Display System
// ESP32-S3 + WeAct GDEY042T81 4.2" E-Ink Display

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <GxEPD2_BW.h>
#include <Fonts/FreeMonoBold9pt7b.h>
#include <Fonts/FreeMonoBold12pt7b.h>
#include <Fonts/FreeMonoBold18pt7b.h>
#include <Fonts/FreeMonoBold24pt7b.h>
#include "qrcode_ric.h"

// ===== WiFi Credentials =====
const char* ssid = "Maruky";
const char* password = "muffinbrrrr06";

// ===== Flask Server =====
const char* serverURL = "https://bus-time-arrival.onrender.com/bus/1001512";
const char* scheduleURL = "https://bus-time-arrival.onrender.com/schedule/1001512";
const char* qrURL     = "https://bus-time-arrival.onrender.com/display/1001512";

// ===== Pin Definitions =====
#define EPD_CS      10
#define EPD_DC       9
#define EPD_RST     46
#define EPD_BUSY     3
#define LED_PIN      2
#define TOUCH_PIN    4
#define BATT_PIN    20

// ===== Battery Monitoring =====
#define BATT_LOW_V    3.5f
#define BATT_MIN_VALID_V  2.5f
#define BATT_DIVIDER  2.0f
#define BATT_CAL_FACTOR  0.987f
#define BATT_LOW_CONFIRM_COUNT  3
#define BATT_CHECK_INTERVAL     30000

// ===== TTP223 Touch Module =====
#define TOUCH_ACTIVE_STATE  HIGH
#define TOUCH_LOG_INTERVAL  1000
#define TOUCH_DEBOUNCE      250
#define LED_DURATION        30000   // 30 seconds

// ===== Display Initialization =====
GxEPD2_BW<GxEPD2_420_GDEY042T81, GxEPD2_420_GDEY042T81::HEIGHT> display(
  GxEPD2_420_GDEY042T81(EPD_CS, EPD_DC, EPD_RST, EPD_BUSY)
);

// ===== Bus Data Structure =====
struct Bus {
  String route;
  float arrivalMinutes;
  float distanceKm;
};

Bus buses[3];
int busCount = 0;
int lastBusCount = 0;
String stopName = "";
String scheduleRoute = "";
int scheduleArrivalMinutes = -1;
float scheduleDistanceKm = 0.0f;
bool scheduleAvailable = false;

// ===== Stored values at last API update =====
float lastArrivalMinutes[3] = {0, 0, 0};
float lastDistanceKm[3]     = {0, 0, 0};

// ===== Battery voltage (for on-screen debug display) =====
float lastBatteryVoltage = 0.0f;

// ===== LED state =====
unsigned long ledOnTime = 0;
bool ledActive = false;
unsigned long lastTouchLog = 0;
unsigned long lastTouchTrigger = 0;

// ===== Timing =====
unsigned long lastAPIUpdate     = 0;
unsigned long lastDisplayUpdate = 0;
unsigned long lastFullRefresh   = 0;
unsigned long lastBatteryCheck  = 0;
bool qrScreenActive = false;

const unsigned long API_INTERVAL          = 60000;
const unsigned long DISPLAY_INTERVAL      = 10000;
const unsigned long FULL_REFRESH_INTERVAL = 600000;

// ===== Battery voltage reading (calibrated, using analogReadMilliVolts) =====
float readBatteryVoltage() {
  long mvTotal = 0;
  for (int i = 0; i < 10; i++) {
    mvTotal += analogReadMilliVolts(BATT_PIN);
    delay(5);
  }
  float pinMilliVolts = mvTotal / 10.0f;
  float pinVoltage = pinMilliVolts / 1000.0f;
  float voltage = pinVoltage * BATT_DIVIDER * BATT_CAL_FACTOR;

  lastBatteryVoltage = voltage;   // store for on-screen display

  Serial.print("Battery pin mV: ");
  Serial.print(pinMilliVolts, 1);
  Serial.print(" pin: ");
  Serial.print(pinVoltage, 3);
  Serial.print("V battery: ");
  Serial.print(voltage, 2);
  Serial.println("V");

  return voltage;
}

bool isLowBatteryReading(float voltage) {
  return voltage > BATT_MIN_VALID_V && voltage < BATT_LOW_V;
}

bool batteryLowConfirmed() {
  int lowCount = 0;

  for (int i = 0; i < BATT_LOW_CONFIRM_COUNT; i++) {
    float voltage = readBatteryVoltage();
    if (isLowBatteryReading(voltage)) {
      lowCount++;
    }
    delay(100);
  }

  if (lowCount > 0) {
    Serial.print("Low battery samples: ");
    Serial.print(lowCount);
    Serial.print("/");
    Serial.println(BATT_LOW_CONFIRM_COUNT);
  }

  return lowCount >= BATT_LOW_CONFIRM_COUNT;
}

// ===== LED + Touch handler =====
void handleLED() {
  unsigned long now = millis();
  int touchState = digitalRead(TOUCH_PIN);

  if (now - lastTouchLog >= TOUCH_LOG_INTERVAL) {
    Serial.print("Touch SIG: ");
    Serial.println(touchState == TOUCH_ACTIVE_STATE ? "HIGH" : "LOW");
    lastTouchLog = now;
  }

  if (touchState == TOUCH_ACTIVE_STATE && now - lastTouchTrigger >= TOUCH_DEBOUNCE) {
    Serial.println("Touch detected - LED ON");
    digitalWrite(LED_PIN, HIGH);
    ledActive = true;
    ledOnTime = now;
    lastTouchTrigger = now;
  }

  if (ledActive && (now - ledOnTime >= LED_DURATION)) {
    Serial.println("LED auto OFF");
    digitalWrite(LED_PIN, LOW);
    ledActive = false;
  }
}

void waitWithLED(unsigned long durationMs) {
  unsigned long start = millis();
  while (millis() - start < durationMs) {
    handleLED();
    delay(50);
  }
}

// ===== Setup =====
void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("\n=================================");
  Serial.println("Bus Arrival Display System");
  Serial.println("=================================\n");

  // Pin setup
  pinMode(LED_PIN, OUTPUT);
  pinMode(TOUCH_PIN, INPUT_PULLDOWN);
  pinMode(BATT_PIN, INPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(BATT_PIN, ADC_11db);
  digitalWrite(LED_PIN, LOW);

  Serial.println("Initializing display...");
  display.init(115200);
  display.setRotation(0);
  Serial.println("✓ Display ready!\n");

  // Battery check
  if (batteryLowConfirmed()) {
    Serial.println("⚠ Low battery — showing QR screen");
    showQRScreen(true);
    waitWithLED(300000);
    ESP.restart();
    return;
  }

  showLoadingScreen();
  connectWiFi();

  if (WiFi.status() != WL_CONNECTED) {
    showQRScreen(false);
    return;
  }

  fetchBusData();
  fetchScheduleData();
  displayBusArrivals(true);
  lastAPIUpdate     = millis();
  lastDisplayUpdate = millis();
  lastFullRefresh   = millis();
  lastBatteryCheck  = millis();
}

// ===== Loop =====
void loop() {
  unsigned long now = millis();

  // Handle LED touch every cycle
  handleLED();

  // Battery check
  if (now - lastBatteryCheck >= BATT_CHECK_INTERVAL) {
    lastBatteryCheck = now;

    if (batteryLowConfirmed()) {
      Serial.println("⚠ Low battery!");
      digitalWrite(LED_PIN, LOW);  // turn off LED to save power
      showQRScreen(true);
      waitWithLED(300000);
      ESP.restart();
      return;
    }
  }

  // Check WiFi
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("⚠ WiFi lost — showing QR screen");
    digitalWrite(LED_PIN, LOW);
    showQRScreen(false);
    waitWithLED(60000);
    connectWiFi();
    return;
  }

  // Fetch new API data every 60 seconds
  if (now - lastAPIUpdate >= API_INTERVAL) {
    Serial.println("\nFetching new API data...");
    fetchBusData();
    fetchScheduleData();

    for (int i = 0; i < busCount; i++) {
      lastArrivalMinutes[i] = buses[i].arrivalMinutes;
      lastDistanceKm[i]     = buses[i].distanceKm;
    }

    lastAPIUpdate = now;
  }

  // Update display every 10 seconds
  if (now - lastDisplayUpdate >= DISPLAY_INTERVAL) {
    float elapsed = (now - lastAPIUpdate) / 60000.0;

    for (int i = 0; i < busCount; i++) {
      buses[i].arrivalMinutes = lastArrivalMinutes[i] - elapsed;
      if (buses[i].arrivalMinutes < 0) buses[i].arrivalMinutes = 0;

      if (lastArrivalMinutes[i] > 0) {
        float speedKmPerMin = lastDistanceKm[i] / lastArrivalMinutes[i];
        buses[i].distanceKm = lastDistanceKm[i] - (speedKmPerMin * elapsed);
        if (buses[i].distanceKm < 0) buses[i].distanceKm = 0;
      }
    }

    bool busCountChanged    = (busCount != lastBusCount);
    bool timeForFullRefresh = (now - lastFullRefresh >= FULL_REFRESH_INTERVAL);
    bool returningFromQR    = qrScreenActive;

    if (busCountChanged || timeForFullRefresh || returningFromQR) {
      displayBusArrivals(true);
      lastFullRefresh = now;
    } else {
      displayBusArrivals(false);
    }

    lastBusCount      = busCount;
    lastDisplayUpdate = now;
  }
}

// ===== WiFi Connection =====
void connectWiFi() {
  Serial.print("Connecting to WiFi: ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n✓ WiFi Connected!");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n✗ WiFi Failed!");
  }
}

// ===== Fetch Bus Data =====
void fetchBusData() {
  if (WiFi.status() != WL_CONNECTED) {
    showQRScreen(false);
    return;
  }

  HTTPClient http;
  http.begin(serverURL);
  http.setTimeout(10000);

  int httpCode = http.GET();
  Serial.print("HTTP Code: ");
  Serial.println(httpCode);

  if (httpCode == 200) {
    String payload = http.getString();
    if (parseJSON(payload)) {
      Serial.println("✓ JSON parsed!");
    }
  } else {
    Serial.print("✗ HTTP Error: ");
    Serial.println(httpCode);
    showQRScreen(false);
  }

  http.end();
}

// ===== Fetch Schedule Data =====
void fetchScheduleData() {
  if (WiFi.status() != WL_CONNECTED) {
    scheduleAvailable = false;
    scheduleRoute = "";
    scheduleArrivalMinutes = -1;
    scheduleDistanceKm = 0.0f;
    return;
  }

  HTTPClient http;
  http.begin(scheduleURL);
  http.setTimeout(10000);

  int httpCode = http.GET();
  Serial.print("Schedule HTTP Code: ");
  Serial.println(httpCode);

  if (httpCode == 200) {
    String payload = http.getString();
    JsonDocument doc;
    DeserializationError error = deserializeJson(doc, payload);

    if (!error) {
      scheduleAvailable = doc["available"].as<bool>();
      if (scheduleAvailable) {
        scheduleRoute = doc["route"].as<String>();
        scheduleArrivalMinutes = (int)(doc["arrival_minutes"].as<float>() + 0.5f);
        scheduleDistanceKm = doc["distance_km"].as<float>();
      } else {
        scheduleRoute = "";
        scheduleArrivalMinutes = -1;
        scheduleDistanceKm = 0.0f;
      }
    } else {
      scheduleAvailable = false;
      scheduleRoute = "";
      scheduleArrivalMinutes = -1;
      scheduleDistanceKm = 0.0f;
    }
  } else {
    scheduleAvailable = false;
    scheduleRoute = "";
    scheduleArrivalMinutes = -1;
    scheduleDistanceKm = 0.0f;
  }

  http.end();
}

// ===== Parse JSON =====
bool parseJSON(String json) {
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, json);
  if (error) return false;

  stopName = doc["stop_name"].as<String>();
  JsonArray busArray = doc["buses"];
  busCount = 0;

  for (JsonObject busObj : busArray) {
    if (busCount >= 3) break;
    buses[busCount].route          = busObj["route"].as<String>();
    buses[busCount].arrivalMinutes = busObj["arrival_minutes"].as<float>();
    buses[busCount].distanceKm     = busObj["distance_km"].as<float>();
    lastArrivalMinutes[busCount]   = buses[busCount].arrivalMinutes;
    lastDistanceKm[busCount]       = buses[busCount].distanceKm;
    busCount++;
  }
  return true;
}

void drawHeaderIcons(float voltage, bool connected, int x, int y) {
  int batteryX = x;
  int wifiX = x + 38;

  display.drawRect(batteryX, y, 22, 12, GxEPD_BLACK);
  display.drawRect(batteryX + 22, y + 4, 3, 4, GxEPD_BLACK);

  int fillWidth = 0;
  if (voltage >= 3.8f) {
    fillWidth = 18;
  } else if (voltage >= 3.5f) {
    fillWidth = 9;
  }

  if (fillWidth > 0) {
    display.fillRect(batteryX + 2, y + 2, fillWidth, 8, GxEPD_BLACK);
  }

  int cx = wifiX + 8;
  int cy = y + 6;
  display.drawCircle(cx, cy, 2, GxEPD_BLACK);
  display.drawCircle(cx, cy, 6, GxEPD_BLACK);
  display.drawCircle(cx, cy, 10, GxEPD_BLACK);

  if (!connected) {
    display.drawLine(cx - 10, cy + 10, cx + 8, cy - 8, GxEPD_BLACK);
  }
}

// ===== QR Code Screen =====
void showQRScreen(bool lowBattery) {
  Serial.println(lowBattery ? "Showing QR (low battery)..." : "Showing QR (no network)...");
  qrScreenActive = true;

  readBatteryVoltage();

  QRCode qrcode;
  uint8_t qrcodeData[qrcode_getBufferSize(4)];
  qrcode_initText(&qrcode, qrcodeData, 4, ECC_LOW, qrURL);

  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);

    display.setFont(&FreeMonoBold12pt7b);
    display.setCursor(20, 30);
    display.print("BUS ARRIVALS");

    drawHeaderIcons(lastBatteryVoltage, WiFi.status() == WL_CONNECTED, 330, 18);
    display.drawLine(20, 40, 380, 40, GxEPD_BLACK);

    display.setFont(&FreeMonoBold9pt7b);
    display.setCursor(20, 60);
    String shortName = stopName;
    if (shortName.length() > 35) shortName = shortName.substring(0, 35) + "...";
    display.print(shortName);
    display.drawLine(20, 75, 380, 75, GxEPD_BLACK);

    display.setFont(&FreeMonoBold12pt7b);
    display.setCursor(20, 105);
    display.print("SCHEDULE BUS");

    String routeText = "Route --";
    String timeText = "No schedule";
    if (scheduleAvailable) {
      routeText = "Route " + scheduleRoute;
      if (scheduleArrivalMinutes <= 1) {
        timeText = "Arriving now";
      } else {
        timeText = String(scheduleArrivalMinutes) + " min";
      }
    }

    display.setFont(&FreeMonoBold24pt7b);
    display.setCursor(20, 140);
    display.print(routeText);

    display.setFont(&FreeMonoBold18pt7b);
    display.setCursor(20, 172);
    display.print(timeText);

    int qrSize   = qrcode.size;
    int scale    = 4;
    int qrPixels = qrSize * scale;
    int qrX      = 230;
    int qrY      = 95;

    for (int y = 0; y < qrSize; y++) {
      for (int x = 0; x < qrSize; x++) {
        if (qrcode_getModule(&qrcode, x, y)) {
          display.fillRect(
            qrX + x * scale,
            qrY + y * scale,
            scale, scale,
            GxEPD_BLACK
          );
        }
      }
    }

  } while (display.nextPage());

  Serial.println("✓ QR screen shown");
}

// ===== Loading Screen =====
void showLoadingScreen() {
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);
    display.setFont(&FreeMonoBold18pt7b);
    display.setCursor(60, 160);
    display.print("Loading...");
  } while (display.nextPage());
}

// ===== Display Bus Arrivals =====
void displayBusArrivals(bool fullRefresh) {
  if (qrScreenActive) {
    fullRefresh = true;
  }

  // Fresh reading just before drawing, so the on-screen number is current
  readBatteryVoltage();

  Serial.println(fullRefresh ? "Updating display (full)..." : "Updating display (partial)...");

  if (fullRefresh) {
    display.setFullWindow();
  } else {
    display.setPartialWindow(20, 80, 360, 220);
  }

  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);

    // HEADER
    display.setFont(&FreeMonoBold18pt7b);
    display.setCursor(20, 35);
    display.print("BUS ARRIVALS");

    drawHeaderIcons(lastBatteryVoltage, WiFi.status() == WL_CONNECTED, 330, 18);

    display.drawLine(20, 45, 380, 45, GxEPD_BLACK);
    display.drawLine(20, 47, 380, 47, GxEPD_BLACK);

    // STOP NAME
    display.setFont(&FreeMonoBold9pt7b);
    display.setCursor(20, 65);
    String shortName = stopName;
    if (shortName.length() > 35) shortName = shortName.substring(0, 35) + "...";
    display.print(shortName);
    display.drawLine(20, 75, 380, 75, GxEPD_BLACK);

    // BUSES
    if (busCount == 0) {
      display.setFont(&FreeMonoBold12pt7b);
      display.setCursor(40, 150);
      display.print("No buses approaching");
    } else {
      int yPos = 115;
      for (int i = 0; i < busCount && i < 3; i++) {
        display.setFont(&FreeMonoBold24pt7b);
        display.setCursor(30, yPos);
        display.print(buses[i].route);

        int arrMins = (int)(buses[i].arrivalMinutes + 0.5);
        if (arrMins <= 1) {
          display.setFont(&FreeMonoBold12pt7b);
          display.setCursor(210, yPos);
          display.print("ARRIVING");
        } else {
          display.setFont(&FreeMonoBold24pt7b);
          display.setCursor(220, yPos);
          display.print(arrMins);
          display.setFont(&FreeMonoBold18pt7b);
          display.setCursor(280, yPos);
          display.print("min");
        }

        display.setFont(&FreeMonoBold9pt7b);
        display.setCursor(30, yPos + 20);
        display.print(buses[i].distanceKm, 1);
        display.print(" km away");
        display.drawLine(20, yPos + 30, 380, yPos + 30, GxEPD_BLACK);
        yPos += 70;
      }
    }

    // FOOTER
    display.drawLine(20, 290, 380, 290, GxEPD_BLACK);
    display.drawLine(20, 293, 380, 293, GxEPD_BLACK);
    display.setFont(&FreeMonoBold9pt7b);
    display.setCursor(20, 300);
    display.print("");

  } while (display.nextPage());

  qrScreenActive = false;

  Serial.println("✓ Display updated!\n");
}