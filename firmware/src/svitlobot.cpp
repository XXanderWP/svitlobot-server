/*
 * Резервний пінг Світлобота: ESP32 сама раз на 3 хвилини викликає
 *   GET https://api.svitlobot.in.ua/channelPing?channel_key=<ключ>
 * але ЛИШЕ тоді, коли VPS не пінгує Світлобот сам. Світлобот приймає не частіше одного
 * запиту на хвилину на канал (інакше HTTP 429), тому два джерела одночасно заважали б одне одному.
 * Про те, що VPS пінгує, ESP32 дізнається із заголовка X-Svitlo-Upstream: 1 у перевірках сторінки
 * (режим upnp) або з поля "upstream" у відповіді на стук (режим push). Якщо такої ознаки не було
 * понад 7 хвилин — VPS вважається недоступним і ESP32 пінгує сама.
 *
 * Сертифікат сервера не перевіряється (setInsecure): у прошивці немає сховища кореневих
 * сертифікатів, а ключ каналу дає лише право надсилати пінги.
 */

#include "svitlobot.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "config.h"
#include "console.h"

namespace {

const char *PING_URL = "https://api.svitlobot.in.ua/channelPing?channel_key=";
const uint32_t PING_INTERVAL_MS = 3UL * 60UL * 1000UL;
// Затримка першого пінгу: за цей час VPS встигає перевірити ESP32 і повідомити, що пінгує сам.
const uint32_t FIRST_PING_DELAY_MS = 90UL * 1000UL;
const uint32_t UPSTREAM_VALID_MS = 7UL * 60UL * 1000UL;

bool started = false;
bool lastOk = false;
bool announced = false;
String statusText = "вимкнено";
uint32_t nextAt = 0;
uint32_t upstreamAt = 0;
bool upstreamSeen = false;
uint32_t lastPingAt = 0;
bool pingedOnce = false;

bool upstreamFresh() { return upstreamSeen && millis() - upstreamAt < UPSTREAM_VALID_MS; }

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
    Console.println(String("SVB:SVITLOBOT ") + (ok ? "OK " : "FAIL ") + text);
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
  lastPingAt = millis();
  pingedOnce = true;

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

void svitlobotNoteUpstream(bool upstream) {
  if (!upstream) return;
  upstreamAt = millis();
  upstreamSeen = true;
}

void svitlobotLoop() {
  if (!started || WiFi.status() != WL_CONNECTED) return;
  if ((int32_t)(millis() - nextAt) < 0) return;
  nextAt = millis() + PING_INTERVAL_MS;
  if (upstreamFresh()) {
    report(true, "очікування: VPS сам пінгує Світлобот");
    return;
  }
  sendPing();
}

bool svitlobotEnabled() { return !cfg.svitlobotKey.isEmpty(); }
bool svitlobotActive() { return svitlobotEnabled() && !upstreamFresh(); }
long svitlobotLastPingAgo() { return pingedOnce ? (long)((millis() - lastPingAt) / 1000) : -1; }
uint32_t svitlobotIntervalS() { return PING_INTERVAL_MS / 1000; }

void svitlobotFillJson(JsonObject obj) {
  obj["enabled"] = svitlobotEnabled();
  if (!svitlobotEnabled()) return;
  obj["active"] = svitlobotActive();
  long ago = svitlobotLastPingAgo();
  if (ago >= 0) {
    obj["last_ping_s"] = ago;
  } else {
    obj["last_ping_s"] = nullptr;
  }
  obj["interval_s"] = svitlobotIntervalS();
  obj["ok"] = lastOk;
  obj["status"] = statusText;
}
bool svitlobotOk() { return lastOk; }
String svitlobotStatusText() { return statusText; }
