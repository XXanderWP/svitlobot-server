// Спільні налаштування прошивки SvitloBot.
#pragma once

#include <Arduino.h>

// Ідентифікатор пристрою: сервер перевіряє його у відповіді, щоб не сплутати
// ESP32 зі сторінкою роутера чи іншого пристрою на тому ж порту.
static const char *DEVICE_ID = "svitlobot-esp32";

static const uint16_t DEFAULT_HTTP_PORT = 8080;
static const char *DEFAULT_UPNP_PORTS = "18080,18081,18082,18083,18084";
static const uint16_t DEFAULT_SERVER_PORT = 8081;
static const uint16_t DEFAULT_PUSH_INTERVAL = 20;

// Режими роботи:
//   "upnp" — ESP32 відкриває порт на роутері через UPnP, сервер сам звертається до сторінки;
//   "push" — ESP32 сам періодично «стукає» на сервер (проброс порту не потрібен).
static const char *MODE_UPNP = "upnp";
static const char *MODE_PUSH = "push";

struct Config {
  String ssid;
  String password;
  String key;        // ключ доступу до сторінки (upnp) або ключ підпису стуків (push)
  String hostname;
  String ip;         // статичний IP (порожньо — DHCP)
  String gateway;
  String subnet;
  String dns;
  uint16_t port = DEFAULT_HTTP_PORT;  // локальний порт HTTP-сторінки
  String mode = MODE_UPNP;
  String upnpPorts = DEFAULT_UPNP_PORTS;  // зовнішні порти-кандидати через кому
  String server;                           // IP або домен сервера (режим push)
  uint16_t serverPort = DEFAULT_SERVER_PORT;
  uint16_t pushInterval = DEFAULT_PUSH_INTERVAL;  // секунд між стуками
};

extern Config cfg;
