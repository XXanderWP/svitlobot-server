/*
 * Резервний пінг Світлобота: ESP32 сам раз на хвилину викликає
 *   GET https://api.svitlobot.in.ua/channelPing?channel_key=<ключ>
 * так само, як класична прошивка. Пінги з VPS і з ESP32 не заважають одне одному:
 * поки світло є, канал просто отримує пінги з двох джерел; коли світла немає — ESP32
 * знеструмлена й мовчить, а VPS теж припиняє пінги.
 *
 * Сертифікат сервера не перевіряється (setInsecure): у прошивці немає сховища кореневих
 * сертифікатів, а ключ каналу дає лише право надсилати пінги.
 */

#include "svitlobot.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "config.h"

namespace {

const char *PING_URL = "https://api.svitlobot.in.ua/channelPing?channel_key=";
const uint32_t PING_INTERVAL_MS = 60UL * 1000UL;
const uint32_t FIRST_PING_DELAY_MS = 5000;

bool started = false;
bool lastOk = false;
bool announced = false;
String statusText = "вимкнено";
uint32_t nextAt = 0;

String urlEncode(const String &value) {
  static const char *hex = "0123456789ABCDEF";
  String out;
  for (size_t i = 0; i < value.length(); i++) {
    uint8_t c = (uint8_t)value[i];
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
      out += (char)c;
    } else {
      out += '%';
      out += hex[c >> 4];
      out += hex[c & 0x0f];
    }
  }
  return out;
}

void report(bool ok, const String &text) {
  if (!announced || ok != lastOk || text != statusText) {
    // Ключ у виводі ніколи не друкуємо.
    Serial.println(String("SVB:SVITLOBOT ") + (ok ? "OK " : "FAIL ") + text);
  }
  announced = true;
  lastOk = ok;
  statusText = text;
}

void sendPing() {
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  http.setTimeout(8000);
  http.setConnectTimeout(8000);
  if (!http.begin(client, String(PING_URL) + urlEncode(cfg.svitlobotKey))) {
    report(false, "не вдалося підготувати запит");
    return;
  }
  int code = http.GET();
  http.end();

  if (code >= 200 && code < 300) {
    report(true, "пінг прийнято (HTTP " + String(code) + ")");
  } else if (code < 0) {
    report(false, "Світлобот недоступний: " + HTTPClient::errorToString(code));
  } else {
    report(false, "Світлобот відповів HTTP " + String(code));
  }
}

}  // namespace

void svitlobotStart() {
  started = !cfg.svitlobotKey.isEmpty();
  statusText = started ? "очікування" : "вимкнено";
  nextAt = millis() + FIRST_PING_DELAY_MS;
}

void svitlobotLoop() {
  if (!started || WiFi.status() != WL_CONNECTED) return;
  if ((int32_t)(millis() - nextAt) < 0) return;
  nextAt = millis() + PING_INTERVAL_MS;
  sendPing();
}

bool svitlobotEnabled() { return !cfg.svitlobotKey.isEmpty(); }
bool svitlobotOk() { return lastOk; }
String svitlobotStatusText() { return statusText; }
