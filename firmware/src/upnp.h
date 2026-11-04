// Автоматичний проброс порту на роутері через UPnP IGD.
#pragma once

#include <Arduino.h>

// Скинути стан і запланувати проброс (викликати після кожного підключення до Wi-Fi).
void upnpReset();
// Викликати в loop(): виконує проброс і періодично його поновлює.
void upnpLoop();

bool upnpOk();
uint16_t upnpPort();        // зовнішній порт, відкритий на роутері (0 — немає)
String upnpExternalIp();    // зовнішній IP роутера за даними UPnP
String upnpStatusText();    // опис поточного стану українською
