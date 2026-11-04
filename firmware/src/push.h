// Режим «стуку»: ESP32 сам періодично надсилає підписаний запит на сервер.
#pragma once

#include <Arduino.h>

// Запустити синхронізацію часу (потрібна для підпису). Викликати після підключення до Wi-Fi.
void pushStart();
// Викликати в loop(): надсилає стук раз на cfg.pushInterval секунд.
void pushLoop();

bool pushOk();
String pushStatusText();
