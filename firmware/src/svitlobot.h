// Резервний пінг Світлобота безпосередньо з ESP32.
// Працює, якщо під час прошивки вказано ключ каналу (--svitlobot-token): тоді канал
// показуватиме «світло є», навіть якщо сервіс на VPS недоступний. Поки VPS пінгує сам,
// ESP32 мовчить, щоб не перевищити ліміт Світлобота (1 запит на хвилину).
#pragma once

#include <Arduino.h>

// Викликати після підключення до Wi-Fi.
void svitlobotStart();
// Викликати в loop(): раз на 3 хвилини надсилає пінг, якщо VPS не пінгує сам.
void svitlobotLoop();

// Сервер повідомив, що сам пінгує Світлобот (заголовок X-Svitlo-Upstream або відповідь на стук).
void svitlobotNoteUpstream(bool upstream);

bool svitlobotEnabled();
// Чи ESP32 зараз сама відповідає за пінги (VPS давно не повідомляв, що пінгує).
bool svitlobotActive();
// Скільки секунд тому ESP32 надіслала останній пінг (-1 — ще не надсилала).
long svitlobotLastPingAgo();
uint32_t svitlobotIntervalS();
bool svitlobotOk();
String svitlobotStatusText();

#include <ArduinoJson.h>
// Заповнює об'єкт стану резервного пінгу для /status і стуків (ключ не включається).
void svitlobotFillJson(JsonObject obj);
