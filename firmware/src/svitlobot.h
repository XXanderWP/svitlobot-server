// Резервний пінг Світлобота безпосередньо з ESP32.
// Працює, якщо під час прошивки вказано ключ каналу (--svitlobot-token): тоді канал
// показуватиме «світло є», навіть якщо сервіс на VPS недоступний.
#pragma once

#include <Arduino.h>

// Викликати після підключення до Wi-Fi.
void svitlobotStart();
// Викликати в loop(): надсилає пінг раз на хвилину.
void svitlobotLoop();

bool svitlobotEnabled();
bool svitlobotOk();
String svitlobotStatusText();
