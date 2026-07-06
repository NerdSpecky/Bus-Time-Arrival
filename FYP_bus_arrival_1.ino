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
#include <qrcode.h>

// ===== WiFi Credentials =====
const char* ssid = "Maruky";
const char* password = "muffin06";

// ===== Flask Server =====
const char* serverURL = "https://bus-time-arrival.onrender.com/bus/1001609";
const char* qrURL     = "https://bus-time-arrival.onrender.com/display/1001609";

// ===== E-ink Display Pins =====
#define EPD_CS     10
#define EPD_DC      9
#define EPD_RST    46
#define EPD_BUSY    3

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

// ===== Stored values at last API update =====
float lastArrivalMinutes[3] = {0, 0, 0};
float lastDistanceKm[3]     = {0, 0, 0};

// ===== Timing =====
unsigned long lastAPIUpdate     = 0;
unsigned long lastDisplayUpdate = 0;
unsigned long lastFullRefresh   = 0;

const unsigned long API_INTERVAL          = 60000;
const unsigned long DISPLAY_INTERVAL      = 10000;
const unsigned long FULL_REFRESH_INTERVAL = 600000;

// ===== Setup =====
void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("\n=================================");
  Serial.println("Bus Arrival Display System");
  Serial.println("=================================\n");

  Serial.println("Initializing display...");
  display.init(115200);
  display.setRotation(0);
  Serial.println("✓ Display ready!\n");

  showLoadingScreen();
  connectWiFi();

  if (WiFi.status() != WL_CONNECTED) {
    showQRScreen();
    return;
  }

  fetchBusData();
  displayBusArrivals(true);
  lastAPIUpdate     = millis();
  lastDisplayUpdate = millis();
  lastFullRefresh   = millis();
}

// ===== Loop =====
void loop() {
  unsigned long now = millis();

  // Check WiFi
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("⚠ WiFi lost — showing QR screen");
    showQRScreen();
    delay(60000);
    connectWiFi();
    return;
  }

  // Fetch new API data every 60 seconds
  if (now - lastAPIUpdate >= API_INTERVAL) {
    Serial.println("\nFetching new API data...");
    fetchBusData();

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

    if (busCountChanged || timeForFullRefresh) {
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
    showQRScreen();
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
    showQRScreen();
  }

  http.end();
}

// ===== Parse JSON =====
bool parseJSON(String json) {
  DynamicJsonDocument doc(4096);
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

// ===== QR Code Screen =====
void showQRScreen() {
  Serial.println("Showing QR screen...");

  QRCode qrcode;
  uint8_t qrcodeData[qrcode_getBufferSize(4)];
  qrcode_initText(&qrcode, qrcodeData, 4, ECC_LOW, qrURL);

  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);

    // Header
    display.setFont(&FreeMonoBold12pt7b);
    display.setCursor(20, 30);
    display.print("BUS ARRIVALS");
    display.drawLine(20, 40, 380, 40, GxEPD_BLACK);

    // Message
    display.setFont(&FreeMonoBold9pt7b);
    display.setCursor(20, 60);
    display.print("No network - scan for live arrivals");

    // QR code centered
    int qrSize   = qrcode.size;
    int scale    = 5;
    int qrPixels = qrSize * scale;
    int qrX      = (400 - qrPixels) / 2;
    int qrY      = 75;

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

    // URL below QR
    display.setFont(&FreeMonoBold9pt7b);
    display.setCursor(20, qrY + qrPixels + 20);
    display.print("bus-time-arrival.onrender.com");

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

  Serial.println("✓ Display updated!\n");
}
