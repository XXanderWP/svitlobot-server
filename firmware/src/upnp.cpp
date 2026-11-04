/*
 * Мінімальний клієнт UPnP IGD для автоматичного пробросу порту.
 *
 * Алгоритм:
 *  1. SSDP-пошук роутера (M-SEARCH на 239.255.255.250:1900) → адреса опису пристрою.
 *  2. Із XML-опису беремо службу WANIPConnection (або WANPPPConnection) та її controlURL.
 *  3. Перебираємо зовнішні порти зі списку cfg.upnpPorts: порт, зайнятий іншим пристроєм,
 *     пропускаємо (GetSpecificPortMappingEntry / помилка 718), перший вільний відкриваємо
 *     (AddPortMapping) на локальний cfg.port.
 *  4. Кожні 20 хвилин поновлюємо оренду; якщо порт втрачено — шукаємо знову.
 *
 * Сервер на VPS перевіряє той самий список портів, тож знати, який саме відкрився, йому не потрібно.
 */

#include "upnp.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#include "config.h"
#include "console.h"

namespace {

const uint32_t LEASE_SECONDS = 3600;
const uint32_t RENEW_INTERVAL_MS = 20UL * 60UL * 1000UL;
const uint32_t RETRY_INTERVAL_MS = 5UL * 60UL * 1000UL;
const uint32_t SSDP_WAIT_MS = 3000;
const char *MAPPING_DESCRIPTION = "SvitloBot ESP32";

String controlUrl;
String serviceType;
uint16_t mappedPort = 0;
String externalIp;
String statusText = "ще не запускався";
bool mappingOk = false;
bool permanentLeaseOnly = false;
bool resultAnnounced = false;
uint32_t nextRunAt = 0;
bool scheduled = false;

// Значення заголовка HTTP-відповіді (без урахування регістру назви).
String headerValue(const String &response, const char *name) {
  String lower = response;
  lower.toLowerCase();
  String key = String("\n") + name + ":";
  key.toLowerCase();
  int start = lower.indexOf(key);
  if (start < 0) return "";
  start += key.length();
  int end = response.indexOf('\r', start);
  if (end < 0) end = response.indexOf('\n', start);
  String value = response.substring(start, end < 0 ? response.length() : end);
  value.trim();
  return value;
}

// Вміст першого тегу <tag>…</tag> (з префіксом простору імен або без).
String tagValue(const String &xml, const String &tag, int from = 0) {
  int start = xml.indexOf("<" + tag + ">", from);
  int openLen = tag.length() + 2;
  if (start < 0) {
    int colon = xml.indexOf(":" + tag + ">", from);
    if (colon < 0) return "";
    start = colon;
    openLen = tag.length() + 2;
  }
  int valueStart = start + openLen;
  int end = xml.indexOf("</", valueStart);
  if (end < 0) return "";
  String value = xml.substring(valueStart, end);
  value.trim();
  return value;
}

bool discoverGateway(String &location) {
  WiFiUDP udp;
  if (!udp.begin(19000 + (esp_random() % 1000))) return false;

  const char *targets[] = {
      "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
      "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
      "urn:schemas-upnp-org:service:WANIPConnection:1",
      "urn:schemas-upnp-org:service:WANPPPConnection:1",
  };
  IPAddress multicast(239, 255, 255, 250);
  for (const char *target : targets) {
    udp.beginPacket(multicast, 1900);
    udp.print("M-SEARCH * HTTP/1.1\r\n"
              "HOST: 239.255.255.250:1900\r\n"
              "MAN: \"ssdp:discover\"\r\n"
              "MX: 2\r\n"
              "ST: ");
    udp.print(target);
    udp.print("\r\n\r\n");
    udp.endPacket();
    delay(20);
  }

  uint32_t until = millis() + SSDP_WAIT_MS;
  char buf[1024];
  while ((int32_t)(millis() - until) < 0) {
    int size = udp.parsePacket();
    if (size > 0) {
      int len = udp.read(buf, sizeof(buf) - 1);
      if (len > 0) {
        buf[len] = 0;
        String loc = headerValue(String(buf), "location");
        if (loc.startsWith("http://")) {
          udp.stop();
          location = loc;
          return true;
        }
      }
    }
    delay(10);
  }
  udp.stop();
  return false;
}

String baseUrl(const String &url) {
  int hostStart = url.indexOf("://");
  if (hostStart < 0) return url;
  int pathStart = url.indexOf('/', hostStart + 3);
  return pathStart < 0 ? url : url.substring(0, pathStart);
}

// Знаходить службу керування пробросом в описі роутера.
bool findService(const String &description, const String &location) {
  const char *candidates[] = {"WANIPConnection:2", "WANIPConnection:1", "WANPPPConnection:1"};
  for (const char *candidate : candidates) {
    String type = String("urn:schemas-upnp-org:service:") + candidate;
    int typePos = description.indexOf(type);
    if (typePos < 0) continue;
    int serviceEnd = description.indexOf("</service>", typePos);
    String url = tagValue(description, "controlURL", typePos);
    int urlPos = description.indexOf("controlURL", typePos);
    if (url.isEmpty() || (serviceEnd >= 0 && urlPos > serviceEnd)) continue;
    if (!url.startsWith("http")) {
      url = baseUrl(location) + (url.startsWith("/") ? "" : "/") + url;
    }
    controlUrl = url;
    serviceType = type;
    return true;
  }
  return false;
}

int soapCall(const String &action, const String &args, String &response) {
  HTTPClient http;
  http.setTimeout(5000);
  if (!http.begin(controlUrl)) return -1;
  http.addHeader("Content-Type", "text/xml; charset=\"utf-8\"");
  http.addHeader("SOAPAction", "\"" + serviceType + "#" + action + "\"");
  String body =
      "<?xml version=\"1.0\"?>"
      "<s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\" "
      "s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\"><s:Body>"
      "<u:" + action + " xmlns:u=\"" + serviceType + "\">" + args + "</u:" + action + ">"
      "</s:Body></s:Envelope>";
  int code = http.POST(body);
  response = code > 0 ? http.getString() : "";
  http.end();
  return code;
}

int soapErrorCode(const String &response) { return tagValue(response, "errorCode").toInt(); }

String portArgs(uint16_t port) {
  return "<NewRemoteHost></NewRemoteHost><NewExternalPort>" + String(port) +
         "</NewExternalPort><NewProtocol>TCP</NewProtocol>";
}

// Пробує відкрити (або поновити) зовнішній порт. У разі невдачі пише причину в error.
bool tryMapPort(uint16_t port, String &error) {
  String myIp = WiFi.localIP().toString();
  String response;

  int code = soapCall("GetSpecificPortMappingEntry", portArgs(port), response);
  if (code == 200) {
    String client = tagValue(response, "NewInternalClient");
    long internalPort = tagValue(response, "NewInternalPort").toInt();
    if (!client.isEmpty() && (client != myIp || internalPort != cfg.port)) {
      error = "зайнятий іншим пристроєм (" + client + ":" + String(internalPort) + ")";
      return false;
    }
  }

  for (int attempt = 0; attempt < 2; attempt++) {
    uint32_t lease = permanentLeaseOnly ? 0 : LEASE_SECONDS;
    String args = portArgs(port) + "<NewInternalPort>" + String(cfg.port) + "</NewInternalPort>" +
                  "<NewInternalClient>" + myIp + "</NewInternalClient>" +
                  "<NewEnabled>1</NewEnabled>" +
                  "<NewPortMappingDescription>" + MAPPING_DESCRIPTION + "</NewPortMappingDescription>" +
                  "<NewLeaseDuration>" + String(lease) + "</NewLeaseDuration>";
    code = soapCall("AddPortMapping", args, response);
    if (code == 200) return true;

    int err = soapErrorCode(response);
    if (err == 725 && !permanentLeaseOnly) {
      // Роутер підтримує лише безстрокові правила — повторюємо з NewLeaseDuration=0.
      permanentLeaseOnly = true;
      continue;
    }
    if (err == 718) {
      error = "зайнятий (помилка 718)";
    } else if (err == 606) {
      error = "роутер заборонив зміну правил (помилка 606)";
    } else if (err > 0) {
      error = "помилка роутера " + String(err);
    } else {
      error = "немає відповіді від роутера (HTTP " + String(code) + ")";
    }
    return false;
  }
  error = "не вдалося задати тривалість оренди";
  return false;
}

bool isPrivateOrCgnat(const String &ipText) {
  IPAddress ip;
  if (!ip.fromString(ipText)) return true;
  if (ip[0] == 10 || ip[0] == 127 || ip[0] == 0) return true;
  if (ip[0] == 172 && ip[1] >= 16 && ip[1] <= 31) return true;
  if (ip[0] == 192 && ip[1] == 168) return true;
  if (ip[0] == 100 && ip[1] >= 64 && ip[1] <= 127) return true;  // CGNAT 100.64.0.0/10
  return false;
}

void scheduleIn(uint32_t ms) {
  nextRunAt = millis() + ms;
  scheduled = true;
}

void reportFailure(const String &reason) {
  bool changed = mappingOk || statusText != reason || !resultAnnounced;
  mappingOk = false;
  mappedPort = 0;
  statusText = reason;
  controlUrl = "";  // наступного разу шукаємо роутер заново
  if (changed) Console.println("SVB:UPNP FAIL " + reason);
  resultAnnounced = true;
  scheduleIn(RETRY_INTERVAL_MS);
}

void reportSuccess(uint16_t port) {
  bool changed = !mappingOk || port != mappedPort || !resultAnnounced;
  mappingOk = true;
  mappedPort = port;

  String response;
  if (soapCall("GetExternalIPAddress", "", response) == 200) {
    externalIp = tagValue(response, "NewExternalIPAddress");
  }
  statusText = "відкрито зовнішній порт " + String(port);
  if (changed) {
    Console.println("SVB:UPNP OK " + String(port) + " " + (externalIp.isEmpty() ? "-" : externalIp));
    if (!externalIp.isEmpty() && isPrivateOrCgnat(externalIp)) {
      Console.println("SVB:UPNP_CGNAT " + externalIp);
    }
  }
  resultAnnounced = true;
  scheduleIn(RENEW_INTERVAL_MS);
}

void runUpnp() {
  if (controlUrl.isEmpty()) {
    String location;
    if (!discoverGateway(location)) {
      reportFailure("роутер не відповідає на UPnP (вимкнено або не підтримується)");
      return;
    }
    HTTPClient http;
    http.setTimeout(5000);
    if (!http.begin(location)) {
      reportFailure("некоректна адреса опису роутера");
      return;
    }
    int code = http.GET();
    String description = code == 200 ? http.getString() : "";
    http.end();
    if (description.isEmpty()) {
      reportFailure("не вдалося отримати опис роутера (HTTP " + String(code) + ")");
      return;
    }
    if (!findService(description, location)) {
      reportFailure("роутер не надає службу керування портами (WANIPConnection)");
      return;
    }
  }

  String error;
  String lastError;
  // Спершу поновлюємо вже відкритий порт.
  if (mappedPort && tryMapPort(mappedPort, error)) {
    reportSuccess(mappedPort);
    return;
  }

  String list = cfg.upnpPorts + ",";
  int start = 0;
  while (start < (int)list.length()) {
    int comma = list.indexOf(',', start);
    String item = list.substring(start, comma);
    start = comma + 1;
    item.trim();
    long port = item.toInt();
    if (port < 1 || port > 65535) continue;
    if (tryMapPort((uint16_t)port, error)) {
      reportSuccess((uint16_t)port);
      return;
    }
    Console.printf("UPnP: порт %ld — %s\n", port, error.c_str());
    lastError = error;
  }
  reportFailure("не вдалося відкрити жоден порт зі списку: " + lastError);
}

}  // namespace

void upnpReset() {
  controlUrl = "";
  mappingOk = false;
  resultAnnounced = false;
  statusText = "очікування";
  scheduleIn(2000);
}

void upnpLoop() {
  if (cfg.mode != MODE_UPNP || WiFi.status() != WL_CONNECTED || !scheduled) return;
  if ((int32_t)(millis() - nextRunAt) < 0) return;
  runUpnp();
}

bool upnpOk() { return mappingOk; }
uint16_t upnpPort() { return mappingOk ? mappedPort : 0; }
String upnpExternalIp() { return externalIp; }
String upnpStatusText() { return statusText; }
