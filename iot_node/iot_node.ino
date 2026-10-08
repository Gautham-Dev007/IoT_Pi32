
#include <WiFi.h>
#include <ESPmDNS.h>
#include <PubSubClient.h>
#include <HTTPUpdate.h>
#include <Preferences.h>
#include <LittleFS.h>
#include "secrets.h"
#ifdef USE_DS18B20
#include <OneWire.h>
#include <DallasTemperature.h>
#else
#include <dhtnew.h>
#endif
#include <sys/time.h>
#include <time.h>
#include "esp_ota_ops.h"
#include "esp_system.h"

struct Rgb { float r, g, b; };

#ifndef LED_PIN
#define LED_PIN 48
#endif
#ifndef SENSOR_PIN
#ifdef DS18B20_PIN
#define SENSOR_PIN DS18B20_PIN
#else
#define SENSOR_PIN 1
#endif
#endif
#ifdef USE_DS18B20
#define SENSOR_NAME "ds18b20"
#else
#define SENSOR_NAME "am2305b"
#endif
#ifndef HUB_HOSTNAME
#define HUB_HOSTNAME "iothub"
#endif
#ifndef AP_SSID
#define AP_SSID "IoTHub"
#endif
#ifndef AP_PASS
#define AP_PASS ""
#endif
#ifndef MQTT_PORT
#define MQTT_PORT 1883
#endif
#ifndef MQTT_HOST
#define MQTT_HOST ""
#endif

#if ESP_ARDUINO_VERSION_MAJOR < 3
#define rgbLedWrite neopixelWrite
#endif

static const char *FW_VERSION = "1.4.2";

static const uint32_t ROLLBACK_TIMEOUT_MS = 180000;
static const uint32_t INFO_EVERY_MS = 60000;
static const uint32_t BUF_RECORDS = 8000;
static const int DRAIN_BATCH = 20;
static const time_t VALID_EPOCH = 1700000000;

struct WifiNet { const char *ssid, *pass; };
static const WifiNet NETS[] = {{sizeof(AP_PASS) > 1 ? AP_SSID : "", AP_PASS}, {WIFI_SSID, WIFI_PASS}};

WiFiClient netClient;
PubSubClient mqtt(netClient);
Preferences prefs;
#ifdef USE_DS18B20
OneWire oneWire(SENSOR_PIN);
DallasTemperature ds(&oneWire);
#else
DHTNEW dht(SENSOR_PIN);
int dhtFails = 0;
#endif

String nodeId, base;
uint32_t intervalMs = 10000;
uint16_t bootId = 0;

bool wifiWasUp = false;
uint32_t lastWifiAttempt = 0;
IPAddress brokerIP;
int mqttFails = 0;
uint32_t lastMqttTry = 0;

bool sensorOk = false, converting = false;
uint32_t convStart = 0, lastSample = 0;
float lastTemp = NAN, lastHum = NAN;

uint8_t brightness = 40;
uint32_t identifyUntil = 0;
uint32_t lastLedColor = 0xFFFFFFFF;

bool pendingVerify = false, otaInProgress = false, rebootRequested = false, renamed = false;
String pendingOtaUrl;
uint32_t lastInfo = 0, lastDrain = 0;

extern "C" bool verifyRollbackLater() { return true; }

String topic(const char *s) { return base + s; }
bool timeValid() { return time(nullptr) > VALID_EPOCH; }

void logMsg(const String &m) {
  Serial.println(m);
  if (mqtt.connected()) mqtt.publish(topic("log").c_str(), m.c_str());
}

struct __attribute__((packed)) Rec {
  uint32_t epoch;
  uint32_t upS;
  uint16_t boot;
  int16_t c100;
  int16_t h100;
};
static const int16_t NO_HUM = INT16_MIN;
struct BufHdr { uint32_t magic, head, count, dropped; };
static const uint32_t BUF_MAGIC = 0x42554632;
static const char *BUF_PATH = "/buf.bin";
BufHdr hdr = {BUF_MAGIC, 0, 0, 0};
bool bufReady = false;

void bufSaveHdr(File &f) { f.seek(0); f.write((uint8_t *)&hdr, sizeof hdr); }

void bufInit() {
  if (!LittleFS.begin(true)) { Serial.println("LittleFS failed - no offline buffer"); return; }
  File f = LittleFS.open(BUF_PATH, "r");
  if (f && f.size() == sizeof(BufHdr) + BUF_RECORDS * sizeof(Rec)) {
    f.read((uint8_t *)&hdr, sizeof hdr);
    f.close();
    if (hdr.magic == BUF_MAGIC && hdr.head < BUF_RECORDS && hdr.count <= BUF_RECORDS) {
      bufReady = true;
      Serial.printf("Offline buffer: %u stored readings\n", (unsigned)hdr.count);
      return;
    }
  }
  if (f) f.close();
  f = LittleFS.open(BUF_PATH, "w");
  if (!f) return;
  hdr = {BUF_MAGIC, 0, 0, 0};
  f.write((uint8_t *)&hdr, sizeof hdr);
  uint8_t zero[256] = {0};
  size_t left = BUF_RECORDS * sizeof(Rec);
  while (left) { size_t n = left > sizeof zero ? sizeof zero : left; f.write(zero, n); left -= n; }
  f.close();
  bufReady = true;
}

void bufPush(float c, float h) {
  if (!bufReady) return;
  Rec r;
  r.epoch = timeValid() ? (uint32_t)time(nullptr) : 0;
  r.upS = millis() / 1000;
  r.boot = bootId;
  r.c100 = (int16_t)lroundf(c * 100);
  r.h100 = isnan(h) ? NO_HUM : (int16_t)lroundf(h * 100);
  uint32_t idx = (hdr.head + hdr.count) % BUF_RECORDS;
  if (hdr.count == BUF_RECORDS) { hdr.head = (hdr.head + 1) % BUF_RECORDS; hdr.dropped++; }
  else hdr.count++;
  File f = LittleFS.open(BUF_PATH, "r+");
  if (!f) return;
  f.seek(sizeof(BufHdr) + idx * sizeof(Rec));
  f.write((uint8_t *)&r, sizeof r);
  bufSaveHdr(f);
  f.close();
}

void bufDrainOnce() {
  if (!bufReady || hdr.count == 0) return;
  File f = LittleFS.open(BUF_PATH, "r+");
  if (!f) return;
  uint32_t n = hdr.count < (uint32_t)DRAIN_BATCH ? hdr.count : DRAIN_BATCH;
  String json = "{\"r\":[";
  uint32_t nowUp = millis() / 1000, sent = 0, lost = 0;
  time_t now = time(nullptr);
  for (uint32_t i = 0; i < n; i++) {
    Rec r;
    f.seek(sizeof(BufHdr) + ((hdr.head + i) % BUF_RECORDS) * sizeof(Rec));
    f.read((uint8_t *)&r, sizeof r);
    long t;
    if (r.epoch > VALID_EPOCH) t = r.epoch;
    else if (r.boot == bootId && timeValid()) t = (long)(now - (nowUp - r.upS));
    else if (r.boot == bootId) t = -(long)(nowUp - r.upS);
    else { lost++; continue; }
    char item[48];
    if (r.h100 == NO_HUM)
      snprintf(item, sizeof item, "%s[%ld,%.2f]", sent ? "," : "", t, r.c100 / 100.0);
    else
      snprintf(item, sizeof item, "%s[%ld,%.2f,%.2f]", sent ? "," : "", t, r.c100 / 100.0, r.h100 / 100.0);
    json += item;
    sent++;
  }
  json += "]}";
  bool ok = sent == 0 || mqtt.publish(topic("backlog").c_str(), json.c_str());
  if (ok) {
    hdr.head = (hdr.head + n) % BUF_RECORDS;
    hdr.count -= n;
    hdr.dropped += lost;
    bufSaveHdr(f);
    if (hdr.count == 0) logMsg("stored readings all delivered");
  }
  f.close();
}

void ledWrite(uint8_t r, uint8_t g, uint8_t b) {
  uint32_t c = ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
  if (c == lastLedColor) return;
  lastLedColor = c;
  rgbLedWrite(LED_PIN, r, g, b);
}

static const Rgb C_GREEN = {0.05f, 1.0f, 0.25f}, C_BLUE = {0.1f, 0.35f, 1.0f}, C_AMBER = {1.0f, 0.45f, 0.0f},
                 C_CYAN = {0.0f, 0.8f, 1.0f}, C_RED = {1.0f, 0.04f, 0.02f}, C_PURPLE = {0.65f, 0.1f, 1.0f},
                 C_WHITE = {1.0f, 0.85f, 0.7f};
uint32_t pingAt = 0;
uint32_t lastLedFrame = 0;

static float phase(uint32_t period) { return (float)(millis() % period) / period; }

static float breathe(uint32_t period, float lo) {
  float s = 0.5f - 0.5f * cosf(2.0f * PI * phase(period));
  return lo + (1.0f - lo) * s;
}

static float bump(float t, float c, float w) {
  float d = (t - c) / w;
  return d > -1 && d < 1 ? 0.5f + 0.5f * cosf(PI * d) : 0.0f;
}

static float heartbeat(uint32_t period, float lo) {
  float t = phase(period);
  float v = bump(t, 0.10f, 0.08f) + 0.7f * bump(t, 0.30f, 0.08f);
  return lo + (1.0f - lo) * (v > 1 ? 1 : v);
}

static Rgb hue(float h) {
  float r = fabsf(h * 6 - 3) - 1, g = 2 - fabsf(h * 6 - 2), b = 2 - fabsf(h * 6 - 4);
  auto cl = [](float x) { return x < 0 ? 0.0f : x > 1 ? 1.0f : x; };
  return {cl(r), cl(g), cl(b)};
}

static void show(const Rgb &c, float level, float scale) {
  float lv = powf(level < 0 ? 0 : level > 1 ? 1 : level, 2.2f) * scale;
  uint32_t since = millis() - pingAt;
  float ping = pingAt && since < 450 ? powf(1.0f - since / 450.0f, 2.0f) * 0.55f * scale : 0.0f;
  auto ch = [&](float v) { float x = v * lv + ping * C_WHITE.r; return (uint8_t)(x > 255 ? 255 : x); };
  ledWrite(ch(c.r), ch(c.g), ch(c.b));
}

void updateLed() {
  if (millis() - lastLedFrame < 15) return;
  lastLedFrame = millis();
  float scale = brightness;

  if (otaInProgress) { show(C_PURPLE, 1.0f, scale); return; }
  if (millis() < identifyUntil) {
    show(hue(phase(2000)), 1.0f, 230);
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    show(C_BLUE, breathe(1400, 0.0f), scale);
  } else if (!mqtt.connected()) {
    show(C_AMBER, heartbeat(2400, 0.04f), scale);
  } else if (!sensorOk) {
    show(C_RED, heartbeat(1600, 0.0f), scale);
  } else if (hdr.count > 0) {
    show(C_CYAN, breathe(700, 0.15f), scale);
  } else {
    show(C_GREEN, breathe(4500, 0.12f), scale);
  }
}

void publishLedState() {
  char buf[48];
  snprintf(buf, sizeof buf, "brightness=%u mode=status", brightness);
  mqtt.publish(topic("led/state").c_str(), buf, true);
}

struct Recent { uint32_t epoch; uint32_t upS; int16_t c100, h100; };
static const int RECENT_N = 40;
Recent recentBuf[RECENT_N];
int recentHead = 0, recentCount = 0;

void recentPush(float c, float h) {
  Recent &r = recentBuf[recentHead];
  r.epoch = timeValid() ? (uint32_t)time(nullptr) : 0;
  r.upS = millis() / 1000;
  r.c100 = (int16_t)lroundf(c * 100);
  r.h100 = isnan(h) ? NO_HUM : (int16_t)lroundf(h * 100);
  recentHead = (recentHead + 1) % RECENT_N;
  if (recentCount < RECENT_N) recentCount++;
}

void resendRecent() {
  if (!recentCount) return;
  uint32_t nowUp = millis() / 1000;
  time_t now = time(nullptr);
  int first = (recentHead - recentCount + RECENT_N) % RECENT_N;
  for (int i = 0; i < recentCount; i += DRAIN_BATCH) {
    String json = "{\"r\":[";
    int last = i + DRAIN_BATCH < recentCount ? i + DRAIN_BATCH : recentCount;
    for (int j = i; j < last; j++) {
      const Recent &r = recentBuf[(first + j) % RECENT_N];
      long t = r.epoch > VALID_EPOCH ? (long)r.epoch
             : timeValid() ? (long)(now - (nowUp - r.upS)) : -(long)(nowUp - r.upS);
      char item[48];
      if (r.h100 == NO_HUM) snprintf(item, sizeof item, "%s[%ld,%.2f]", j > i ? "," : "", t, r.c100 / 100.0);
      else snprintf(item, sizeof item, "%s[%ld,%.2f,%.2f]", j > i ? "," : "", t, r.c100 / 100.0, r.h100 / 100.0);
      json += item;
    }
    json += "]}";
    mqtt.publish(topic("backlog").c_str(), json.c_str());
    mqtt.loop();
  }
  recentCount = 0;
}

void handleReading(float c, float h) {
  lastTemp = c;
  lastHum = h;
  bool sent = false;
  if (mqtt.connected()) {
    char b[16];
    snprintf(b, sizeof b, "%.2f", c);
    sent = mqtt.publish(topic("temp").c_str(), b);
    if (sent && !isnan(h)) {
      snprintf(b, sizeof b, "%.2f", h);
      mqtt.publish(topic("hum").c_str(), b);
    }
  }
  if (sent) { pingAt = millis(); recentPush(c, h); }
  else bufPush(c, h);
}

#ifdef USE_DS18B20
void sensorBegin() {
  ds.begin();
  sensorOk = ds.getDeviceCount() > 0;
  if (sensorOk) {
    ds.setResolution(12);
    ds.setWaitForConversion(false);
  }
  Serial.printf("DS18B20 on GPIO %d: %s\n", SENSOR_PIN, sensorOk ? "found" : "NOT found");
}

void sensorLoop() {
  if (!converting && millis() - lastSample >= intervalMs) {
    lastSample = millis();
    if (!sensorOk) sensorBegin();
    if (!sensorOk) return;
    ds.requestTemperatures();
    converting = true;
    convStart = millis();
  }
  if (converting && millis() - convStart >= 800) {
    converting = false;
    float c = ds.getTempCByIndex(0);
    if (c == DEVICE_DISCONNECTED_C || c < -55 || c > 125 || c == 85.0f) {
      if (sensorOk) logMsg("DS18B20 read failed - check wiring / 4.7k pull-up");
      sensorOk = false;
      return;
    }
    sensorOk = true;
    handleReading(c, NAN);
  }
}
#else
void sensorBegin() {
  dht.setType(22);
  sensorOk = false;
  Serial.printf("AM2305B on GPIO %d\n", SENSOR_PIN);
}

void sensorLoop() {
  uint32_t every = intervalMs < 2500 ? 2500 : intervalMs;
  if (millis() - lastSample < every) return;
  lastSample = millis();
  int rc = dht.read();
  float c = dht.getTemperature(), h = dht.getHumidity();
  if (rc != DHTLIB_OK || isnan(c) || c < -40 || c > 125 || h < 0 || h > 100) {
    if (++dhtFails == 3) logMsg("AM2305B not responding (code " + String(rc) + ") - check wiring / pull-up");
    if (dhtFails >= 3) sensorOk = false;
    return;
  }
  if (!sensorOk && dhtFails >= 3) logMsg("AM2305B OK again");
  dhtFails = 0;
  sensorOk = true;
  handleReading(c, h);
}
#endif

static const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power_on";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_EXT: return "ext";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    default: return "other";
  }
}

void publishInfo() {
  char buf[560];
  snprintf(buf, sizeof buf,
           "{\"id\":\"%s\",\"fw\":\"%s\",\"built\":\"%s %s\",\"ip\":\"%s\",\"wifi\":\"%s\",\"rssi\":%d,"
           "\"hub\":\"%s\",\"uptime_s\":%lu,\"heap\":%u,\"interval_s\":%lu,\"sensor\":\"%s\","
           "\"chip_c\":%.1f,\"hum\":%s,\"buffered\":%lu,\"dropped\":%lu,\"time_ok\":%s,"
           "\"boot\":%u,\"reset\":\"%s\"}",
           nodeId.c_str(), FW_VERSION, __DATE__, __TIME__, WiFi.localIP().toString().c_str(),
           WiFi.SSID().c_str(), (int)WiFi.RSSI(), brokerIP.toString().c_str(),
           (unsigned long)(millis() / 1000), (unsigned)ESP.getFreeHeap(),
           (unsigned long)(intervalMs / 1000), sensorOk ? SENSOR_NAME : "missing", temperatureRead(),
           isnan(lastHum) ? "null" : String(lastHum, 1).c_str(), (unsigned long)hdr.count, (unsigned long)hdr.dropped, timeValid() ? "true" : "false",
           (unsigned)bootId, resetReason());
  mqtt.publish(topic("info").c_str(), buf, true);
}

void handleCommand(String msg, bool broadcast) {
  msg.trim();
  if (msg.isEmpty()) return;
  String cmd = msg, arg;
  int sp = msg.indexOf(' ');
  if (sp > 0) { cmd = msg.substring(0, sp); arg = msg.substring(sp + 1); arg.trim(); }
  cmd.toLowerCase();

  if (cmd == "reboot") {
    rebootRequested = true;
  } else if (cmd == "info") {
    publishInfo();
  } else if (cmd == "identify") {
    identifyUntil = millis() + 10000;
    logMsg("rainbow for 10 s (find node)");
  } else if (cmd == "ota") {
    String allowed = "http://" + brokerIP.toString() + ":";
    if (!arg.startsWith(allowed)) { logMsg("ota refused: firmware must come from the hub (" + allowed + ")"); return; }
    pendingOtaUrl = arg;
  } else if (cmd == "interval") {
    long s = arg.toInt();
    if (s < 2 || s > 3600) { logMsg("interval must be 2..3600 seconds"); return; }
    intervalMs = s * 1000;
    prefs.putULong("interval", intervalMs);
    publishInfo();
  } else if (cmd == "brightness") {
    long v = arg.toInt();
    if (v < 0 || v > 255) { logMsg("brightness must be 0..255"); return; }
    brightness = v;
    prefs.putUChar("bright", brightness);
    lastLedColor = 0xFFFFFFFF;
    publishLedState();
  } else if (cmd == "clearbuffer") {
    hdr.head = hdr.count = 0;
    File f = LittleFS.open(BUF_PATH, "r+");
    if (f) { bufSaveHdr(f); f.close(); }
    logMsg("offline buffer cleared");
  } else if (cmd == "name") {
    if (broadcast) { logMsg("refusing to rename every node at once"); return; }
    arg.toLowerCase();
    bool ok = arg.length() > 0 && arg.length() <= 24;
    for (unsigned i = 0; i < arg.length(); i++) {
      char c = arg[i];
      ok = ok && (isalnum((unsigned char)c) || c == '-' || c == '_');
    }
    if (!ok) { logMsg("name: 1-24 chars, a-z 0-9 - _"); return; }
    prefs.putString("id", arg);
    logMsg("renamed to " + arg + ", rebooting");
    mqtt.publish(topic("status").c_str(), "", true);
    mqtt.publish(topic("info").c_str(), "", true);
    mqtt.publish(topic("led/state").c_str(), "", true);
    renamed = true;
    rebootRequested = true;
  } else {
    logMsg("unknown command '" + msg + "' (reboot, info, identify, ota <url>, interval <s>, "
           "brightness <0-255>, clearbuffer, name <id>)");
  }
}

void onMessage(char *t, byte *payload, unsigned int len) {
  String tp(t), msg;
  msg.reserve(len);
  for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];
  if (tp == "home/hub/time") {
    long e = msg.toInt();
    if (e > VALID_EPOCH && (!timeValid() || labs((long)time(nullptr) - e) > 120)) {
      struct timeval tv = {e, 0};
      settimeofday(&tv, nullptr);
      Serial.println("Clock set from hub");
    }
    return;
  }
  Serial.printf("<- %s: %s\n", t, msg.c_str());
  bool broadcast = tp.startsWith("home/all/");
  if (tp.endsWith("/cmd")) handleCommand(msg, broadcast);
}

void performHttpOta(const String &url) {
  otaInProgress = true;
  updateLed();
  String st = topic("ota/status");
  mqtt.publish(st.c_str(), ("downloading " + url).c_str());
  mqtt.loop();

  WiFiClient client;
  httpUpdate.rebootOnUpdate(false);
  HTTPUpdateResult r = httpUpdate.update(client, url);
  if (r == HTTP_UPDATE_OK) {
    mqtt.publish(st.c_str(), "written, rebooting");
    mqtt.publish(topic("status").c_str(), "offline", true);
    mqtt.loop();
    delay(300);
    ESP.restart();
  }
  String err = "failed: " + httpUpdate.getLastErrorString();
  mqtt.publish(st.c_str(), err.c_str());
  Serial.println(err);
  otaInProgress = false;
}

void resolveBroker() {
  IPAddress ip;
  if (WiFi.SSID() == AP_SSID) {
    ip = WiFi.gatewayIP();
  } else {
    ip = MDNS.queryHost(HUB_HOSTNAME, 2000);
    if (ip[0] == 0) {
      if (prefs.getString("bkSsid", "") == WiFi.SSID()) ip.fromString(prefs.getString("bkIp", ""));
      if (ip[0] == 0 && strlen(MQTT_HOST)) ip.fromString(MQTT_HOST);
    }
  }
  brokerIP = ip;
  Serial.printf("Hub address: %s\n", ip[0] ? ip.toString().c_str() : "not found");
  if (ip[0]) mqtt.setServer(brokerIP, MQTT_PORT);
}

void onWifiUp() {
  wifiWasUp = true;
  Serial.printf("WiFi: %s, IP %s, %d dBm\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(),
                (int)WiFi.RSSI());
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  MDNS.end();
  MDNS.begin(nodeId.c_str());
  mqttFails = 0;
  lastMqttTry = 0;
  resolveBroker();
}

void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiWasUp) onWifiUp();
    return;
  }
  if (wifiWasUp) {
    wifiWasUp = false;
    lastWifiAttempt = 0;
    Serial.println("WiFi lost");
  }
  if (lastWifiAttempt && millis() - lastWifiAttempt < 15000) return;
  lastWifiAttempt = millis();

  WiFi.disconnect(false);
  int n = WiFi.scanNetworks();
  for (const auto &net : NETS) {
    if (!strlen(net.ssid)) continue;
    for (int i = 0; i < n; i++) {
      if (WiFi.SSID(i) == net.ssid) {
        Serial.printf("WiFi: joining %s\n", net.ssid);
        WiFi.scanDelete();
        WiFi.begin(net.ssid, net.pass);
        return;
      }
    }
  }
  WiFi.scanDelete();
  Serial.println("WiFi: no known network in range");
}

void ensureMqtt() {
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED) return;
  if (lastMqttTry && millis() - lastMqttTry < 5000) return;
  lastMqttTry = millis();
  if (brokerIP[0] == 0 || mqttFails >= 3) {
    resolveBroker();
    mqttFails = 0;
    if (brokerIP[0] == 0) return;
  }

  String st = topic("status");
  if (!mqtt.connect(nodeId.c_str(), MQTT_USER, MQTT_PASS, st.c_str(), 1, true, "offline")) {
    mqttFails++;
    Serial.printf("MQTT connect to %s failed, rc=%d\n", brokerIP.toString().c_str(), mqtt.state());
    return;
  }
  mqttFails = 0;
  Serial.printf("MQTT connected to hub %s as %s\n", brokerIP.toString().c_str(), nodeId.c_str());
  if (WiFi.SSID() != AP_SSID) {
    prefs.putString("bkSsid", WiFi.SSID());
    prefs.putString("bkIp", brokerIP.toString());
  }
  mqtt.publish(st.c_str(), "online", true);
  mqtt.publish(topic("cmd").c_str(), "", true);
  mqtt.subscribe(topic("cmd").c_str());
  mqtt.subscribe("home/all/cmd");
  mqtt.subscribe("home/hub/time");

  if (pendingVerify) {
    esp_ota_mark_app_valid_cancel_rollback();
    pendingVerify = false;
    mqtt.publish(topic("ota/status").c_str(), "new firmware confirmed");
  }
  String booted = String("booted fw=") + FW_VERSION + " built=" + __DATE__ + " " + __TIME__;
  mqtt.publish(topic("ota/status").c_str(), booted.c_str());
  resendRecent();
  if (hdr.count) logMsg("hub back - sending " + String(hdr.count) + " stored readings");
  publishInfo();
  publishLedState();
  lastInfo = millis();
}

void setup() {
  Serial.begin(115200);
  delay(200);
  rgbLedWrite(LED_PIN, 10, 10, 10);

  prefs.begin("node", false);
  nodeId = prefs.getString("id", "");
  if (nodeId.isEmpty()) {
    uint64_t mac = ESP.getEfuseMac();
    char b[16];
    snprintf(b, sizeof b, "esp-%02x%02x%02x", (unsigned)((mac >> 24) & 0xFF),
             (unsigned)((mac >> 32) & 0xFF), (unsigned)((mac >> 40) & 0xFF));
    nodeId = b;
  }
  base = "home/" + nodeId + "/";
  bootId = prefs.getUShort("boot", 0) + 1;
  prefs.putUShort("boot", bootId);
  brightness = prefs.getUChar("bright", 40);
  intervalMs = prefs.getULong("interval", 10000);

  const esp_partition_t *running = esp_ota_get_running_partition();
  esp_ota_img_states_t state;
  if (esp_ota_get_state_partition(running, &state) == ESP_OK && state == ESP_OTA_IMG_PENDING_VERIFY)
    pendingVerify = true;
  Serial.printf("\nNode %s, fw %s, boot #%u, partition %s%s\n", nodeId.c_str(), FW_VERSION, bootId,
                running->label, pendingVerify ? " (new image, awaiting confirmation)" : "");

  bufInit();
  sensorBegin();

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(nodeId.c_str());
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(false);

  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(1024);
  mqtt.setKeepAlive(30);
  mqtt.setSocketTimeout(5);
  lastSample = millis() - intervalMs;
}

void loop() {
  ensureWifi();
  if (WiFi.status() == WL_CONNECTED) ensureMqtt();
  mqtt.loop();
  sensorLoop();

  if (mqtt.connected()) {
    if (hdr.count && millis() - lastDrain >= 150) { lastDrain = millis(); bufDrainOnce(); }
    if (millis() - lastInfo >= INFO_EVERY_MS) { lastInfo = millis(); publishInfo(); }
  }

  if (pendingOtaUrl.length()) {
    String url = pendingOtaUrl;
    pendingOtaUrl = "";
    performHttpOta(url);
  }
  if (rebootRequested) {
    if (!renamed) mqtt.publish(topic("status").c_str(), "offline", true);
    mqtt.disconnect();
    delay(200);
    ESP.restart();
  }
  if (pendingVerify && millis() > ROLLBACK_TIMEOUT_MS) {
    Serial.println("New firmware never reached the hub - rolling back");
    esp_ota_mark_app_invalid_rollback_and_reboot();
  }
  updateLed();
  delay(2);
}
