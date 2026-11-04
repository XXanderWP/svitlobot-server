/*
 * Режим «стуку»: ESP32 раз на cfg.pushInterval секунд надсилає на сервер
 *   POST http://<cfg.server>:<cfg.serverPort>/hb
 * з тілом JSON і заголовками:
 *   X-Svitlo-Ts:  час відправлення (Unix, секунди, UTC)
 *   X-Svitlo-Sig: HMAC-SHA256(ключ, "<ts>\n<тіло>") у hex
 * Сервер перевіряє підпис і свіжість часу, тому підробити чи повторити стук не вийде.
 */

#include "push.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <time.h>

#include "config.h"
#include "console.h"
#include "mbedtls/md.h"
#include "svitlobot.h"

#ifndef FW_VERSION
#define FW_VERSION "0.0.0"
#endif

namespace {

const time_t MIN_VALID_TIME = 1700000000;  // будь-який час до 2023 року — годинник ще не синхронізовано

bool started = false;
bool lastOk = false;
bool announced = false;
String statusText = "очікування";
uint32_t nextAt = 0;

String hmacSha256Hex(const String &key, const String &message) {
  uint8_t digest[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char *)key.c_str(), key.length());
  mbedtls_md_hmac_update(&ctx, (const unsigned char *)message.c_str(), message.length());
  mbedtls_md_hmac_finish(&ctx, digest);
  mbedtls_md_free(&ctx);

  static const char *hex = "0123456789abcdef";
  String out;
  out.reserve(64);
  for (uint8_t b : digest) {
    out += hex[b >> 4];
    out += hex[b & 0x0f];
  }
  return out;
}

void report(bool ok, const String &text) {
  if (!announced || ok != lastOk || text != statusText) {
    Console.println(String("SVB:PUSH ") + (ok ? "OK " : "FAIL ") + text);
  }
  announced = true;
  lastOk = ok;
  statusText = text;
}

void sendHeartbeat() {
  time_t now = time(nullptr);
  if (now < MIN_VALID_TIME) {
    report(false, "очікування синхронізації часу (NTP)");
    nextAt = millis() + 2000;
    return;
  }

  JsonDocument doc;
  doc["device"] = DEVICE_ID;
  doc["version"] = FW_VERSION;
  doc["uptime_s"] = (uint32_t)(millis() / 1000);
  doc["rssi"] = WiFi.RSSI();
  doc["ip"] = WiFi.localIP().toString();
  svitlobotFillJson(doc["svitlobot"].to<JsonObject>());
  String body;
  serializeJson(doc, body);

  String ts = String((uint32_t)now);
  String signature = hmacSha256Hex(cfg.key, ts + "\n" + body);

  HTTPClient http;
  http.setTimeout(5000);
  http.setConnectTimeout(5000);
  String url = "http://" + cfg.server + ":" + String(cfg.serverPort) + "/hb";
  if (!http.begin(url)) {
    report(false, "некоректна адреса сервера");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Svitlo-Ts", ts);
  http.addHeader("X-Svitlo-Sig", signature);
  int code = http.POST(body);
  String response = code == 200 ? http.getString() : "";
  http.end();

  if (code == 200) {
    JsonDocument reply;
    if (!deserializeJson(reply, response)) svitlobotNoteUpstream(reply["upstream"] | false);
    report(true, "сервер прийняв стук");
  } else if (code == 401 || code == 403) {
    report(false, "сервер відхилив підпис — перевірте ключ (HTTP " + String(code) + ")");
  } else if (code < 0) {
    report(false, "сервер недоступний: " + HTTPClient::errorToString(code));
  } else {
    report(false, "неочікувана відповідь сервера (HTTP " + String(code) + ")");
  }
}

}  // namespace

void pushStart() {
  if (!started) {
    configTime(0, 0, "pool.ntp.org", "time.cloudflare.com", "time.google.com");
    started = true;
  }
  nextAt = millis() + 1000;
}

void pushLoop() {
  if (cfg.mode != MODE_PUSH || !started || WiFi.status() != WL_CONNECTED) return;
  if ((int32_t)(millis() - nextAt) < 0) return;
  nextAt = millis() + (uint32_t)cfg.pushInterval * 1000UL;
  sendHeartbeat();
}

bool pushOk() { return lastOk; }
String pushStatusText() { return statusText; }
