#pragma once
// Copy to secrets.h and fill in. secrets.h is ignored by git.

#define WIFI_SSID     "your-wifi-name"
#define WIFI_PASS     "your-wifi-password"

#define MQTT_USER     "esp"
#define MQTT_PASS     "your-mqtt-password"

// Wi-Fi the Pi creates in Ethernet mode (setup-network.sh). Leave empty if unused.
#define AP_PASS       ""

// Optional fixed hub IP, used only if mDNS (iothub.local) and the gateway fail.
// #define MQTT_HOST  "192.168.1.50"

// Onboard RGB LED: 48 on ESP32-S3-DevKitM-1 / DevKitC-1 v1.1, 38 on DevKitC-1 v1.0
#define LED_PIN       48

// Sensor: AM2305B on GPIO 1 by default. For a DS18B20 instead:
// #define USE_DS18B20
// #define SENSOR_PIN 1
