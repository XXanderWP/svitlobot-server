// Консоль пристрою: вивід дублюється в UART0 (USB-UART міст) і, на ESP32-S3,
// у рідний USB чипа (USB-Serial-JTAG). Так налаштовувати плату можна через будь-який із двох портів.
#pragma once

#include <Arduino.h>

#if ARDUINO_USB_MODE && !ARDUINO_USB_CDC_ON_BOOT
#define SVB_HAS_USB_SERIAL 1
#include "HWCDC.h"
#else
#define SVB_HAS_USB_SERIAL 0
#endif

class DualConsole : public Print {
 public:
  void begin();
  size_t write(uint8_t c) override;
  size_t write(const uint8_t *buffer, size_t size) override;
  void flush();
};

extern DualConsole Console;
