#include "protocol.h"
#include <M5Unified.h>
#include <WiFi.h>
#include <ArduinoJson.h>
#include "behavior.h"
#include "bubble.h"
#include "cloud.h"
#include "ext.h"
#include "install.h"
#include "sdcard.h"
#include "sound.h"
#include "vibration.h"

// Intervalo mínimo entre dois eventos do mesmo tipo
static struct { const char* type; uint32_t ms, last; } limits[] = {
    {"touch.tap", 2000, 0}, {"button.a", 1000, 0},  {"button.c", 1000, 0},
    {"sound.loud", 5000, 0}, {"imu.shake", 5000, 0}, {"imu.tap", 2000, 0},
};

// Limites dos comandos
static constexpr uint32_t LOOK_MS = 1500, LOOK_MS_MAX = 5000;
static constexpr uint32_t VIBRATE_MS = 200;
static constexpr uint32_t LEDS_HOLD_MS = 5000, LEDS_HOLD_MS_MAX = 60000;
static constexpr int PLAY_VOLUME = 128;

// A bateria entra no state a cada 5% (ou quando liga/desliga da tomada)
static constexpr uint32_t BATTERY_CHECK_MS = 10000;
static constexpr int BATTERY_STEP = 5;

static std::vector<ObaInfo> installed;  // obaList() lê o cartão: só de novo quando muda
static bool statePending = true;
static bool lastRec = false;
static int batteryPct = -1;
static bool charging = false;
static uint32_t nextBattery = 0;
static LedFx ledsFx;
static uint32_t ledsUntil = 0;

static uint32_t clampMs(JsonVariantConst v, uint32_t def, uint32_t max) {
  uint32_t ms = v | def;
  return ms < 1 ? 1 : ms > max ? max : ms;
}

// ---------------------------------------------------------------- mensagens

static void envelope(JsonDocument& d, const char* type) {
  d["v"] = 1;
  d["type"] = type;
  d["ts"] = cloudEpochMs();
  d["oba"] = oba().id;
}

static void send(Channel ch, JsonDocument& d, const char* evt = nullptr, bool publish = true) {
  String out;
  serializeJson(d, out);
  cloudSend(ch, out, evt, publish);
}

void protoReply(const char* id, const char* re, bool ok, const String& error, const JsonDocument* extra) {
  if (!ok) Serial.printf("[proto] %s recusado: %s\n", re, error.c_str());
  if (!*id) return;
  JsonDocument r;
  envelope(r, "reply");
  r["id"] = id;
  r["re"] = re;
  r["ok"] = ok;
  if (!ok) r["error"] = error;
  if (extra) {
    for (JsonPairConst kv : extra->as<JsonObjectConst>()) r[kv.key()] = kv.value();
  }
  send(Channel::Reply, r);
}

static void reply(const char* id, const char* re, bool ok, const String& error = "") {
  protoReply(id, re, ok, error);
}

// O harness só recebe os eventos que acordam o agente do Oba
// (agent.triggers); os outros passam só pelas regras armadas
static void publishEvent(JsonDocument& d) {
  String type = d["type"].as<String>();
  bool wanted = false;
  for (const String& e : oba().agentEvents) wanted |= e == type;
  send(Channel::Evt, d, type.c_str(), wanted);
}

static bool allow(const char* type, uint32_t now) {
  for (auto& l : limits) {
    if (strcmp(l.type, type)) continue;
    if (l.last && now - l.last < l.ms) return false;
    l.last = now;
  }
  return true;
}

static void onPetEvent(Event e, float dir, uint32_t now) {
  if (e != Event::TouchTap && e != Event::SoundLoud && e != Event::ImuShake && e != Event::ImuTap) return;
  const char* type = eventName(e);
  if (!allow(type, now)) return;
  JsonDocument d;
  envelope(d, type);
  if (e == Event::TouchTap) {
    d["x"] = pet.touchX;
    d["y"] = pet.touchY;
  }
  if (e == Event::ImuTap) d["dir"] = dir > 0 ? 1 : -1;
  publishEvent(d);
}

static void publishState() {
  JsonDocument d;
  envelope(d, "state");
  d["online"] = true;
  d["fw"] = FW_VERSION;
  JsonObject active = d["active"].to<JsonObject>();
  active["id"] = oba().id;
  active["name"] = oba().name;
  active["version"] = oba().version;
  active["sha256"] = oba().sha256;
  JsonArray list = d["obas"].to<JsonArray>();
  for (const ObaInfo& o : installed) {
    JsonObject x = list.add<JsonObject>();
    x["id"] = o.id;
    x["name"] = o.name;
    x["version"] = o.version;
    if (!o.onCard) x["builtin"] = true;
  }
  JsonArray caps = d["caps"].to<JsonArray>();
  for (const char* c : {"display", "touch", "buttons", "imu", "mic.level", "mic.transcribe", "leds", "vibration",
                        "battery", "ext"}) {
    caps.add(c);
  }
  if (M5.Speaker.isEnabled()) caps.add("speaker");
  if (sdState() == SdState::Ready) caps.add("sd");
  d["rec"] = cloudRecording();
  JsonObject bat = d["battery"].to<JsonObject>();
  bat["pct"] = batteryPct;
  bat["charging"] = charging;
  extState(d);
  send(Channel::State, d);
  statePending = false;
  Serial.printf("[proto] state: %s, rec %d, bateria %d%%\n", oba().id.c_str(), cloudRecording(), batteryPct);
}

// ---------------------------------------------------------------- comandos

static float round3(float v) { return roundf(v * 1000) / 1000; }

static void read(JsonDocument& cmd) {
  JsonArrayConst what = cmd["what"];
  auto want = [&](const char* key) {
    if (what.size() == 0) return true;  // sem what: tudo
    for (JsonVariantConst w : what) if (w == key) return true;
    return false;
  };
  JsonDocument r;
  envelope(r, "reply");
  r["id"] = cmd["id"] | "";
  r["re"] = "read";
  r["ok"] = true;
  JsonObject v = r["values"].to<JsonObject>();
  if (want("battery")) {
    JsonObject b = v["battery"].to<JsonObject>();
    b["pct"] = M5.Power.getBatteryLevel();
    b["mv"] = M5.Power.getBatteryVoltage();
    b["charging"] = M5.Power.isCharging() == m5::Power_Class::is_charging;
  }
  if (want("imu") && M5.Imu.isEnabled()) {
    // getAccel dá false quando não há amostra nova desde a última leitura do loop,
    // mas a última continua valendo
    float ax, ay, az, gx, gy, gz;
    M5.Imu.getAccel(&ax, &ay, &az);
    M5.Imu.getGyro(&gx, &gy, &gz);
    JsonObject i = v["imu"].to<JsonObject>();
    i["ax"] = round3(ax), i["ay"] = round3(ay), i["az"] = round3(az);
    i["gx"] = round3(gx), i["gy"] = round3(gy), i["gz"] = round3(gz);
  }
  if (want("mic")) {
    float rms, base;
    petMicLevel(&rms, &base);
    v["mic"]["rms"] = (int)rms;
    v["mic"]["base"] = (int)base;
  }
  if (want("mood")) v["mood"] = moodName(pet.mood);
  if (want("rec")) v["rec"] = cloudRecording();
  if (want("time")) {
    v["time"]["epoch_ms"] = cloudEpochMs();
    v["time"]["uptime_s"] = millis() / 1000;
  }
  if (want("oba")) {
    v["oba"]["id"] = oba().id;
    v["oba"]["name"] = oba().name;
    v["oba"]["version"] = oba().version;
  }
  if (want("wifi")) v["wifi"]["rssi"] = WiFi.RSSI();
  send(Channel::Reply, r);
}

void protoVibrate(uint32_t ms, int level, uint32_t now) {
  VibPattern p{};
  p.ms[0] = ms;
  p.steps = 1;
  p.level = level < 1 ? 1 : level > 255 ? 255 : level;
  vibrationStart(p, now);
}

bool protoEffect(const JsonDocument& cmd, uint32_t now, String& err) {
  const char* type = cmd["type"] | "";
  if (!strcmp(type, "react")) {
    if (!petAct(cmd["do"] | "", now)) err = "do desconhecido";
  } else if (!strcmp(type, "vibrate")) {
    protoVibrate(clampMs(cmd["ms"], VIBRATE_MS, VIBRATION_MS_MAX), cmd["level"] | (int)VIBRATION_LEVEL, now);
  } else if (!strcmp(type, "leds")) {
    LedFx fx;
    if (!obaParseLeds(cmd.as<JsonObjectConst>(), fx)) {
      err = "fx desconhecido";
    } else {
      ledsFx = fx;
      ledsUntil = now + clampMs(cmd["hold_ms"], LEDS_HOLD_MS, LEDS_HOLD_MS_MAX);
    }
  } else if (!strcmp(type, "play")) {
    int vol = cmd["volume"] | PLAY_VOLUME;
    soundPlay(cmd["sound"] | "", vol < 1 ? 1 : vol > 255 ? 255 : vol, err);
  } else {
    return false;
  }
  return true;
}

// Devolve true se trocou o Oba ativo. chunk: era um pedaço de instalação.
static bool handle(const String& json, uint32_t now, bool& chunk) {
  JsonDocument cmd;
  if (deserializeJson(cmd, json)) return false;
  const char* type = cmd["type"] | "";
  const char* id = cmd["id"] | "";
  chunk = !strcmp(type, "oba.chunk");
  if (!chunk) Serial.printf("[proto] comando %s\n", type);  // os pedaços são muitos

  String err;
  if (protoEffect(cmd, now, err)) {
    if (err.length()) reply(id, type, false, err);
  } else if (!strcmp(type, "look")) {
    petLookAt(cmd["x"] | 0.f, cmd["y"] | 0.f, clampMs(cmd["ms"], LOOK_MS, LOOK_MS_MAX), now);
  } else if (!strcmp(type, "read")) {
    read(cmd);
  } else if (!strcmp(type, "oba.activate")) {
    String target = cmd["target"] | "", err;
    if (cloudRecording()) err = "desligue o REC antes";
    else if (!bubbleIdle()) err = "tem um balão na tela";
    else if (extShowing()) err = "tem um pedido na tela";
    else if (target == oba().id) return reply(id, type, true), false;
    else if (obaActivate(target, err)) return reply(id, type, true), true;
    reply(id, type, false, err);
  } else if (!strcmp(type, "oba.remove")) {
    String target = cmd["target"] | "", err;
    bool ok = obaRemove(target, err);
    reply(id, type, ok, err);
    if (ok) protoObasChanged();
  } else if (!strcmp(type, "oba.install") || chunk) {
    installCommand(cmd, now);
  }
  return false;
}

// ---------------------------------------------------------------- API

void protoBegin() {
  petListen(onPetEvent);
  installed = obaList();
  lastRec = cloudRecording();
}

bool protoUpdate(uint32_t now) {
  bool changed = false;
  // No máximo um pedaço de instalação por quadro: gravar no cartão leva alguns
  // ms e a animação segue lisa
  while (String* cmd = cloudTakeCommand()) {
    bool chunk = false;
    changed |= handle(*cmd, now, chunk);
    delete cmd;
    if (chunk) break;
  }
  vibrationUpdate(now);

  if (cloudRecording() != lastRec) {
    lastRec = !lastRec;
    statePending = true;
  }
  if ((int32_t)(now - nextBattery) >= 0) {
    nextBattery = now + BATTERY_CHECK_MS;
    int pct = M5.Power.getBatteryLevel();
    bool ch = M5.Power.isCharging() == m5::Power_Class::is_charging;
    if (abs(pct - batteryPct) >= BATTERY_STEP || ch != charging) {
      batteryPct = pct;
      charging = ch;
      statePending = true;
    }
  }
  if (cloudTakeStateRequest()) statePending = true;
  if (statePending && cloudOnline()) publishState();
  return changed;
}

void protoStateChanged() { statePending = true; }

void protoObasChanged() {
  installed = obaList();
  statePending = true;
}

void protoButton(char which, uint32_t now) {
  const char* type = which == 'a' ? "button.a" : "button.c";
  if (!allow(type, now)) return;
  JsonDocument d;
  envelope(d, type);
  publishEvent(d);
}

void protoBubbleDone(const String& id) {
  if (!id.length()) return;
  JsonDocument d;
  envelope(d, "bubble.done");
  d["id"] = id;
  publishEvent(d);
}

void protoSimulate(const String& type, uint32_t now) {
  if (type == "button.a" || type == "button.c") return protoButton(type[7], now);
  for (Event e : {Event::TouchTap, Event::SoundLoud, Event::ImuShake, Event::ImuTap}) {
    if (type == eventName(e)) {
      petReact(e, now, 1.f);
      return;
    }
  }
  Serial.printf("[proto] evento desconhecido: %s\n", type.c_str());
}

const LedFx& protoLeds(const LedFx& mood, uint32_t now) {
  if (ledsUntil && (int32_t)(now - ledsUntil) >= 0) ledsUntil = 0;
  return ledsUntil ? ledsFx : mood;
}
