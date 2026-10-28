// M5Stack Core2 / CoreS3 リモコン。Pi（robot/srr/remote.py）と Bluetooth LE でつなぐ。
//
// M5Stack は表示とボタンだけを受け持ち、何を調整しているか・動いているかは Pi が決める。
//   - EVT（notify、M5 → Pi）：押したボタン。{"c":"start"} {"c":"adj","v":-10} {"c":"mode","v":"calib"}
//     {"c":"vol","v":5} {"c":"voltest"} {"c":"amp","v":10} {"c":"spd","v":10} {"c":"itv","v":5}（動作の間隔）{"c":"prog","v":1}（台本を選ぶ）{"c":"swap"}（キャラの配置を入れ替える）など
//   - STATE（write、Pi → M5）：画面に出す内容。{"m":"remote"|"calib"|"settings","run":true,"t":["行1","行2",...]}
// Pi と切れたら広告を出し直し、Pi が見つけてつなぎ直す。
// USB ケーブル（シリアル 115200 bps）でも同じ JSON を 1 行ずつやりとりできる（Pi の remote.transport: serial）。
// シリアルで Pi から STATE が届いている間は、押したボタンをシリアルにも送る。
// 画面の上のタブか、本体のボタン（Core2 の A：前のタブ、B：リモコン、C：次のタブ）で画面を切り替える。

#include <M5Unified.h>
#include <NimBLEDevice.h>
#include <ArduinoJson.h>

static const char *DEVICE_NAME = "SRR-Remote";
static const char *SERVICE_UUID = "6e3f0001-5a1e-4c1b-9f5b-7d2a1c0e5a01";
static const char *EVT_UUID = "6e3f0002-5a1e-4c1b-9f5b-7d2a1c0e5a01";
static const char *STATE_UUID = "6e3f0003-5a1e-4c1b-9f5b-7d2a1c0e5a01";

static NimBLECharacteristic *evtChar = nullptr;
static volatile bool connected = false;   // BLE でつながっている
static volatile bool connChanged = true;
static bool serialAlive = false;            // シリアルで Pi から STATE が届いている
static uint32_t serialLastMs = 0;
static const uint32_t SERIAL_TIMEOUT_MS = 5000;   // Pi は変わらなくても 2 s ごとに送り直す
static std::string serialBuf;

static bool linked() { return connected || serialAlive; }

// Pi から届いた STATE。BLE のタスクで受け、描画は loop で行う
static portMUX_TYPE stateMux = portMUX_INITIALIZER_UNLOCKED;
static std::string pendingState;
static bool hasPending = false;

// 今の表示内容
static String mode = "remote";  // remote / calib / settings / manual
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
    {0, 0, 80, 34, "リモコン", "mode", 0, COL_KEY},
    {80, 0, 80, 34, "角度調整", "mode", 1, COL_KEY},
    {160, 0, 80, 34, "設定", "mode", 2, COL_KEY},
    {240, 0, 80, 34, "手動", "mode", 3, COL_KEY},
};
static const char *TAB_MODES[] = {"remote", "calib", "settings", "manual"};
static const int TAB_COUNT = 4;
static const Button REMOTE_BTNS[] = {
    {4, 142, 102, 36, "◀ 台本", "prog", -1, COL_KEY},
    {110, 142, 100, 36, "入替", "swap", 0, COL_SAVE},
    {214, 142, 102, 36, "台本 ▶", "prog", 1, COL_KEY},
    {4, 184, 102, 52, "開始", "start", 0, COL_START},
    {110, 184, 100, 52, "1回", "once", 0, COL_KEY},
    {214, 184, 102, 52, "停止", "stop", 0, COL_STOP},
};
// 設定はボタンが 4 段あるので、文字の欄を詰めて（SETTINGS_TOP から）ボタンを並べる
static const int SETTINGS_TOP = 118;
static const Button SETTINGS_BTNS[] = {
    {4, 120, 102, 27, "音量 -", "vol", -5, COL_KEY},
    {110, 120, 100, 27, "テスト", "voltest", 0, COL_SAVE},
    {214, 120, 102, 27, "音量 +", "vol", 5, COL_KEY},
    {4, 150, 102, 27, "動き -", "amp", -10, COL_KEY},
    {110, 150, 100, 27, "1回", "once", 0, COL_START},
    {214, 150, 102, 27, "動き +", "amp", 10, COL_KEY},
    {4, 180, 102, 27, "速さ -", "spd", -10, COL_KEY},
    {110, 180, 100, 27, "停止", "stop", 0, COL_STOP},
    {214, 180, 102, 27, "速さ +", "spd", 10, COL_KEY},
    {4, 210, 102, 27, "間隔 -", "itv", -5, COL_KEY},
    {110, 210, 100, 27, "開始", "start", 0, COL_START},
    {214, 210, 102, 27, "間隔 +", "itv", 5, COL_KEY},
};
static const Button MANUAL_BTNS[] = {
    {4, 142, 75, 44, "-10", "adj", -10, COL_KEY},
    {83, 142, 75, 44, "-1", "adj", -1, COL_KEY},
    {162, 142, 75, 44, "+1", "adj", 1, COL_KEY},
    {241, 142, 75, 44, "+10", "adj", 10, COL_KEY},
    {4, 192, 100, 44, "◀ 前", "prev", 0, COL_KEY},
    {110, 192, 100, 44, "次 ▶", "next", 0, COL_KEY},
    {216, 192, 100, 44, "原点", "home", 0, COL_STOP},
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
  if (!linked()) return;
  JsonDocument doc;
  doc["c"] = cmd;
  if (strValue) doc["v"] = strValue;
  else if (hasInt) doc["v"] = intValue;
  std::string out;
  serializeJson(doc, out);
  if (serialAlive) Serial.println(out.c_str());
  if (connected && evtChar != nullptr) {
    evtChar->setValue(out);
    evtChar->notify();
  }
}

static void drawButton(const Button &b, bool pressed = false) {
  auto &d = M5.Display;
  uint16_t fill = pressed ? COL_TEXT : b.color;
  d.fillRoundRect(b.x, b.y, b.w, b.h, 8, fill);
  d.setTextColor(pressed ? COL_BG : COL_TEXT);
  d.setTextDatum(middle_center);
  d.setFont(b.h >= 50 ? &fonts::efontJA_24 : &fonts::efontJA_16);
  d.drawString(b.label, b.x + b.w / 2, b.y + b.h / 2);
}

static void drawTabs() {
  auto &d = M5.Display;
  for (int i = 0; i < TAB_COUNT; i++) {
    const Button &t = TABS[i];
    bool active = mode == TAB_MODES[i];
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
  bool settings = mode == "settings";
  d.fillRect(0, 36, 320, (settings ? SETTINGS_TOP : 140) - 36, COL_BG);
  d.setTextDatum(top_left);
  if (!linked()) {
    d.setFont(&fonts::efontJA_16);
    d.setTextColor(COL_DIM);
    d.drawString("Pi と未接続", 10, 50);
    d.drawString("Pi の srr run を起動して待つ", 10, 76);
    d.drawString("（BLE か USB ケーブル）", 10, 102);
    return;
  }
  for (int i = 0; i < lineCount; i++) {
    // 角度調整ではパルス幅の行（3 行目）、手動では今の値の行（2 行目）を大きく出す
    bool big = (mode == "calib" && i == 2) || (mode == "manual" && i == 1);
    d.setFont(big ? &fonts::efontJA_24 : &fonts::efontJA_16);
    d.setTextColor(i == 0 ? COL_TEXT : (big ? TFT_YELLOW : 0xC618));
    int y = settings ? 40 + i * 19 : 42 + i * 24 + (mode == "calib" && i == 3 ? 6 : 0);
    d.drawString(lines[i].c_str(), 10, y);
  }
  if (mode == "remote") {
    d.fillCircle(304, 50, 7, running ? TFT_GREEN : COL_DIM);
  }
}

static const Button *currentButtons(int &n) {
  if (mode == "calib") {
    n = sizeof(CALIB_BTNS) / sizeof(Button);
    return CALIB_BTNS;
  }
  if (mode == "manual") {
    n = sizeof(MANUAL_BTNS) / sizeof(Button);
    return MANUAL_BTNS;
  }
  if (mode == "settings") {
    n = sizeof(SETTINGS_BTNS) / sizeof(Button);
    return SETTINGS_BTNS;
  }
  n = sizeof(REMOTE_BTNS) / sizeof(Button);
  return REMOTE_BTNS;
}

static void drawButtons() {
  int top = mode == "settings" ? SETTINGS_TOP : 140;
  M5.Display.fillRect(0, top, 320, 240 - top, COL_BG);
  int n;
  const Button *btns = currentButtons(n);
  for (int i = 0; i < n; i++) drawButton(btns[i]);
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
  for (int i = 0; i < TAB_COUNT; i++) {
    if (hit(TABS[i], x, y)) {
      if (mode != TAB_MODES[i]) sendEvent("mode", TAB_MODES[i], 0, false);
      return;
    }
  }
  int n;
  const Button *btns = currentButtons(n);
  for (int i = 0; i < n; i++) {
    const Button &b = btns[i];
    if (!hit(b, x, y)) continue;
    drawButton(b, true);
    if (b.value != 0) sendEvent(b.cmd, nullptr, b.value, true);
    else sendEvent(b.cmd, nullptr, 0, false);
    delay(80);
    drawButton(b);
    return;
  }
}

void setup() {
  Serial.setRxBufferSize(4096);   // 描画中に届いた STATE を取りこぼさない
  Serial.begin(115200);           // M5Unified は serial_baudrate が 0（既定）だと開かない
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

// シリアルで届いた行のうち、{ で始まるもの（STATE）を表示に使う。起動時のログなどは読み捨てる
static void readSerial() {
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\r') continue;
    if (ch != '\n') {
      if (serialBuf.size() < 2048) serialBuf += ch;
      continue;
    }
    if (!serialBuf.empty() && serialBuf[0] == '{') {
      serialLastMs = millis();
      if (!serialAlive) {
        serialAlive = true;
        connChanged = true;
      }
      applyState(serialBuf);
    }
    serialBuf.clear();
  }
  if (serialAlive && millis() - serialLastMs > SERIAL_TIMEOUT_MS) {
    serialAlive = false;
    connChanged = true;
  }
}

void loop() {
  M5.update();
  readSerial();
  if (connChanged) {
    connChanged = false;
    if (linked()) sendEvent("hello", mode.c_str(), 0, false);
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
  // 本体のボタンで画面を切り替える（A：前、B：リモコン、C：次）
  int cur = 0;
  for (int i = 0; i < TAB_COUNT; i++) if (mode == TAB_MODES[i]) cur = i;
  if (M5.BtnA.wasPressed()) sendEvent("mode", TAB_MODES[(cur + TAB_COUNT - 1) % TAB_COUNT], 0, false);
  if (M5.BtnB.wasPressed() && cur != 0) sendEvent("mode", TAB_MODES[0], 0, false);
  if (M5.BtnC.wasPressed()) sendEvent("mode", TAB_MODES[(cur + 1) % TAB_COUNT], 0, false);
  delay(10);
}
