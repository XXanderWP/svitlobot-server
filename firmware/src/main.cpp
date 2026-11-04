/*
 * Прошивка SvitloBot для ESP32.
 *
 * Призначення: ESP32 живиться від мережі (без ДБЖ) і піднімає маленьку
 * HTTP-сторінку. Сервіс моніторингу на VPS періодично звертається до неї:
 * якщо сторінка відповідає — світло є.
 *
 * Налаштування (Wi-Fi, порт, ключ доступу, статичний IP) зберігаються в NVS
 * і передаються через UART утилітою svitlo-flasher. Опис протоколу — в AGENT.md.
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>

#ifndef FW_VERSION
#define FW_VERSION "0.0.0"
#endif

#ifndef LED_PIN
#define LED_PIN 2
#endif

// Ідентифікатор пристрою: сервер перевіряє його у відповіді, щоб не сплутати
// ESP32 зі сторінкою роутера чи іншого пристрою на тому ж порту.
static const char *DEVICE_ID = "svitlobot-esp32";
static const uint16_t DEFAULT_HTTP_PORT = 8080;
// Якщо Wi-Fi недоступний довше за цей час — перезавантажуємося.
static const uint32_t WIFI_RESTART_TIMEOUT_MS = 5UL * 60UL * 1000UL;
// Через скільки після початку підключення повідомити CLI про тайм-аут.
static const uint32_t WIFI_REPORT_TIMEOUT_MS = 30UL * 1000UL;

struct Config {
  String ssid;
  String password;
  String key;
  String hostname;
  String ip;
  String gateway;
  String subnet;
  String dns;
  uint16_t port = DEFAULT_HTTP_PORT;
};

static Config cfg;
static Preferences prefs;
static WebServer *server = nullptr;
static String serialBuf;

static bool wifiConnected = false;
static uint32_t wifiLostSince = 0;
static uint32_t wifiStartedAt = 0;
static bool wifiTimeoutReported = false;

// ---------------------------------------------------------------------------
// Збереження налаштувань
// ---------------------------------------------------------------------------

static void loadConfig() {
  prefs.begin("svb", true);
  cfg.ssid = prefs.getString("ssid", "");
  cfg.password = prefs.getString("pass", "");
  cfg.key = prefs.getString("key", "");
  cfg.hostname = prefs.getString("host", "svitlobot");
  cfg.ip = prefs.getString("ip", "");
  cfg.gateway = prefs.getString("gw", "");
  cfg.subnet = prefs.getString("mask", "");
  cfg.dns = prefs.getString("dns", "");
  cfg.port = prefs.getUShort("port", DEFAULT_HTTP_PORT);
  prefs.end();
}

static bool isValidIp(const String &value) {
  if (value.isEmpty()) return true;  // порожнє значення = не використовується
  IPAddress addr;
  return addr.fromString(value);
}

// Повертає порожній рядок у разі успіху або текст помилки.
static String saveConfigFromJson(const String &json) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, json);
  if (err) return String("некоректний JSON: ") + err.c_str();

  String ssid = doc["ssid"] | "";
  if (ssid.isEmpty()) return "не вказано ssid";
  if (ssid.length() > 32) return "ssid задовгий (максимум 32 символи)";

  String password = doc["password"] | "";
  if (password.length() > 64) return "пароль задовгий (максимум 64 символи)";

  long port = doc["port"] | (long)DEFAULT_HTTP_PORT;
  if (port < 1 || port > 65535) return "порт має бути в діапазоні 1..65535";

  String ip = doc["ip"] | "";
  String gateway = doc["gateway"] | "";
  String subnet = doc["subnet"] | "";
  String dns = doc["dns"] | "";
  if (!isValidIp(ip) || !isValidIp(gateway) || !isValidIp(subnet) || !isValidIp(dns)) {
    return "некоректна IP-адреса в налаштуваннях мережі";
  }
  if (!ip.isEmpty() && (gateway.isEmpty() || subnet.isEmpty())) {
    return "для статичного IP потрібні також gateway та subnet";
  }

  String hostname = doc["hostname"] | "svitlobot";
  if (hostname.isEmpty()) hostname = "svitlobot";

  prefs.begin("svb", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", password);
  prefs.putString("key", String(doc["key"] | ""));
  prefs.putString("host", hostname);
  prefs.putString("ip", ip);
  prefs.putString("gw", gateway);
  prefs.putString("mask", subnet);
  prefs.putString("dns", dns);
  prefs.putUShort("port", (uint16_t)port);
  prefs.end();
  return "";
}

static void clearConfig() {
  prefs.begin("svb", false);
  prefs.clear();
  prefs.end();
}

// ---------------------------------------------------------------------------
// Протокол налаштування через UART
// ---------------------------------------------------------------------------

static void printInfo() {
  JsonDocument doc;
  doc["version"] = FW_VERSION;
  doc["configured"] = !cfg.ssid.isEmpty();
  doc["ssid"] = cfg.ssid;
  doc["port"] = cfg.port;
  doc["hostname"] = cfg.hostname;
  doc["key_set"] = !cfg.key.isEmpty();
  doc["static_ip"] = cfg.ip;
  doc["connected"] = wifiConnected;
  doc["ip"] = wifiConnected ? WiFi.localIP().toString() : "";
  doc["rssi"] = wifiConnected ? WiFi.RSSI() : 0;
  doc["mac"] = WiFi.macAddress();
  Serial.print("SVB:INFO ");
  serializeJson(doc, Serial);
  Serial.println();
}

static void handleSerialLine(String line) {
  line.trim();
  if (line.isEmpty()) return;

  if (line == "SVB:PING") {
    Serial.println("SVB:PONG");
  } else if (line == "SVB:GET") {
    printInfo();
  } else if (line.startsWith("SVB:SET ")) {
    String error = saveConfigFromJson(line.substring(8));
    if (error.isEmpty()) {
      Serial.println("SVB:OK");
      Serial.flush();
      delay(300);
      ESP.restart();
    } else {
      Serial.println("SVB:ERR " + error);
    }
  } else if (line == "SVB:CLEAR") {
    clearConfig();
    Serial.println("SVB:OK");
    Serial.flush();
    delay(300);
    ESP.restart();
  } else if (line.startsWith("SVB:")) {
    Serial.println("SVB:ERR невідома команда");
  }
}

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (!serialBuf.isEmpty()) {
        handleSerialLine(serialBuf);
        serialBuf = "";
      }
    } else if (serialBuf.length() < 1024) {
      serialBuf += c;
    } else {
      // Захист від переповнення: відкидаємо надто довгий рядок.
      serialBuf = "";
      Serial.println("SVB:ERR рядок задовгий");
    }
  }
}

// ---------------------------------------------------------------------------
// HTTP-сервер
// ---------------------------------------------------------------------------

static bool checkAccessKey() {
  if (cfg.key.isEmpty()) return true;
  String provided = server->hasArg("key") ? server->arg("key") : server->header("X-Svitlo-Key");
  if (provided == cfg.key) return true;
  server->send(403, "application/json; charset=utf-8",
               "{\"status\":\"forbidden\",\"message\":\"Невірний ключ доступу\"}");
  return false;
}

static void handleStatus() {
  if (!checkAccessKey()) return;
  JsonDocument doc;
  doc["status"] = "ok";
  doc["device"] = DEVICE_ID;
  doc["version"] = FW_VERSION;
  doc["uptime_s"] = (uint32_t)(millis() / 1000);
  doc["rssi"] = WiFi.RSSI();
  doc["message"] = "Світло є";
  String body;
  serializeJson(doc, body);
  server->sendHeader("Cache-Control", "no-store");
  server->send(200, "application/json; charset=utf-8", body);
}

static void handleNotFound() {
  server->send(404, "application/json; charset=utf-8",
               "{\"status\":\"not_found\",\"message\":\"Сторінку не знайдено\"}");
}

static void startHttpServer() {
  server = new WebServer(cfg.port);
  static const char *headerKeys[] = {"X-Svitlo-Key"};
  server->collectHeaders(headerKeys, 1);
  server->on("/", HTTP_GET, handleStatus);
  server->on("/status", HTTP_GET, handleStatus);
  server->onNotFound(handleNotFound);
  server->begin();
}

// ---------------------------------------------------------------------------
// Wi-Fi
// ---------------------------------------------------------------------------

static void startWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(cfg.hostname.c_str());
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);

  if (!cfg.ip.isEmpty()) {
    IPAddress ip, gateway, subnet, dns;
    ip.fromString(cfg.ip);
    gateway.fromString(cfg.gateway);
    subnet.fromString(cfg.subnet);
    if (cfg.dns.isEmpty() || !dns.fromString(cfg.dns)) dns = gateway;
    WiFi.config(ip, gateway, subnet, dns);
  }

  Serial.println("SVB:CONNECTING " + cfg.ssid);
  WiFi.begin(cfg.ssid.c_str(), cfg.password.c_str());
  wifiStartedAt = millis();
  wifiLostSince = millis();
}

static void watchWifi() {
  bool connected = WiFi.status() == WL_CONNECTED;

  if (connected && !wifiConnected) {
    wifiConnected = true;
    wifiTimeoutReported = false;
    digitalWrite(LED_PIN, HIGH);
    Serial.println("SVB:IP " + WiFi.localIP().toString());
    Serial.printf("Підключено до Wi-Fi, HTTP-сторінка: http://%s:%u/status\n",
                  WiFi.localIP().toString().c_str(), cfg.port);
  } else if (!connected && wifiConnected) {
    wifiConnected = false;
    wifiLostSince = millis();
    digitalWrite(LED_PIN, LOW);
    Serial.println("Втрачено з'єднання з Wi-Fi, перепідключення...");
  }

  if (!connected) {
    if (!wifiTimeoutReported && millis() - wifiStartedAt > WIFI_REPORT_TIMEOUT_MS) {
      wifiTimeoutReported = true;
      Serial.println("SVB:WIFI_TIMEOUT");
    }
    if (millis() - wifiLostSince > WIFI_RESTART_TIMEOUT_MS) {
      Serial.println("Wi-Fi недоступний понад 5 хвилин — перезавантаження");
      Serial.flush();
      ESP.restart();
    }
  }
}

// ---------------------------------------------------------------------------
// Основний цикл
// ---------------------------------------------------------------------------

void setup() {
  Serial.setRxBufferSize(1024);
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  delay(200);

  Serial.println();
  Serial.println("SVB:READY " FW_VERSION);
  loadConfig();

  if (cfg.ssid.isEmpty()) {
    Serial.println("SVB:NOCONFIG");
    Serial.println("Налаштування відсутні. Скористайтеся: svitlo-flasher configure");
    return;
  }

  startWifi();
  startHttpServer();
}

void loop() {
  pollSerial();

  if (cfg.ssid.isEmpty()) {
    // Режим очікування налаштувань: часте блимання світлодіодом.
    digitalWrite(LED_PIN, (millis() / 250) % 2);
    delay(10);
    return;
  }

  watchWifi();
  if (server) server->handleClient();
  delay(2);
}
