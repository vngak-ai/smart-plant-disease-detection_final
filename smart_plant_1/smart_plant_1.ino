/**
 * SIT210 Smart Plant Disease Detection — Arduino Nano 33 IoT firmware
 *
 * Sensors:
 *   DHT22       D2   (10k pull-up)
 *   DS18B20     D4   (4.7k pull-up)
 *   BH1750      I2C  (A4/A5)
 *   Soil moist. A0   (calibrated DRY=810, WET=535)
 * Actuators:
 *   Water pump  D5  -> P2N2222 NPN base via 1k; pump on separate 5V supply;
 *                      1N5408 flyback diode across pump (cathode->pump +)
 *   Buzzer      D6   (active)
 *
 * MQTT topics
 *   PUB  plant/sensors           JSON every 5s
 *   PUB  plant/events            watering / alert events
 *   PUB  plant/status            "online"/"offline" (LWT, retained)
 *   SUB  plant/control/water         payload ignored, triggers manual water
 *   SUB  plant/control/check_disease beep ack (Pi runs inference itself)
 *   SUB  plant/control/plant_type    e.g. "tomato", "potato"
 *
 * Author: Nguyen Anh Khoa Vo — Deakin SIT210 T1 2026
 */

// ============================================================
// CONFIGURATION — change here when switching home <-> hotspot
// ============================================================
#define WIFI_SSID      "YOUR_WIFI_ID"
#define WIFI_PASS      "YOUR_WIFI_PASSWORD"

#define MQTT_BROKER    "PI_IP"   // Pi IP (change for hotspot)
#define MQTT_PORT      1883
#define MQTT_CLIENT_ID "plant-nano-01"

// ============================================================
// INCLUDES
// ============================================================
#include <SPI.h>
#include <WiFiNINA.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <DHT.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <BH1750.h>

// ============================================================
// PINS / CONSTANTS
// ============================================================
#define DHT_PIN              2
#define DHT_TYPE             DHT22
#define DS18B20_PIN          4
#define PUMP_PIN             5     // -> 1k -> P2N2222 base (active-HIGH: HIGH=pump ON)
#define BUZZER_PIN           6
#define SOIL_PIN             A0

// Pump driver logic (transistor / relay both active-HIGH):
//   digitalWrite(PUMP_PIN, HIGH) = pump ON
//   digitalWrite(PUMP_PIN, LOW)  = pump OFF
#define PUMP_ON              HIGH
#define PUMP_OFF             LOW

#define SOIL_DRY             810   // raw ADC when air-dry
#define SOIL_WET             535   // raw ADC when in water
#define MOISTURE_THRESHOLD   40    // % below which auto-water triggers

#define WATER_DURATION_MS    3000UL
// DEMO: cooldown 15s for quick demonstration. PRODUCTION: use 60000UL (60s)
// so water has time to soak before re-reading moisture (avoids over-watering).
#define MIN_WATER_INTERVAL_MS 15000UL    // min 15s between waterings (demo)
#define MAX_WATER_PER_HOUR   6           // safety cap (circuit breaker for stuck sensor)
// Window over which MAX_WATER_PER_HOUR is counted.
// DEMO: 120000UL (2 min) so repeated demo triggers don't hit the cap.
// PRODUCTION: 3600000UL (1 hour).
#define RATE_LIMIT_WINDOW_MS 120000UL
#define SENSOR_PUBLISH_MS    5000UL
#define WIFI_RECONNECT_MS    10000UL

// ============================================================
// GLOBALS
// ============================================================
DHT               dht(DHT_PIN, DHT_TYPE);
OneWire           oneWire(DS18B20_PIN);
DallasTemperature dsSensor(&oneWire);
BH1750            lightMeter;

WiFiClient        wifiClient;
PubSubClient      mqtt(wifiClient);

unsigned long lastSensorPublish = 0;
unsigned long lastWaterTime     = 0;
unsigned long lastWifiAttempt   = 0;
unsigned long wateringEventsMs[MAX_WATER_PER_HOUR];
int           wateringEventIdx  = 0;
bool          isOnline          = false;
String        currentPlant      = "tomato";

// Sensor reading bundle — must be declared above setup() so Arduino IDE's
// auto-generated function prototypes can see the type.
struct SensorReadings {
  float airTempC;
  float airHumidity;
  float soilTempC;
  int   soilMoisturePercent;
  float lux;
  bool  dhtOk, dsOk, bhOk;
};

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) { /* wait briefly for USB */ }
  Serial.println(F("\n=== SIT210 Smart Plant — booting ==="));

  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

  pinMode(PUMP_PIN, OUTPUT);
  digitalWrite(PUMP_PIN, PUMP_OFF);   // ensure pump OFF at boot

  Wire.begin();
  dht.begin();
  dsSensor.begin();
  if (!lightMeter.begin()) {
    Serial.println(F("[BH1750] init failed (will retry on each read)"));
  }

  for (int i = 0; i < MAX_WATER_PER_HOUR; i++) wateringEventsMs[i] = 0;

  connectWiFi();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setCallback(mqttCallback);
  connectMQTT();

  beep(2, 80);
  Serial.println(F("=== Boot complete ==="));
}

// ============================================================
// MAIN LOOP
// ============================================================
void loop() {
  // --- Maintain network ---
  if (WiFi.status() != WL_CONNECTED) {
    isOnline = false;
    if (millis() - lastWifiAttempt > WIFI_RECONNECT_MS) connectWiFi();
  } else if (!mqtt.connected()) {
    isOnline = false;
    connectMQTT();
  } else {
    isOnline = true;
    mqtt.loop();
  }

  // --- Publish sensor data periodically ---
  if (millis() - lastSensorPublish >= SENSOR_PUBLISH_MS) {
    lastSensorPublish = millis();
    publishSensorData();
  }

  // --- Auto-water decision (runs even offline = local fault tolerance) ---
  checkAutoWater();

  // --- Serial test commands (for bench testing without Pi/MQTT) ---
  handleSerialCommands();
}

// ============================================================
// SERIAL TEST COMMANDS  (type a letter + Enter in Serial Monitor)
//   w = force pump ON for WATER_DURATION_MS (bypasses moisture + rate limit)
//   b = test buzzer
//   s = print one sensor reading immediately
// ============================================================
void handleSerialCommands() {
  if (!Serial.available()) return;
  char c = Serial.read();
  // flush rest of line (newline etc.)
  while (Serial.available()) Serial.read();

  switch (c) {
    case 'w':
    case 'W':
      Serial.println(F("[TEST] manual pump pulse"));
      beep(1, 60);
      digitalWrite(PUMP_PIN, PUMP_ON);
      delay(WATER_DURATION_MS);
      digitalWrite(PUMP_PIN, PUMP_OFF);
      Serial.println(F("[TEST] pump pulse done"));
      break;
    case 'b':
    case 'B':
      Serial.println(F("[TEST] buzzer"));
      beep(2, 100);
      break;
    case 's':
    case 'S': {
      SensorReadings r = readSensors();
      Serial.print(F("[TEST] T=")); Serial.print(r.airTempC);
      Serial.print(F(" H=")); Serial.print(r.airHumidity);
      Serial.print(F(" SM=")); Serial.print(r.soilMoisturePercent);
      Serial.print(F("% L=")); Serial.println(r.lux);
      break;
    }
    default:
      break;   // ignore unknown chars
  }
}

// ============================================================
// NETWORKING
// ============================================================
void connectWiFi() {
  lastWifiAttempt = millis();
  Serial.print(F("[WiFi] connecting to "));
  Serial.println(WIFI_SSID);

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(500);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print(F("[WiFi] OK — IP: "));
    Serial.println(WiFi.localIP());
  } else {
    Serial.println(F("[WiFi] FAIL — running offline (local fallback)"));
  }
}

void connectMQTT() {
  if (WiFi.status() != WL_CONNECTED) return;

  Serial.print(F("[MQTT] connecting... "));
  // Last-Will-Testament: broker publishes "offline" if we drop ungracefully
  bool ok = mqtt.connect(
    MQTT_CLIENT_ID,
    "plant/status",   // LWT topic
    1,                // LWT QoS
    true,             // retained
    "offline"         // LWT payload
  );
  if (ok) {
    Serial.println(F("OK"));
    mqtt.publish("plant/status", "online", true);
    mqtt.subscribe("plant/control/water");
    mqtt.subscribe("plant/control/check_disease");
    mqtt.subscribe("plant/control/plant_type");
  } else {
    Serial.print(F("FAIL rc=")); Serial.println(mqtt.state());
  }
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String t = String(topic);
  String p; p.reserve(length);
  for (unsigned int i = 0; i < length; i++) p += (char)payload[i];

  Serial.print(F("[MQTT in] ")); Serial.print(t);
  Serial.print(F(" -> "));        Serial.println(p);

  if (t == "plant/control/water") {
    triggerWatering("manual");
  } else if (t == "plant/control/check_disease") {
    // Acknowledge with a single short beep; Pi handles actual inference
    beep(1, 100);
  } else if (t == "plant/control/plant_type") {
    currentPlant = p;
    publishEvent("plant_changed", currentPlant);
  }
}

// ============================================================
// SENSORS
// ============================================================
int readSoilMoisturePercent() {
  int raw = analogRead(SOIL_PIN);
  int pct = map(raw, SOIL_DRY, SOIL_WET, 0, 100);
  return constrain(pct, 0, 100);
}

SensorReadings readSensors() {
  SensorReadings r;

  float t = dht.readTemperature();
  float h = dht.readHumidity();
  r.dhtOk = !isnan(t) && !isnan(h);
  r.airTempC    = r.dhtOk ? t : NAN;
  r.airHumidity = r.dhtOk ? h : NAN;

  dsSensor.requestTemperatures();
  float st = dsSensor.getTempCByIndex(0);
  r.dsOk        = (st != DEVICE_DISCONNECTED_C);
  r.soilTempC   = r.dsOk ? st : NAN;

  r.soilMoisturePercent = readSoilMoisturePercent();

  float lx = lightMeter.readLightLevel();
  r.bhOk        = (lx >= 0);
  r.lux         = r.bhOk ? lx : NAN;

  return r;
}

float round1(float v) { return ((int)(v * 10 + 0.5)) / 10.0; }

void publishSensorData() {
  SensorReadings r = readSensors();

  // Always log to Serial — helps debug whether offline or online
  Serial.print(F("[Sensors] "));
  Serial.print(F("T=")); Serial.print(r.airTempC, 1); Serial.print(F("C "));
  Serial.print(F("H=")); Serial.print(r.airHumidity, 1); Serial.print(F("% "));
  Serial.print(F("ST=")); Serial.print(r.soilTempC, 1); Serial.print(F("C "));
  Serial.print(F("SM=")); Serial.print(r.soilMoisturePercent); Serial.print(F("% "));
  Serial.print(F("L=")); Serial.print(r.lux, 0); Serial.println(F("lx"));

  if (!isOnline) return;

  StaticJsonDocument<256> doc;
  doc["ts"]              = millis();
  if (r.dhtOk) {
    doc["temp_c"]        = round1(r.airTempC);
    doc["humid"]         = round1(r.airHumidity);
  }
  if (r.dsOk) doc["soil_temp"] = round1(r.soilTempC);
  doc["soil_moisture"]   = r.soilMoisturePercent;
  if (r.bhOk) doc["lux"] = (int)r.lux;
  doc["plant"]           = currentPlant;

  char buf[256];
  size_t n = serializeJson(doc, buf);
  mqtt.publish("plant/sensors", buf, n);
}

void publishEvent(const char* type, const String& detail) {
  Serial.print(F("[Event] ")); Serial.print(type);
  Serial.print(F(" ")); Serial.println(detail);
  if (!isOnline) return;

  StaticJsonDocument<128> doc;
  doc["type"]   = type;
  doc["detail"] = detail;
  doc["ts"]     = millis();
  char buf[128];
  size_t n = serializeJson(doc, buf);
  mqtt.publish("plant/events", buf, n);
}

// ============================================================
// AUTO-WATERING (with safety caps)
// ============================================================
void checkAutoWater() {
  if (millis() - lastWaterTime < MIN_WATER_INTERVAL_MS) return;

  int moisture = readSoilMoisturePercent();
  if (moisture >= MOISTURE_THRESHOLD) return;

  if (!canWaterUnderRateLimit()) {
    static unsigned long lastWarn = 0;
    if (millis() - lastWarn > 60000) {
      lastWarn = millis();
      publishEvent("rate_limited", String("moisture=") + moisture);
    }
    return;
  }

  triggerWatering("auto");
}

bool canWaterUnderRateLimit() {
  unsigned long now    = millis();
  unsigned long cutoff = (now > RATE_LIMIT_WINDOW_MS) ? (now - RATE_LIMIT_WINDOW_MS) : 0;
  int count = 0;
  for (int i = 0; i < MAX_WATER_PER_HOUR; i++) {
    if (wateringEventsMs[i] != 0 && wateringEventsMs[i] >= cutoff) count++;
  }
  return count < MAX_WATER_PER_HOUR;
}

void triggerWatering(const char* reason) {
  Serial.print(F("[Water] trigger reason=")); Serial.println(reason);

  wateringEventsMs[wateringEventIdx] = millis();
  wateringEventIdx = (wateringEventIdx + 1) % MAX_WATER_PER_HOUR;
  lastWaterTime    = millis();

  publishEvent("watering_started", reason);
  beep(1, 60);                       // short pre-alert beep
  digitalWrite(PUMP_PIN, PUMP_ON);   // transistor/relay ON -> pump runs
  delay(WATER_DURATION_MS);
  digitalWrite(PUMP_PIN, PUMP_OFF);  // pump OFF
  publishEvent("watering_done", reason);
}

// ============================================================
// BUZZER
// ============================================================
void beep(int count, int durationMs) {
  for (int i = 0; i < count; i++) {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(durationMs);
    digitalWrite(BUZZER_PIN, LOW);
    if (i < count - 1) delay(durationMs);
  }
}
