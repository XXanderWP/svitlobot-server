/*
 * Прошивка SvitloBot для ESP32.
 *
 * Призначення: ESP32 живиться від мережі (без ДБЖ) і сигналізує серверу, що світло є.
 * Два режими роботи:
 *   upnp — піднімає HTTP-сторінку та сам відкриває для неї порт на роутері (UPnP);
 *          сервіс на VPS періодично звертається до сторінки;
 *   push — сам періодично «стукає» на сервер підписаним запитом (проброс порту не потрібен).
 *
 * Налаштування зберігаються в NVS і передаються через UART утилітою svitlo-flasher.
 * Опис протоколу — в AGENT.md.
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>

#include "config.h"
#include "push.h"
#include "svitlobot.h"
#include "upnp.h"

#ifndef FW_VERSION
#define FW_VERSION "0.0.0"
#endif

#ifndef LED_PIN
#define LED_PIN 2
#endif

// Якщо Wi-Fi недоступний довше за цей час — перезавантажуємося.
static const uint32_t WIFI_RESTART_TIMEOUT_MS = 5UL * 60UL * 1000UL;
// Через скільки після початку підключення повідомити CLI про тайм-аут.
static const uint32_t WIFI_REPORT_TIMEOUT_MS = 30UL * 1000UL;

Config cfg;
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
  cfg.mode = prefs.getString("mode", MODE_UPNP);
  cfg.upnpPorts = prefs.getString("uports", DEFAULT_UPNP_PORTS);
  cfg.server = prefs.getString("srv", "");
  cfg.serverPort = prefs.getUShort("sport", DEFAULT_SERVER_PORT);
  cfg.pushInterval = prefs.getUShort("pint", DEFAULT_PUSH_INTERVAL);
  cfg.svitlobotKey = prefs.getString("svbkey", "");
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

  String key = doc["key"] | "";
  String mode = doc["mode"] | MODE_UPNP;
  if (mode != MODE_UPNP && mode != MODE_PUSH) return "режим має бути upnp або push";

  String upnpPorts = doc["upnp_ports"] | DEFAULT_UPNP_PORTS;
  int portCount = 0;
  String list = upnpPorts + ",";
  int start = 0;
  while (start < (int)list.length()) {
    int comma = list.indexOf(',', start);
    String item = list.substring(start, comma);
    start = comma + 1;
    item.trim();
    if (item.isEmpty()) continue;
    long value = item.toInt();
    if (value < 1 || value > 65535) return "некоректний порт у списку upnp_ports: " + item;
    portCount++;
  }
  if (mode == MODE_UPNP && portCount == 0) return "список upnp_ports порожній";
  if (portCount > 10) return "у списку upnp_ports не більше 10 портів";

  String server = doc["server"] | "";
  server.trim();
  long serverPort = doc["server_port"] | (long)DEFAULT_SERVER_PORT;
  long pushInterval = doc["push_interval"] | (long)DEFAULT_PUSH_INTERVAL;
  if (serverPort < 1 || serverPort > 65535) return "server_port має бути в діапазоні 1..65535";
  if (pushInterval < 5 || pushInterval > 300) return "push_interval має бути від 5 до 300 секунд";
  // Ключ Світлобота: поле відсутнє — залишаємо збережений, порожній рядок — вимикаємо.
  bool svitlobotProvided = doc["svitlobot_key"].is<const char *>();
  String svitlobotKey = doc["svitlobot_key"] | "";
  svitlobotKey.trim();
  if (svitlobotKey.length() > 64) return "ключ Світлобота задовгий";

  if (mode == MODE_PUSH) {
    if (server.isEmpty()) return "для режиму push потрібна адреса сервера (server)";
    if (key.length() < 16) return "для режиму push потрібен ключ щонайменше з 16 символів";
  }

  prefs.begin("svb", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", password);
  prefs.putString("key", key);
  prefs.putString("host", hostname);
  prefs.putString("ip", ip);
  prefs.putString("gw", gateway);
  prefs.putString("mask", subnet);
  prefs.putString("dns", dns);
  prefs.putUShort("port", (uint16_t)port);
  prefs.putString("mode", mode);
  prefs.putString("uports", upnpPorts);
  prefs.putString("srv", server);
  prefs.putUShort("sport", (uint16_t)serverPort);
  prefs.putUShort("pint", (uint16_t)pushInterval);
  if (svitlobotProvided) prefs.putString("svbkey", svitlobotKey);
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
  doc["mode"] = cfg.mode;
  doc["svitlobot_set"] = svitlobotEnabled();
  if (svitlobotEnabled()) {
    doc["svitlobot_ok"] = svitlobotOk();
    doc["svitlobot_status"] = svitlobotStatusText();
  }
  if (cfg.mode == MODE_UPNP) {
    doc["upnp_ports"] = cfg.upnpPorts;
    doc["upnp_ok"] = upnpOk();
    doc["upnp_port"] = upnpPort();
    doc["upnp_external_ip"] = upnpExternalIp();
    doc["upnp_status"] = upnpStatusText();
  } else {
    doc["server"] = cfg.server;
    doc["server_port"] = cfg.serverPort;
    doc["push_interval"] = cfg.pushInterval;
    doc["push_ok"] = pushOk();
    doc["push_status"] = pushStatusText();
  }
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
  svitlobotNoteUpstream(server->header("X-Svitlo-Upstream") == "1");
  JsonDocument doc;
  doc["status"] = "ok";
  doc["device"] = DEVICE_ID;
  doc["version"] = FW_VERSION;
  doc["uptime_s"] = (uint32_t)(millis() / 1000);
  doc["rssi"] = WiFi.RSSI();
  doc["mode"] = cfg.mode;
  if (cfg.mode == MODE_UPNP) doc["upnp_port"] = upnpPort();
  if (svitlobotEnabled()) {
    JsonObject svb = doc["svitlobot"].to<JsonObject>();
    svb["ok"] = svitlobotOk();
    svb["status"] = svitlobotStatusText();
  }
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
  static const char *headerKeys[] = {"X-Svitlo-Key", "X-Svitlo-Upstream"};
  server->collectHeaders(headerKeys, 2);
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
    if (cfg.mode == MODE_UPNP) {
      upnpReset();
    } else {
      pushStart();
    }
    svitlobotStart();
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
  upnpLoop();
  pushLoop();
  svitlobotLoop();
  delay(2);
}
