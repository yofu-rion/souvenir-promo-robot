// M5Stack Core2 / CoreS3 リモコン。Pi（robot/srr/remote.py）と Bluetooth LE でつなぐ。
//
// M5Stack は表示とボタンだけを受け持ち、何を調整しているか・動いているかは Pi が決める。
//   - EVT（notify、M5 → Pi）：押したボタン。{"c":"start"} {"c":"adj","v":-10} {"c":"mode","v":"calib"} など
//   - STATE（write、Pi → M5）：画面に出す内容。{"m":"remote"|"calib","run":true,"t":["行1","行2",...]}
// Pi と切れたら広告を出し直し、Pi が見つけてつなぎ直す。

#include <M5Unified.h>
#include <NimBLEDevice.h>
#include <ArduinoJson.h>

static const char *DEVICE_NAME = "SRR-Remote";
static const char *SERVICE_UUID = "6e3f0001-5a1e-4c1b-9f5b-7d2a1c0e5a01";
static const char *EVT_UUID = "6e3f0002-5a1e-4c1b-9f5b-7d2a1c0e5a01";
static const char *STATE_UUID = "6e3f0003-5a1e-4c1b-9f5b-7d2a1c0e5a01";

static NimBLECharacteristic *evtChar = nullptr;
static volatile bool connected = false;
static volatile bool connChanged = true;

// Pi から届いた STATE。BLE のタスクで受け、描画は loop で行う
static portMUX_TYPE stateMux = portMUX_INITIALIZER_UNLOCKED;
static std::string pendingState;
static bool hasPending = false;

// 今の表示内容
static String mode = "remote";  // remote / calib
static bool running = false;
static String lines[4];
static int lineCount = 0;

struct Button {
  int x, y, w, h;
  const char *label;
  const char *cmd;
  int value;  // adj の増減。使わないボタンは 0
  uint16_t color;
};

static const uint16_t COL_BG = TFT_BLACK;
static const uint16_t COL_TEXT = TFT_WHITE;
static const uint16_t COL_DIM = 0x7BEF;  // 灰
static const uint16_t COL_START = 0x0560;
static const uint16_t COL_STOP = 0xB800;
static const uint16_t COL_KEY = 0x2945;
static const uint16_t COL_SAVE = 0x02B5;

static const Button TABS[] = {
    {0, 0, 160, 34, "リモコン", "mode", 0, COL_KEY},
    {160, 0, 160, 34, "角度調整", "mode", 1, COL_KEY},
};
static const Button REMOTE_BTNS[] = {
    {4, 142, 102, 94, "開始", "start", 0, COL_START},
    {110, 142, 100, 94, "1回", "once", 0, COL_KEY},
    {214, 142, 102, 94, "停止", "stop", 0, COL_STOP},
};
static const Button CALIB_BTNS[] = {
    {4, 142, 75, 44, "-10", "adj", -10, COL_KEY},
    {83, 142, 75, 44, "-1", "adj", -1, COL_KEY},
    {162, 142, 75, 44, "+1", "adj", 1, COL_KEY},
    {241, 142, 75, 44, "+10", "adj", 10, COL_KEY},
    {4, 192, 100, 44, "◀ 前", "prev", 0, COL_KEY},
    {110, 192, 100, 44, "次 ▶", "next", 0, COL_KEY},
    {216, 192, 100, 44, "保存", "save", 0, COL_SAVE},
};

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *server) override {
    connected = true;
    connChanged = true;
  }
  void onDisconnect(NimBLEServer *server) override {
    connected = false;
    connChanged = true;
    NimBLEDevice::startAdvertising();
  }
};

class StateCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *c) override {
    std::string v = c->getValue();
    portENTER_CRITICAL(&stateMux);
    pendingState = v;
    hasPending = true;
    portEXIT_CRITICAL(&stateMux);
  }
};

static void sendEvent(const char *cmd, const char *strValue, int intValue, bool hasInt) {
  if (!connected || evtChar == nullptr) return;
  JsonDocument doc;
  doc["c"] = cmd;
  if (strValue) doc["v"] = strValue;
  else if (hasInt) doc["v"] = intValue;
  std::string out;
  serializeJson(doc, out);
  evtChar->setValue(out);
  evtChar->notify();
}

static void drawButton(const Button &b, bool pressed = false) {
  auto &d = M5.Display;
  uint16_t fill = pressed ? COL_TEXT : b.color;
  d.fillRoundRect(b.x, b.y, b.w, b.h, 8, fill);
  d.setTextColor(pressed ? COL_BG : COL_TEXT);
  d.setTextDatum(middle_center);
  d.setFont(b.h > 60 ? &fonts::efontJA_24 : &fonts::efontJA_16);
  d.drawString(b.label, b.x + b.w / 2, b.y + b.h / 2);
}

static void drawTabs() {
  auto &d = M5.Display;
  bool calib = mode == "calib";
  for (int i = 0; i < 2; i++) {
    const Button &t = TABS[i];
    bool active = (i == 1) == calib;
    d.fillRect(t.x, t.y, t.w, t.h, active ? 0x3186 : COL_BG);
    d.setTextColor(active ? COL_TEXT : COL_DIM);
    d.setTextDatum(middle_center);
    d.setFont(&fonts::efontJA_16);
    d.drawString(t.label, t.x + t.w / 2, t.y + t.h / 2);
  }
  d.drawFastHLine(0, 34, 320, COL_DIM);
}

static void drawBody() {
  auto &d = M5.Display;
  d.fillRect(0, 36, 320, 104, COL_BG);
  d.setTextDatum(top_left);
  if (!connected) {
    d.setFont(&fonts::efontJA_16);
    d.setTextColor(COL_DIM);
    d.drawString("Pi と未接続", 10, 50);
    d.drawString("Pi の srr run を起動して待つ", 10, 76);
    return;
  }
  for (int i = 0; i < lineCount; i++) {
    // 角度調整ではパルス幅の行（3 行目）を大きく出す
    bool big = mode == "calib" && i == 2;
    d.setFont(big ? &fonts::efontJA_24 : &fonts::efontJA_16);
    d.setTextColor(i == 0 ? COL_TEXT : (big ? TFT_YELLOW : 0xC618));
    int y = 42 + i * 24 + (mode == "calib" && i == 3 ? 6 : 0);
    d.drawString(lines[i].c_str(), 10, y);
  }
  if (mode == "remote") {
    d.fillCircle(304, 50, 7, running ? TFT_GREEN : COL_DIM);
  }
}

static void drawButtons() {
  M5.Display.fillRect(0, 140, 320, 100, COL_BG);
  if (mode == "calib") {
    for (auto &b : CALIB_BTNS) drawButton(b);
  } else {
    for (auto &b : REMOTE_BTNS) drawButton(b);
  }
}

static void drawAll() {
  M5.Display.fillScreen(COL_BG);
  drawTabs();
  drawBody();
  drawButtons();
}

static void applyState(const std::string &json) {
  JsonDocument doc;
  if (deserializeJson(doc, json)) return;
  String newMode = doc["m"] | "remote";
  running = doc["run"] | false;
  JsonArray t = doc["t"].as<JsonArray>();
  lineCount = 0;
  for (JsonVariant v : t) {
    if (lineCount >= 4) break;
    lines[lineCount++] = v.as<const char *>();
  }
  if (newMode != mode) {
    mode = newMode;
    drawAll();
  } else {
    drawBody();
  }
}

static bool hit(const Button &b, int x, int y) {
  return x >= b.x && x < b.x + b.w && y >= b.y && y < b.y + b.h;
}

static void onTouch(int x, int y) {
  for (int i = 0; i < 2; i++) {
    if (hit(TABS[i], x, y)) {
      const char *want = i == 1 ? "calib" : "remote";
      if (mode != want) sendEvent("mode", want, 0, false);
      return;
    }
  }
  const Button *btns = mode == "calib" ? CALIB_BTNS : REMOTE_BTNS;
  int n = mode == "calib" ? sizeof(CALIB_BTNS) / sizeof(Button) : sizeof(REMOTE_BTNS) / sizeof(Button);
  for (int i = 0; i < n; i++) {
    const Button &b = btns[i];
    if (!hit(b, x, y)) continue;
    drawButton(b, true);
    if (strcmp(b.cmd, "adj") == 0) sendEvent("adj", nullptr, b.value, true);
    else sendEvent(b.cmd, nullptr, 0, false);
    delay(80);
    drawButton(b);
    return;
  }
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  M5.Display.setBrightness(160);

  NimBLEDevice::init(DEVICE_NAME);
  NimBLEDevice::setMTU(517);
  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  NimBLEService *svc = server->createService(SERVICE_UUID);
  evtChar = svc->createCharacteristic(EVT_UUID, NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY);
  NimBLECharacteristic *stateChar =
      svc->createCharacteristic(STATE_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR, 512);
  stateChar->setCallbacks(new StateCallbacks());
  svc->start();
  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  adv->start();

  drawAll();
}

void loop() {
  M5.update();
  if (connChanged) {
    connChanged = false;
    if (connected) sendEvent("hello", mode.c_str(), 0, false);
    drawBody();
  }
  std::string st;
  bool got = false;
  portENTER_CRITICAL(&stateMux);
  if (hasPending) {
    st = pendingState;
    hasPending = false;
    got = true;
  }
  portEXIT_CRITICAL(&stateMux);
  if (got) applyState(st);

  auto t = M5.Touch.getDetail();
  if (t.wasPressed()) onTouch(t.x, t.y);
  delay(10);
}
