// Indoor Tracker - ESP32 firmware (USB serial link + optional BLE)
// Libraries: "DHT sensor library" (Adafruit) + "Adafruit Unified Sensor",
//            LiquidCrystal_I2C, NimBLE-Arduino 2.x (only if USE_BLE = 1)
// Board package: ESP32 Arduino core 3.x
//
// Wiring:
//   DHT22 data -> GPIO 4  (3.3V, GND, 10k pull-up to 3.3V if the module has none)
//   LCD I2C    -> SDA GPIO 21, SCL GPIO 22 (try address 0x3F if 0x27 stays blank)
//   Piezo      -> GPIO 26
//   LED        -> GPIO 27 (+ 220 ohm resistor)
//   Button     -> GPIO 13 to GND (internal pull-up)
//
// USB serial protocol (115200 baud, one message per line)
//   ESP32 -> laptop (JSON):
//     {"type":"ready"}
//     {"type":"env","t":23.4,"h":41.0}
//     {"type":"status","event":"ALARM_ON"}   // ALARM_ON | ALARM_OFF | BTN
//   Laptop -> ESP32 (plain text):
//     TIME <local epoch seconds>
//     SCHED <minutes after midnight>
//     ACK            (stop the alarm)
//     PING           (heartbeat, also replies with a fresh env line)

#define USE_BLE 0          // set to 0 for USB-only, set 1 to activate

#include <Arduino.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <DHT.h>
#include <Preferences.h>
#if USE_BLE
#include <NimBLEDevice.h>
#endif

// ---------- Pins ----------
#define DHTPIN   4
#define DHTTYPE  DHT22
#define PIEZO    26
#define LED      27
#define BUTTON   13
#define I2C_SDA  21
#define I2C_SCL  22

// ---------- BLE UUIDs (must match the React Native app) ----------
#define SVC_UUID   "12345678-1234-1234-1234-123456789abc"
#define ENV_UUID   "12345678-1234-1234-1234-123456789ab1"
#define ACK_UUID   "12345678-1234-1234-1234-123456789ab2"
#define TIME_UUID  "12345678-1234-1234-1234-123456789ab3"
#define SCHED_UUID "12345678-1234-1234-1234-123456789ab4"
#define STAT_UUID  "12345678-1234-1234-1234-123456789ab5"

// ---------- Hardware objects ----------
DHT dht(DHTPIN, DHTTYPE);
LiquidCrystal_I2C lcd(0x27, 16, 2);
Preferences prefs;

#if USE_BLE
NimBLECharacteristic *envChar, *statChar, *schedChar;
#endif
bool bleConnected = false;

// ---------- State ----------
float temperature = NAN, humidity = NAN;
bool alarmActive = false;
uint32_t alarmStartedMs = 0;
const uint32_t ALARM_TIMEOUT_MS = 10UL * 60UL * 1000UL;   // auto-stop after 10 min

bool timeSynced = false;
uint32_t baseEpoch = 0;                    // local-time epoch at millis() == 0
uint16_t reminderMinute = 20 * 60;         // default 20:00, overridden by NVS/app
int32_t lastFiredDay = -1;

uint32_t lastHostMs = 0;                   // last time the laptop sent us anything
bool hostSeen = false;

volatile bool ackRequested = false;
volatile bool scheduleChanged = false;
volatile bool timeJustSynced = false;
volatile bool sendEnvNow = false;

// ---------- Helpers ----------
uint32_t nowEpoch() { return baseEpoch + millis() / 1000; }
bool usbConnected() { return hostSeen && (millis() - lastHostMs < 12000); }

void readSensor() {
  float t = dht.readTemperature();   // Celsius
  float h = dht.readHumidity();
  if (!isnan(t) && !isnan(h)) { temperature = t; humidity = h; }
}

// ----- outputs: USB serial (always) and BLE (if enabled) -----
void emitEnvSerial() {
  if (isnan(temperature) || isnan(humidity)) return;
  Serial.printf("{\"type\":\"env\",\"t\":%.1f,\"h\":%.1f}\n", temperature, humidity);
}

void sendStatus(const char* s) {
  Serial.printf("{\"type\":\"status\",\"event\":\"%s\"}\n", s);
#if USE_BLE
  statChar->setValue(s);
  if (bleConnected) statChar->notify();
#endif
}

// ----- commands (shared by BLE and USB) -----
void applyTime(uint32_t localEpoch) {
  baseEpoch = localEpoch - millis() / 1000;
  timeSynced = true;
  timeJustSynced = true;
}
void applySchedule(int minutes) {
  if (minutes >= 0 && minutes < 1440) { reminderMinute = minutes; scheduleChanged = true; }
}

void startAlarm() {
  alarmActive = true;
  alarmStartedMs = millis();
  sendStatus("ALARM_ON");
}

void stopAlarm(const char* reason) {
  if (!alarmActive) return;
  alarmActive = false;
  digitalWrite(LED, LOW);
  ledcWriteTone(PIEZO, 0);
  sendStatus(reason);                      // "BTN" if the button did it, else "ALARM_OFF"
}

void updateLCD() {
  char l1[17], l2[17];
  if (isnan(temperature)) snprintf(l1, sizeof(l1), "Reading sensor..");
  else snprintf(l1, sizeof(l1), "Temp: %.1f%cC", temperature, (char)223);

  if (isnan(humidity)) snprintf(l2, sizeof(l2), " ");
  else if (alarmActive) snprintf(l2, sizeof(l2), "TAKE YOUR PHOTO!");
  else snprintf(l2, sizeof(l2), "Humid: %.0f%% %s", humidity,
                bleConnected ? "BLE" : (usbConnected() ? "USB" : "   "));

  lcd.setCursor(0, 0); lcd.printf("%-16s", l1);
  lcd.setCursor(0, 1); lcd.printf("%-16s", l2);
}

// ---------- USB serial command parser ----------
void handleSerialLine(String line) {
  line.trim();
  if (line.length() == 0) return;
  hostSeen = true;
  lastHostMs = millis();

  if (line.startsWith("TIME "))       applyTime(strtoul(line.c_str() + 5, nullptr, 10));
  else if (line.startsWith("SCHED ")) applySchedule(atoi(line.c_str() + 6));
  else if (line == "ACK")             ackRequested = true;
  else if (line == "PING")            sendEnvNow = true;
}

void pollSerial() {
  static String buf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') { handleSerialLine(buf); buf = ""; }
    else if (c != '\r' && buf.length() < 64) buf += c;
  }
}

// ---------- BLE ----------
#if USE_BLE
class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*, NimBLEConnInfo&) override { bleConnected = true; sendEnvNow = true; }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo&, int) override {
    bleConnected = false;
    NimBLEDevice::startAdvertising();
  }
};

class WriteCB : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c, NimBLEConnInfo&) override {
    std::string uuid = c->getUUID().toString();
    std::string val  = c->getValue();
    if (uuid == ACK_UUID)        ackRequested = true;
    else if (uuid == TIME_UUID)  applyTime(strtoul(val.c_str(), nullptr, 10));
    else if (uuid == SCHED_UUID) applySchedule(atoi(val.c_str()));
  }
};

void setupBLE() {
  NimBLEDevice::init("IndoorTracker");
  NimBLEServer* server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCB());
  NimBLEService* svc = server->createService(SVC_UUID);

  envChar  = svc->createCharacteristic(ENV_UUID,  NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY);
  statChar = svc->createCharacteristic(STAT_UUID, NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY);
  auto* ack = svc->createCharacteristic(ACK_UUID,  NIMBLE_PROPERTY::WRITE);
  auto* tm  = svc->createCharacteristic(TIME_UUID, NIMBLE_PROPERTY::WRITE);
  schedChar = svc->createCharacteristic(SCHED_UUID, NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::WRITE);

  static WriteCB writeCB;
  ack->setCallbacks(&writeCB);
  tm->setCallbacks(&writeCB);
  schedChar->setCallbacks(&writeCB);
  schedChar->setValue(String(reminderMinute).c_str());
  statChar->setValue("ALARM_OFF");

  svc->start();
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->enableScanResponse(true);
  adv->start();
}
#endif

// ---------- Setup ----------
void setup() {
  Serial.begin(115200);
  pinMode(LED, OUTPUT);
  pinMode(BUTTON, INPUT_PULLUP);
  ledcAttach(PIEZO, 2000, 8);

  Wire.begin(I2C_SDA, I2C_SCL);
  lcd.init();
  lcd.backlight();
  lcd.print("Indoor Tracker");

  dht.begin();

  prefs.begin("indoor", false);
  reminderMinute = prefs.getUShort("remind", reminderMinute);

#if USE_BLE
  setupBLE();
#endif

  delay(1500);       // DHT22 needs a moment after power-up
  readSensor();
  updateLCD();
  Serial.println("{\"type\":\"ready\"}");
}

// ---------- Loop ----------
void loop() {
  static uint32_t lastSample = 0, lastSerialEnv = 0, lastBleEnv = 0, lastBtn = 0;
  uint32_t ms = millis();

  pollSerial();

  // 1. Sample sensor + refresh LCD every 2.5 s (DHT22 max rate is ~0.5 Hz)
  if (ms - lastSample >= 2500) {
    lastSample = ms;
    readSensor();
    updateLCD();
  }

  // 2. Stream readings: USB every 3 s, BLE every 10 s, both immediately on request
  if (sendEnvNow || ms - lastSerialEnv >= 3000) {
    lastSerialEnv = ms;
    emitEnvSerial();
  }
#if USE_BLE
  if (bleConnected && !isnan(temperature) && (sendEnvNow || ms - lastBleEnv >= 10000)) {
    lastBleEnv = ms;
    char buf[24];
    snprintf(buf, sizeof(buf), "%.1f,%.1f", temperature, humidity);
    envChar->setValue(buf);
    envChar->notify();
  }
#endif
  sendEnvNow = false;

  // 3. Handle flags set by serial/BLE handlers
  if (scheduleChanged) {
    scheduleChanged = false;
    prefs.putUShort("remind", reminderMinute);
    lastFiredDay = -1;
#if USE_BLE
    schedChar->setValue(String(reminderMinute).c_str());
#endif
  }
  if (timeJustSynced) { timeJustSynced = false; lastFiredDay = -1; }
  if (ackRequested)   { ackRequested = false; stopAlarm("ALARM_OFF"); }

  // 4. Daily reminder check
  if (timeSynced && !alarmActive) {
    uint32_t t = nowEpoch();
    int32_t day = t / 86400;
    uint16_t minuteOfDay = (t % 86400) / 60;
    if (minuteOfDay == reminderMinute && day != lastFiredDay) {
      lastFiredDay = day;
      startAlarm();
    }
  }

  // 5. Alarm output (blink LED, beep piezo) + timeout
  if (alarmActive) {
    bool on = (ms / 400) % 2;
    digitalWrite(LED, on);
    ledcWriteTone(PIEZO, on ? 2500 : 0);
    if (ms - alarmStartedMs > ALARM_TIMEOUT_MS) stopAlarm("ALARM_OFF");
    updateLCD();
  }

  // 6. Manual button (debounced): silences the alarm and tells the app
  if (digitalRead(BUTTON) == LOW && ms - lastBtn > 300) {
    lastBtn = ms;
    if (alarmActive) stopAlarm("BTN");
    else sendStatus("BTN");               // app can open the camera screen anyway
  }
}
