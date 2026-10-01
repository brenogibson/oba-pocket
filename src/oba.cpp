#include "oba.h"
#include <ArduinoJson.h>
#include <Preferences.h>
#include <SD.h>
#include <algorithm>
#include <mbedtls/version.h>
#include "sdcard.h"
#include "sound.h"
#include "sprite.h"
#include "builtin_oba.h"

static constexpr size_t REFLEX_MAX = 32;
static constexpr size_t WAKE_WORD_MAX = 8;

static const char* const MOOD_NAMES[MOOD_COUNT] = {"idle", "scared", "shy", "happy", "dizzy", "sleepy", "busy", "alert"};
static const char* const EVENT_NAMES[EVENT_COUNT] = {"touch.tap", "sound.loud", "sound.voice", "imu.shake",
                                                     "imu.tap",   "wake",       "bubble.open"};
static const char* const EYES_NAMES[] = {"open", "closing", "happy", "scared", "spiral", "covered"};
static const char* const EXTRA_NAMES[] = {"none", "stars", "zzz"};
static const char* const LED_NAMES[] = {"off", "solid", "breathe", "rainbow", "strobe", "chase"};

static int nameIndex(const char* s, const char* const* names, int n) {
  for (int i = 0; s && i < n; ++i) if (!strcmp(s, names[i])) return i;
  return -1;
}

const char* moodName(Mood m) { return (int)m < MOOD_COUNT ? MOOD_NAMES[(int)m] : "?"; }

bool moodByName(const char* name, Mood* out) {
  int i = nameIndex(name, MOOD_NAMES, MOOD_COUNT);
  if (i >= 0) *out = (Mood)i;
  return i >= 0;
}

const char* eventName(Event e) { return (int)e < EVENT_COUNT ? EVENT_NAMES[(int)e] : "?"; }

// O documento do ArduinoJson vai para a PSRAM, junto com o texto
struct PsramAllocator : ArduinoJson::Allocator {
  void* allocate(size_t n) override { return ps_malloc(n); }
  void deallocate(void* p) override { free(p); }
  void* reallocate(void* p, size_t n) override { return ps_realloc(p, n); }
};
static PsramAllocator psram;

// ---------------------------------------------------------------- parse

static uint16_t rgb565(uint32_t rgb) {
  return ((rgb >> 8) & 0xF800) | ((rgb >> 5) & 0x07E0) | ((rgb >> 3) & 0x001F);
}

// "#RRGGBB"
static bool parseColor(JsonVariantConst v, uint32_t* rgb) {
  const char* s = v.as<const char*>();
  if (!s || s[0] != '#' || strlen(s) != 7) return false;
  char* end;
  uint32_t c = strtoul(s + 1, &end, 16);
  if (*end) return false;
  *rgb = c;
  return true;
}

static float num(JsonVariantConst v, float def) { return v.is<float>() ? v.as<float>() : def; }

static Motion constant(float v) {
  Motion m;
  m.a = v;
  return m;
}

static Motion curve(Motion::Kind kind, float a, float b, float c = 0) {
  Motion m;
  m.kind = kind;
  m.a = a;
  m.b = b;
  m.c = c;
  return m;
}

// Número = constante; {"sin": [vel, amp, base]}, {"bounce": [vel, amp, base]}, {"jitter": [min, max]}
static Motion parseMotion(JsonVariantConst v, const Motion& def) {
  if (v.is<float>()) return constant(v.as<float>());
  static const struct { const char* key; Motion::Kind kind; } KINDS[] = {
      {"sin", Motion::Sin}, {"bounce", Motion::Bounce}, {"jitter", Motion::Jitter}};
  for (const auto& k : KINDS) {
    JsonArrayConst a = v[k.key].as<JsonArrayConst>();
    if (!a.isNull()) return curve(k.kind, a[0] | 0.f, a[1] | 0.f, a[2] | 0.f);
  }
  return def;
}

static LedFx led(LedFx::Kind kind, uint32_t color, float min = 0, float max = 1, float speed = 1, uint32_t ms = 100) {
  LedFx f;
  f.kind = kind;
  f.color = color;
  f.min = min;
  f.max = max;
  f.speed = speed;
  f.ms = ms;
  return f;
}

// Humores de um Oba que não diz nada sobre eles
static void defaultMoods(MoodSpec* m, uint32_t bg, uint32_t blush, uint32_t star) {
  MoodSpec& idle = m[(int)Mood::Idle];
  idle.bob = curve(Motion::Sin, 2, 7);
  idle.sway = constant(0);
  idle.squash = curve(Motion::Sin, 2, 0.02f);
  idle.wave = constant(3.5f);
  idle.cheeks = Cheeks::Touch;
  idle.leds = led(LedFx::Breathe, bg, 0.3f, 1, 1.6f);
  idle.ms = 45000;
  idle.then = Mood::Sleepy;

  for (int i = 1; i < MOOD_COUNT; ++i) m[i] = idle;

  MoodSpec& scared = m[(int)Mood::Scared];
  scared.bob = constant(-6);
  scared.sway = curve(Motion::Jitter, -5, 5);
  scared.squash = constant(0.05f);
  scared.eyes = Eyes::Scared;
  scared.cheeks = Cheeks::Off;
  scared.leds = led(LedFx::Strobe, 0xFFFFFF, 0, 1, 1, 80);
  scared.ms = 700;
  scared.then = Mood::Shy;

  MoodSpec& shy = m[(int)Mood::Shy];
  shy.bob = curve(Motion::Sin, 2, 2, 4);
  shy.sway = curve(Motion::Sin, 18, 1.5f);
  shy.squash = constant(-0.03f);
  shy.eyes = Eyes::Covered;
  shy.cheeks = Cheeks::On;
  shy.leds = led(LedFx::Solid, blush);
  shy.ms = 2000;
  shy.quietMs = 2500;
  shy.then = Mood::Idle;

  MoodSpec& happy = m[(int)Mood::Happy];
  happy.bob = curve(Motion::Bounce, 9, 16);
  happy.squash = curve(Motion::Bounce, 9, 0.05f);
  happy.wave = constant(6);
  happy.eyes = Eyes::Happy;
  happy.cheeks = Cheeks::On;
  happy.leds = led(LedFx::Rainbow, 0, 0, 1, 80);
  happy.ms = 1800;
  happy.then = Mood::Idle;

  MoodSpec& dizzy = m[(int)Mood::Dizzy];
  dizzy.bob = curve(Motion::Sin, 3, 4);
  dizzy.sway = curve(Motion::Sin, 6, 16);
  dizzy.wave = constant(6);
  dizzy.eyes = Eyes::Spiral;
  dizzy.cheeks = Cheeks::Off;
  dizzy.extra = Extra::Stars;
  dizzy.leds = led(LedFx::Chase, star, 0, 1, 1, 60);
  dizzy.leds.trail = bg;
  dizzy.ms = 2600;
  dizzy.then = Mood::Idle;

  MoodSpec& sleepy = m[(int)Mood::Sleepy];
  sleepy.bob = curve(Motion::Sin, 1.1f, 3, 6);
  sleepy.wave = constant(1.5f);
  sleepy.eyes = Eyes::Closing;
  sleepy.cheeks = Cheeks::Off;
  sleepy.extra = Extra::Zzz;
  sleepy.dy = 4;
  sleepy.leds = led(LedFx::Breathe, bg, 0.05f, 0.3f, 0.6f);
  sleepy.ms = 0;
  sleepy.then = Mood::Happy;

  // Os de fora duram enquanto a fonte quiser (ext.cpp): ms não conta
  MoodSpec& busy = m[(int)Mood::Busy];
  busy.bob = curve(Motion::Sin, 5, 3);
  busy.squash = curve(Motion::Sin, 5, 0.02f);
  busy.wave = constant(5);
  busy.cheeks = Cheeks::Off;
  busy.leds = led(LedFx::Chase, star, 0, 1, 1, 90);
  busy.leds.trail = bg;
  busy.ms = 0;

  MoodSpec& alert = m[(int)Mood::Alert];
  alert.bob = curve(Motion::Bounce, 7, 8);
  alert.squash = curve(Motion::Bounce, 7, 0.04f);
  alert.wave = constant(5);
  alert.cheeks = Cheeks::Off;
  alert.leds = led(LedFx::Breathe, 0xFFB000, 0.1f, 1, 6);
  alert.ms = 0;
}

static bool parseMood(JsonObjectConst o, MoodSpec& m, const char* name, String& err) {
  m.bob = parseMotion(o["bob"], m.bob);
  m.sway = parseMotion(o["sway"], m.sway);
  m.squash = parseMotion(o["squash"], m.squash);
  m.wave = parseMotion(o["wave"], m.wave);

  JsonObjectConst face = o["face"];
  if (!face.isNull()) {
    if (face["eyes"].is<const char*>()) {
      int i = nameIndex(face["eyes"].as<const char*>(), EYES_NAMES, 6);
      if (i < 0) return err = String("moods.") + name + ".face.eyes desconhecido", false;
      m.eyes = (Eyes)i;
    }
    JsonVariantConst ch = face["cheeks"];
    if (ch.is<bool>()) m.cheeks = ch.as<bool>() ? Cheeks::On : Cheeks::Off;
    else if (ch.is<const char*>() && !strcmp(ch.as<const char*>(), "touch")) m.cheeks = Cheeks::Touch;
    if (face["extra"].is<const char*>()) {
      int i = nameIndex(face["extra"].as<const char*>(), EXTRA_NAMES, 3);
      if (i < 0) return err = String("moods.") + name + ".face.extra desconhecido", false;
      m.extra = (Extra)i;
    }
    m.dy = num(face["dy"], m.dy);
  }

  JsonObjectConst leds = o["leds"];
  if (!leds.isNull() && !obaParseLeds(leds, m.leds)) return err = String("moods.") + name + ".leds.fx desconhecido", false;

  m.ms = o["ms"] | m.ms;
  m.quietMs = o["quiet_ms"] | m.quietMs;
  if (o["then"].is<const char*>() && (!moodByName(o["then"].as<const char*>(), &m.then) || moodExternal(m.then))) {
    return err = String("moods.") + name + ".then desconhecido", false;
  }
  return true;
}

bool obaParseLeds(JsonObjectConst o, LedFx& out) {
  int i = nameIndex(o["fx"] | "", LED_NAMES, 6);
  if (i < 0) return false;
  LedFx f = out;
  f.kind = (LedFx::Kind)i;
  parseColor(o["color"], &f.color);
  parseColor(o["trail"], &f.trail);
  f.min = num(o["min"], f.min);
  f.max = num(o["max"], f.max);
  f.speed = num(o["speed"], f.speed);
  f.ms = o["ms"] | f.ms;
  if (!f.ms) f.ms = 1;
  out = f;
  return true;
}

// Humores em que o reflexo vale: "from" (só nestes) ou "except" (todos menos estes)
static bool parseMoodSet(JsonVariantConst v, uint8_t* mask) {
  *mask = 0;
  for (JsonVariantConst n : v.as<JsonArrayConst>()) {
    Mood m;
    if (!moodByName(n.as<const char*>(), &m)) return false;
    *mask |= 1 << (int)m;
  }
  return true;
}

static void defaultReflexes(std::vector<Reflex>& r) {
  auto bits = [](std::initializer_list<Mood> ms) {
    uint8_t b = 0;
    for (Mood m : ms) b |= 1 << (int)m;
    return b;
  };
  const uint8_t ALL = (1 << MOOD_COUNT) - 1;
  r = {
      {Event::SoundLoud, Reflex::ToMood, Mood::Scared, bits({Mood::Idle, Mood::Happy, Mood::Sleepy}), ""},
      {Event::SoundVoice, Reflex::Glance, Mood::Idle, bits({Mood::Idle}), ""},
      {Event::ImuTap, Reflex::Turn, Mood::Idle, (uint8_t)(ALL & ~bits({Mood::Dizzy})), ""},
      {Event::ImuShake, Reflex::ToMood, Mood::Dizzy, (uint8_t)(ALL & ~bits({Mood::Dizzy})), ""},
      {Event::TouchTap, Reflex::ToMood, Mood::Idle, bits({Mood::Shy}), ""},
      {Event::TouchTap, Reflex::ToMood, Mood::Happy, (uint8_t)(ALL & ~bits({Mood::Scared, Mood::Dizzy, Mood::Shy})), ""},
      {Event::Wake, Reflex::ToMood, Mood::Happy, ALL, ""},
      {Event::BubbleOpen, Reflex::ToMood, Mood::Happy, (uint8_t)(ALL & ~bits({Mood::Scared, Mood::Dizzy})), ""},
  };
}

// "vibrate": {"ms": 150} ou {"ms": [liga, desliga, liga...], "level": 1..255}
static bool parseVibrate(JsonVariantConst v, VibPattern& p, String& err) {
  p = VibPattern{};
  JsonVariantConst ms = v["ms"];
  uint32_t total = 0;
  auto add = [&](JsonVariantConst s) {
    if (!s.is<int>() || p.steps >= VIBRATION_STEPS_MAX) return false;
    int n = s.as<int>();
    if (n < (int)VIBRATION_STEP_MIN_MS || n > (int)VIBRATION_MS_MAX) return false;
    p.ms[p.steps++] = n;
    total += n;
    return true;
  };
  if (ms.is<JsonArrayConst>()) {
    for (JsonVariantConst s : ms.as<JsonArrayConst>()) {
      if (!add(s)) return err = ".ms: até 8 passos, cada um de 20 a 2000 ms", false;
    }
  } else if (!add(ms)) {
    return err = ".ms: de 20 a 2000 ms, ou uma lista deles", false;
  }
  if (!p.steps || total > VIBRATION_MS_MAX) return err = ".ms: somando tudo, de 20 a 2000 ms", false;
  int level = v["level"] | (int)VIBRATION_LEVEL;
  if (!v["level"].isNull() && !v["level"].is<int>()) level = 0;
  if (level < 1 || level > 255) return err = ".level: de 1 a 255", false;
  p.level = level;
  return true;
}

static bool parseReflexes(JsonArrayConst arr, const ObaSpec& spec, std::vector<Reflex>& out, String& err) {
  out.clear();
  const uint8_t ALL = (1 << MOOD_COUNT) - 1;
  int i = 0;
  for (JsonObjectConst o : arr) {
    String where = "reflexes[" + String(i++) + "]";
    if (out.size() >= REFLEX_MAX) return err = "reflexos demais", false;
    Reflex r{};
    int e = nameIndex(o["on"] | "", EVENT_NAMES, EVENT_COUNT);
    if (e < 0) return err = where + ".on desconhecido", false;
    r.on = (Event)e;
    const char* act = o["do"] | "";
    if (!strcmp(act, "glance")) r.act = Reflex::Glance;
    else if (!strcmp(act, "turn")) r.act = Reflex::Turn;
    else if (!strcmp(act, "none")) r.act = Reflex::None;
    else if (moodByName(act, &r.mood) && !moodExternal(r.mood)) r.act = Reflex::ToMood;
    else return err = where + ".do desconhecido", false;
    r.moods = ALL;
    uint8_t mask;
    if (!o["from"].isNull()) {
      if (!parseMoodSet(o["from"], &mask)) return err = where + ".from com humor desconhecido", false;
      r.moods = mask;
    } else if (!o["except"].isNull()) {
      if (!parseMoodSet(o["except"], &mask)) return err = where + ".except com humor desconhecido", false;
      r.moods = ALL & ~mask;
    }
    if (!o["sound"].isNull()) {
      r.sound = o["sound"] | "";
      if (!spec.sound(r.sound.c_str())) return err = where + ".sound: não existe em sounds", false;
    }
    if (!o["vibrate"].isNull()) {
      String why;
      if (!parseVibrate(o["vibrate"], r.vibrate, why)) return err = where + ".vibrate" + why, false;
    }
    out.push_back(r);
  }
  return true;
}

bool obaValidId(const String& id) {
  if (!id.length() || id.length() > 31) return false;
  for (char c : id) {
    if (!isdigit((unsigned char)c) && !(c >= 'a' && c <= 'z') && c != '-' && c != '_') return false;
  }
  return true;
}

bool obaValidFile(const String& rel) {
  if (!rel.length() || rel.length() > 64 || rel[0] == '/' || rel[0] == '.' || rel.endsWith("/")) return false;
  if (rel.indexOf("..") >= 0 || rel.indexOf("//") >= 0) return false;
  int dirs = 0;
  for (char c : rel) {
    if (!isalnum((unsigned char)c) && c != '.' && c != '-' && c != '_' && c != '/') return false;
    dirs += c == '/';
  }
  return dirs <= OBA_FILE_DIRS_MAX;  // o obaRemoveTree desce um nível por pasta, na pilha do loop
}

static bool validSha(const String& s) {
  if (s.length() != 64) return false;
  for (char c : s) {
    if (!isdigit((unsigned char)c) && !(c >= 'a' && c <= 'f')) return false;
  }
  return true;
}

std::shared_ptr<const SoundClip> ObaSpec::sound(const char* name) const {
  for (const auto& c : sounds) {
    if (c->name == name) return c;
  }
  return nullptr;
}

const String& ObaSpec::fileSha(const String& path) const {
  static const String NONE;
  for (const auto& f : files) {
    if (f.first == path) return f.second;
  }
  return NONE;
}

// look dos sprites: tamanho do quadro, origem, escala e a folha de cada humor
static bool parseSprites(JsonObjectConst look, ObaSpec& out, String& err) {
  SpriteLook& S = out.sprites;
  S = SpriteLook();
  JsonArrayConst size = look["size"];
  if (size.size() != 2 || !size[0].is<int>() || !size[1].is<int>()) return err = "look.size precisa ser [w, h]", false;
  int w = size[0].as<int>(), h = size[1].as<int>();
  if (w < 8 || w > 96 || h < 8 || h > 96) return err = "look.size: w e h de 8 a 96", false;
  S.w = w;
  S.h = h;
  S.originX = w / 2.f;
  S.originY = h / 2.f;
  if (!look["origin"].isNull()) {
    JsonArrayConst o = look["origin"];
    if (o.size() != 2 || !o[0].is<float>() || !o[1].is<float>()) return err = "look.origin precisa ser [x, y]", false;
    S.originX = o[0].as<float>();
    S.originY = o[1].as<float>();
    if (!(S.originX >= 0 && S.originX <= w && S.originY >= 0 && S.originY <= h)) return err = "look.origin fora do quadro", false;
  }
  JsonVariantConst scale = look["scale"];
  if (!scale.isNull()) {
    if (!scale.is<int>() || scale.as<int>() < 1 || scale.as<int>() > 8) return err = "look.scale: inteiro de 1 a 8", false;
    S.scale = scale.as<int>();
  }
  if (w * S.scale > 240 || h * S.scale > 200) return err = "look: size × scale passa de 240 × 200", false;

  bool has[MOOD_COUNT] = {};
  for (JsonPairConst kv : look["frames"].as<JsonObjectConst>()) {
    String where = String("look.frames.") + kv.key().c_str();
    Mood m;
    if (!moodByName(kv.key().c_str(), &m)) return err = where + " não existe", false;
    JsonObjectConst f = kv.value();
    SpriteLook::Frames& F = S.frames[(int)m];
    F.sheet = f["sheet"] | "";
    if (!obaValidFile(F.sheet)) return err = where + ".sheet: caminho inválido", false;
    if (!out.fileSha(F.sheet).length()) return err = where + ".sheet não está em files", false;
    JsonVariantConst fps = f["fps"];
    if (!fps.isNull()) {
      if (!fps.is<int>() || fps.as<int>() < 1 || fps.as<int>() > 30) return err = where + ".fps: inteiro de 1 a 30", false;
      F.fps = fps.as<int>();
    }
    has[(int)m] = true;
  }
  if (!has[(int)Mood::Idle]) return err = "look.frames.idle é obrigatório", false;
  for (int i = 0; i < MOOD_COUNT; ++i) {
    if (!has[i]) S.frames[i] = S.frames[(int)Mood::Idle];
  }
  return true;
}

bool obaParse(const char* json, size_t len, ObaSpec& out, String& err) {
  JsonDocument doc(&psram);
  DeserializationError de = deserializeJson(doc, json, len);
  if (de) return err = String("JSON inválido: ") + de.c_str(), false;
  if ((doc["oba"] | 0) != 1) return err = "\"oba\" precisa ser 1", false;

  out.id = doc["id"] | "";
  out.name = doc["name"] | "";
  out.version = doc["version"] | "0";
  if (!obaValidId(out.id)) return err = "id inválido (só a-z, 0-9, - e _)", false;
  if (!out.name.length()) return err = "falta o nome", false;

  // Arquivos da pasta (quadros e sons), cada um com o sha256
  out.files.clear();
  if (!doc["files"].isNull() && !doc["files"].is<JsonObjectConst>()) return err = "files precisa ser um objeto", false;
  for (JsonPairConst kv : doc["files"].as<JsonObjectConst>()) {
    String path = kv.key().c_str();
    if (out.files.size() >= OBA_FILES_MAX) return err = "arquivos demais em files (até " + String(OBA_FILES_MAX) + ")", false;
    if (path == "oba.json") return err = "o oba.json não entra em files", false;
    if (!obaValidFile(path)) return err = "files: caminho inválido: " + path, false;
    if (out.fileSha(path).length()) return err = "files: repetido: " + path, false;
    String sha = kv.value() | "";
    sha.toLowerCase();
    if (!validSha(sha)) return err = "files." + path + ": o sha256 precisa ter 64 hex", false;
    out.files.push_back({path, sha});
  }

  JsonObjectConst look = doc["look"];
  const char* type = look["type"] | "rig";
  bool sprites = !strcmp(type, "sprites");
  if (!sprites && strcmp(type, "rig")) return err = "look.type desconhecido (\"rig\" ou \"sprites\")", false;
  out.lookType = sprites ? LookType::Sprites : LookType::Rig;
  Look& L = out.look;

  // Paleta. Nos sprites, body, outline e eye (se faltarem, ink) e blush (star) são opcionais.
  JsonObjectConst pal = look["palette"];
  static const char* const REQUIRED[] = {"bg", "shadow", "body", "outline", "eye", "blush", "star", "text", "bubble", "ink"};
  static const int8_t SPRITE_DEFAULT[] = {-1, -1, 9, 9, 9, 6, -1, -1, -1, -1};
  uint32_t rgb[13];
  for (int i = 0; i < 10; ++i) {
    if (parseColor(pal[REQUIRED[i]], &rgb[i])) continue;
    if (sprites && SPRITE_DEFAULT[i] >= 0 && pal[REQUIRED[i]].isNull()) continue;
    return err = String("look.palette.") + REQUIRED[i] + " falta ou não é #RRGGBB", false;
  }
  for (int i = 0; sprites && i < 10; ++i) {
    if (SPRITE_DEFAULT[i] >= 0 && pal[REQUIRED[i]].isNull()) rgb[i] = rgb[SPRITE_DEFAULT[i]];
  }
  if (!parseColor(pal["rec"], &rgb[10])) rgb[10] = 0xFF3B4E;
  if (!parseColor(pal["wait"], &rgb[11])) rgb[11] = 0xFFC23D;
  if (!parseColor(pal["off"], &rgb[12])) rgb[12] = rgb[7];
  uint16_t* c565[] = {&L.color.bg,   &L.color.shadow, &L.color.body,   &L.color.outline, &L.color.eye,
                      &L.color.blush, &L.color.star,   &L.color.text,   &L.color.bubble,  &L.color.ink,
                      &L.color.rec,   &L.color.wait,   &L.color.off};
  for (int i = 0; i < 13; ++i) *c565[i] = rgb565(rgb[i]);

  // Alto e baixo do corpo, para os padrões da sombra e dos enfeites
  float minY, maxY;
  L.outline.clear();
  if (sprites) {
    if (!parseSprites(look, out, err)) return false;
    const SpriteLook& S = out.sprites;
    minY = -S.originY * S.scale;
    maxY = (S.h - S.originY) * S.scale;
    // O rosto está no desenho: sem olhos, bochechas, braços nem balanço da barra
    L.wave = {0, 1, 0, 0};
    L.eyes = {0, 0, 0, 0, 0, 0, 0, 0};
    L.cheeks = {0, 0, 0, 0};
    L.arms[0] = L.arms[1] = {0, 0, 0, 0, 0};
  } else {
    // Contorno
    JsonArrayConst pts = look["outline"];
    if (pts.size() < 3 || pts.size() > OBA_OUTLINE_MAX) {
      return err = "look.outline precisa de 3 a " + String(OBA_OUTLINE_MAX) + " pontos", false;
    }
    minY = 1e9f;
    maxY = -1e9f;
    for (JsonVariantConst pv : pts) {
      JsonArrayConst p = pv.as<JsonArrayConst>();
      if (p.size() != 2 || !p[0].is<float>() || !p[1].is<float>()) return err = "look.outline: cada ponto é [x, y]", false;
      L.outline.push_back({p[0].as<float>(), p[1].as<float>()});
      minY = fminf(minY, L.outline.back().y);
      maxY = fmaxf(maxY, L.outline.back().y);
    }

    JsonObjectConst w = look["wave"];
    L.wave = {num(w["from"], 15), num(w["span"], 75), num(w["speed"], 4), num(w["phase"], 0.07f)};
    if (L.wave.span <= 0) L.wave.span = 1;

    JsonObjectConst eyes = look["eyes"];
    if (!eyes["left"].is<float>() || !eyes["right"].is<float>() || !eyes["y"].is<float>()) {
      return err = "look.eyes precisa de left, right e y", false;
    }
    JsonObjectConst gaze = eyes["look"];
    L.eyes = {eyes["left"].as<float>(), eyes["right"].as<float>(), eyes["y"].as<float>(), num(eyes["rx"], 9), num(eyes["ry"], 14),
              num(gaze["left"], 12), num(gaze["right"], 12), num(gaze["y"], 9)};

    JsonObjectConst ch = look["cheeks"];
    L.cheeks = {num(ch["dx"], 14), num(ch["dy"], 20), num(ch["rx"], 7), num(ch["ry"], 4)};

    // Braços do tímido, cada um a partir de um olho
    static const Look::Arm ARMS[2] = {{-8, 24, 30, 211.5f, 301.5f}, {8, 24, 30, 328.5f, 238.5f}};
    JsonArrayConst arms = look["arms"];
    for (int i = 0; i < 2; ++i) {
      JsonObjectConst a = arms[i].as<JsonObjectConst>();
      const Look::Arm& d = ARMS[i];
      L.arms[i] = {num(a["dx"], d.dx), num(a["dy"], d.dy), a["r"] | d.r, num(a["from"], d.from), num(a["to"], d.to)};
    }
  }

  JsonObjectConst sh = look["shadow"];
  L.shadow = {num(sh["y"], roundf(maxY) + 18), num(sh["rx"], 46), num(sh["ry"], 6), num(sh["dx"], 0), num(sh["bob"], 1.2f)};

  JsonObjectConst an = look["anchors"];
  L.bubble = {num(an["bubble"][0], 60), num(an["bubble"][1], -40)};
  JsonObjectConst st = an["stars"];
  L.stars = {num(st["x"], 0), num(st["y"], roundf(minY) - 16), num(st["rx"], 62), num(st["ry"], 12)};
  JsonObjectConst zz = an["zzz"];
  L.zzz = {num(zz["x"], 50), num(zz["y"], roundf(minY) + 10), num(zz["dx"], 40), num(zz["dy"], -45)};

  // Sons: nome -> WAV da pasta (o PCM só entra ao carregar do cartão)
  out.sounds.clear();
  if (!doc["sounds"].isNull() && !doc["sounds"].is<JsonObjectConst>()) return err = "sounds precisa ser um objeto", false;
  for (JsonPairConst kv : doc["sounds"].as<JsonObjectConst>()) {
    String name = kv.key().c_str();
    if (out.sounds.size() >= OBA_SOUND_COUNT_MAX) return err = "sons demais (até " + String(OBA_SOUND_COUNT_MAX) + ")", false;
    if (!obaValidId(name)) return err = "sounds: nome inválido: " + name, false;
    if (out.sound(name.c_str())) return err = "sounds: repetido: " + name, false;
    auto clip = std::make_shared<SoundClip>();
    clip->name = name;
    clip->path = kv.value() | "";
    if (!obaValidFile(clip->path)) return err = "sounds." + name + ": caminho inválido", false;
    if (!out.fileSha(clip->path).length()) return err = "sounds." + name + " não está em files", false;
    out.sounds.push_back(clip);
  }

  // Humores
  defaultMoods(out.moods, rgb[0], rgb[5], rgb[6]);
  JsonObjectConst moods = doc["moods"];
  for (JsonPairConst kv : moods) {
    Mood m;
    if (!moodByName(kv.key().c_str(), &m)) return err = String("moods.") + kv.key().c_str() + " não existe", false;
    if (!parseMood(kv.value().as<JsonObjectConst>(), out.moods[(int)m], kv.key().c_str(), err)) return false;
  }

  // Reflexos: sem a chave, vale a tabela padrão
  if (doc["reflexes"].isNull()) defaultReflexes(out.reflexes);
  else if (!parseReflexes(doc["reflexes"].as<JsonArrayConst>(), out, out.reflexes, err)) return false;

  out.wakeWords.clear();
  for (JsonVariantConst v : doc["wake_words"].as<JsonArrayConst>()) {
    String s = v | "";
    s.trim();
    s.toLowerCase();
    if (s.length() && out.wakeWords.size() < WAKE_WORD_MAX) out.wakeWords.push_back(s);
  }
  out.requires.clear();
  for (JsonVariantConst v : doc["requires"].as<JsonArrayConst>()) out.requires.push_back(v | "");
  if (out.sounds.size() && std::find(out.requires.begin(), out.requires.end(), "speaker") == out.requires.end()) {
    return err = "com sounds, requires precisa ter \"speaker\"", false;
  }
  for (const Reflex& r : out.reflexes) {
    if (r.vibrate.steps && std::find(out.requires.begin(), out.requires.end(), "vibration") == out.requires.end()) {
      return err = "com vibrate nos reflexos, requires precisa ter \"vibration\"", false;
    }
  }
  out.agentEvents.clear();
  for (JsonVariantConst t : doc["agent"]["triggers"].as<JsonArrayConst>()) {
    String on = t["on"] | "";
    if (on.length()) out.agentEvents.push_back(on);
  }
  return true;
}

// ---------------------------------------------------------------- sha256

ObaSha256::ObaSha256() {
  mbedtls_sha256_init(&ctx);
#if MBEDTLS_VERSION_MAJOR >= 3
  mbedtls_sha256_starts(&ctx, 0);
#else
  mbedtls_sha256_starts_ret(&ctx, 0);
#endif
}

ObaSha256::~ObaSha256() { mbedtls_sha256_free(&ctx); }

void ObaSha256::update(const void* data, size_t len) {
#if MBEDTLS_VERSION_MAJOR >= 3
  mbedtls_sha256_update(&ctx, (const unsigned char*)data, len);
#else
  mbedtls_sha256_update_ret(&ctx, (const unsigned char*)data, len);
#endif
}

String ObaSha256::hex() {
  unsigned char out[32];
#if MBEDTLS_VERSION_MAJOR >= 3
  mbedtls_sha256_finish(&ctx, out);
#else
  mbedtls_sha256_finish_ret(&ctx, out);
#endif
  char txt[65];
  for (int i = 0; i < 32; ++i) snprintf(txt + i * 2, 3, "%02x", out[i]);
  return String(txt);
}

String obaSha256(const void* data, size_t len) {
  ObaSha256 sha;
  sha.update(data, len);
  return sha.hex();
}

// ---------------------------------------------------------------- biblioteca

static ObaSpec* active = nullptr;
static Preferences prefs;

const ObaSpec& oba() { return *active; }

static ObaSpec* loadBuiltin() {
  ObaSpec* s = new ObaSpec();
  String err;
  if (!obaParse(BUILTIN_OBA, strlen(BUILTIN_OBA), *s, err)) Serial.printf("[oba] embutido inválido: %s\n", err.c_str());
  s->sha256 = obaSha256(BUILTIN_OBA, strlen(BUILTIN_OBA));
  s->builtin = true;
  return s;
}

static String jsonPath(const String& id) { return String(OBAS_DIR) + "/" + id + "/oba.json"; }

// Um arquivo da pasta do Oba, lido aos pedaços pelos decodificadores. Tudo que
// passa (inclusive o que eles pulam) entra no sha256, conferido no fim.
class CardFile : public ObaReader {
 public:
  bool open(const String& dir, const String& rel, String& err) {
    String full = dir + "/" + rel;
    f = SD.exists(full) ? SD.open(full) : File();
    if (!f || f.isDirectory()) return err = "falta o arquivo " + rel, false;
    if (f.size() > OBA_FILE_MAX) return err = rel + ": grande demais (até 2 MB)", false;
    return true;
  }
  size_t read(uint8_t* buf, size_t len) override {
    uint8_t skip[512];
    size_t done = 0;
    while (done < len) {
      uint8_t* dst = buf ? buf + done : skip;
      size_t want = buf ? len - done : std::min(len - done, sizeof(skip));
      size_t got = f.read(dst, want);
      if (!got) break;
      sha.update(dst, got);
      done += got;
    }
    return done;
  }
  // Lê o que sobrou do arquivo e confere o sha256
  bool check(const String& rel, const String& expected, String& err) {
    while (read(nullptr, 4096) == 4096) {}
    f.close();
    if (sha.hex() != expected) return err = "sha256 não bate: " + rel, false;
    return true;
  }
  // O decodificador recusou: se o arquivo nem é o de files, esse é o erro
  bool failed(const String& rel, const String& expected, String& err) {
    String why = err;
    if (check(rel, expected, err)) err = rel + ": " + why;
    return false;
  }

 private:
  File f;
  ObaSha256 sha;
};

// Quadros e sons do Oba, da pasta para a PSRAM
static bool loadAssets(const String& dir, ObaSpec& s, String& err) {
  for (const auto& f : s.files) {
    if (!SD.exists(dir + "/" + f.first)) return err = "falta o arquivo " + f.first, false;
  }

  // Folhas: cada arquivo uma vez, mesmo usado por vários humores
  size_t spriteBytes = 0;
  for (int i = 0; s.lookType == LookType::Sprites && i < MOOD_COUNT; ++i) {
    SpriteLook::Frames& F = s.sprites.frames[i];
    for (int j = 0; j < i && !F.data; ++j) {
      if (s.sprites.frames[j].sheet == F.sheet) F.data = s.sprites.frames[j].data;
    }
    if (F.data) continue;
    CardFile in;
    if (!in.open(dir, F.sheet, err)) return false;
    auto sheet = spriteDecode(in, s.sprites.w, s.sprites.h, OBA_SPRITES_MAX - spriteBytes, err);
    if (!sheet) return in.failed(F.sheet, s.fileSha(F.sheet), err);
    if (!in.check(F.sheet, s.fileSha(F.sheet), err)) return false;
    spriteBytes += sheet->bytes();
    F.data = sheet;
  }

  // Sons
  size_t soundBytes = 0;
  for (auto& clip : s.sounds) {
    CardFile in;
    if (!in.open(dir, clip->path, err)) return false;
    auto loaded = std::make_shared<SoundClip>();
    loaded->name = clip->name;
    loaded->path = clip->path;
    int16_t* pcm = nullptr;
    bool ok = soundDecode(in, OBA_SOUNDS_MAX - soundBytes, &pcm, &loaded->samples, err);
    loaded->pcm = pcm;
    if (!ok) return in.failed(clip->path, s.fileSha(clip->path), err);
    if (!in.check(clip->path, s.fileSha(clip->path), err)) return false;
    soundBytes += loaded->samples * 2;
    clip = loaded;
  }
  return true;
}

ObaSpec* obaLoadDir(const String& dir, const String& id, String& err) {
  if (sdState() != SdState::Ready) return err = "sem cartão", nullptr;
  String path = dir + "/oba.json";
  // O exists antes evita o erro que o SD.open escreve no log quando falta o arquivo
  File f = SD.exists(path) ? SD.open(path) : File();
  if (!f) return err = "não está no cartão", nullptr;
  size_t n = f.size();
  if (n > OBA_JSON_MAX) return err = "oba.json grande demais", nullptr;
  char* buf = (char*)ps_malloc(n + 1);
  if (!buf) return err = "sem memória", nullptr;
  size_t got = f.read((uint8_t*)buf, n);
  f.close();
  ObaSpec* s = nullptr;
  if (got != n) {
    err = "erro de leitura";
  } else {
    s = new ObaSpec();
    s->sha256 = obaSha256(buf, n);
    if (!obaParse(buf, n, *s, err)) {
      delete s;
      s = nullptr;
    } else if (s->id != id) {
      err = "o id do JSON (" + s->id + ") não bate com a pasta";
      delete s;
      s = nullptr;
    }
  }
  free(buf);
  if (s && !loadAssets(dir, *s, err)) {
    delete s;
    s = nullptr;
  }
  return s;
}

static ObaSpec* loadFromCard(const String& id, String& err) {
  if (sdState() != SdState::Ready) return err = "sem cartão", nullptr;
  if (!obaValidId(id)) return err = "id inválido", nullptr;
  return obaLoadDir(String(OBAS_DIR) + "/" + id, id, err);
}

// O do cartão ganha do embutido quando os dois têm o mesmo id
ObaSpec* obaLoad(const String& id, String& err) {
  ObaSpec* s = loadFromCard(id, err);
  if (s) return s;
  ObaSpec* b = loadBuiltin();
  if (b->id == id) return b;
  delete b;
  return nullptr;
}

void obaUse(ObaSpec* spec, bool persist) {
  delete active;
  active = spec;
  if (persist) prefs.putString("active", spec->id);
  String look = spec->lookType == LookType::Sprites
                    ? "sprites " + String(spec->sprites.w) + "x" + String(spec->sprites.h)
                    : String((unsigned)spec->look.outline.size()) + " pontos";
  Serial.printf("[oba] ativo: %s %s (%s, %s, %u reflexos, %u sons)\n", spec->name.c_str(), spec->version.c_str(),
                spec->builtin ? "embutido" : "cartão", look.c_str(), (unsigned)spec->reflexes.size(),
                (unsigned)spec->sounds.size());
}

void obaBegin() {
  prefs.begin("oba", false);
  String id = prefs.getString("active", "");
  String err;
  ObaSpec* s = id.length() ? obaLoad(id, err) : nullptr;
  if (id.length() && !s) Serial.printf("[oba] %s: %s; fica o embutido\n", id.c_str(), err.c_str());
  obaUse(s ? s : loadBuiltin(), false);
}

bool obaActivate(const String& id, String& err) {
  ObaSpec* s = obaLoad(id, err);
  if (!s) return false;
  obaUse(s, true);
  return true;
}

bool obaValidPath(const String& path) {
  int slash = path.indexOf('/');
  if (slash <= 0 || !obaValidId(path.substring(0, slash)) || path.endsWith("/")) return false;
  if (path.indexOf("..") >= 0 || path.indexOf("//") >= 0 || path.length() > 96) return false;
  for (char c : path) {
    if (!isalnum((unsigned char)c) && c != '.' && c != '-' && c != '_' && c != '/') return false;
  }
  return true;
}

bool obaWriteFile(const String& path, const uint8_t* data, size_t len, String& err) {
  if (sdState() != SdState::Ready) return err = "sem cartão", false;
  if (!obaValidPath(path)) return err = "caminho inválido", false;
  String full = String(OBAS_DIR) + "/" + path;
  for (int i = full.indexOf('/', 1); i > 0; i = full.indexOf('/', i + 1)) {
    String dir = full.substring(0, i);
    if (!SD.exists(dir) && !SD.mkdir(dir)) return err = "não deu para criar " + dir, false;
  }
  File f = SD.open(full, FILE_WRITE);
  if (!f) return err = "não deu para abrir " + full, false;
  size_t n = f.write(data, len);
  f.close();
  if (n != len) return err = "cartão cheio ou com erro", false;
  return true;
}

bool obaRemoveTree(const String& path) {
  File dir = SD.open(path);
  if (!dir) return false;
  if (!dir.isDirectory()) {
    dir.close();
    return SD.remove(path);
  }
  std::vector<String> children;
  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) children.push_back(path + "/" + f.name());
  dir.close();
  bool ok = true;
  for (const String& c : children) ok = obaRemoveTree(c) && ok;
  return SD.rmdir(path) && ok;
}

bool obaRemove(const String& id, String& err) {
  if (sdState() != SdState::Ready) return err = "sem cartão", false;
  if (!obaValidId(id)) return err = "id inválido", false;
  if (active && !active->builtin && active->id == id) return err = "é o Oba ativo", false;
  String dir = String(OBAS_DIR) + "/" + id;
  if (!SD.exists(dir)) return err = "não está no cartão", false;
  if (!obaRemoveTree(dir)) return err = "não deu para apagar tudo", false;
  return true;
}

// Só id, nome e versão: o resto do JSON nem entra na memória
static bool readHeader(Stream& in, ObaInfo& info) {
  JsonDocument filter;
  filter["id"] = true;
  filter["name"] = true;
  filter["version"] = true;
  JsonDocument doc(&psram);
  if (deserializeJson(doc, in, DeserializationOption::Filter(filter))) return false;
  info = {doc["id"] | "", doc["name"] | "", doc["version"] | "0", true};
  return obaValidId(info.id);
}

std::vector<ObaInfo> obaList() {
  std::vector<ObaInfo> list;
  ObaInfo builtin;
  {
    ObaSpec* b = loadBuiltin();
    builtin = {b->id, b->name, b->version, false};
    delete b;
  }
  list.push_back(builtin);
  if (sdState() != SdState::Ready) return list;
  File root = SD.open(OBAS_DIR);
  if (!root) return list;
  for (File d = root.openNextFile(); d; d = root.openNextFile()) {
    if (!d.isDirectory()) continue;
    String id = d.name();
    d.close();
    if (id.startsWith(".")) continue;  // instalação em andamento ou sobra dela
    File f = SD.exists(jsonPath(id)) ? SD.open(jsonPath(id)) : File();
    ObaInfo info;
    if (!f || !readHeader(f, info) || info.id != id) continue;
    if (id == builtin.id) list[0] = info;
    else list.push_back(info);
  }
  return list;
}
