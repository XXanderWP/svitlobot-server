#include "console.h"

DualConsole Console;

void DualConsole::begin() {
  Serial.setRxBufferSize(1024);
  Serial.begin(115200);
#if SVB_HAS_USB_SERIAL
  // Без хоста (живлення від блока) запис у USB не має блокувати роботу прошивки.
  USBSerial.setTxTimeoutMs(0);
  USBSerial.setRxBufferSize(1024);
  USBSerial.begin();
#endif
}

size_t DualConsole::write(uint8_t c) { return write(&c, 1); }

size_t DualConsole::write(const uint8_t *buffer, size_t size) {
  Serial.write(buffer, size);
#if SVB_HAS_USB_SERIAL
  if (USBSerial) USBSerial.write(buffer, size);
#endif
  return size;
}

void DualConsole::flush() {
  Serial.flush();
#if SVB_HAS_USB_SERIAL
  if (USBSerial) USBSerial.flush();
#endif
}
