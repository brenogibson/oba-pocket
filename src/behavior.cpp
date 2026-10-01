#include "behavior.h"
#include <M5Unified.h>
#include "audio.h"
#include "screen.h"
#include "sound.h"
#include "vibration.h"

// Microfone (blocos de ~32 ms, ver audio.h). O limiar se adapta ao ruído
// ambiente (baseline); os fatores dizem quantas vezes acima do ambiente conta
// como "voz" e "barulho alto".
static constexpr float VOICE_FACTOR = 2.2f;
static constexpr float VOICE_MIN    = 350.f;
static constexpr float LOUD_FACTOR  = 6.0f;
static constexpr float LOUD_MIN     = 2500.f;

// IMU
static constexpr float SHAKE_DPS   = 220.f;  // rotação (já sem o offset do sensor) p/ ficar tonto
static constexpr float TILT_GAIN   = 1.6f;
static constexpr float TILT_SIGN_X = 1.f;    // inverta se o olhar for pro lado errado
static constexpr float TILT_SIGN_Y = 1.f;
// O IMU não acompanha a rotação da tela: girada 180°, X e Y do sensor
// ficam invertidos em relação ao que aparece na tela.
static constexpr float IMU_FLIP    = SCREEN_ROTATION == 3 ? -1.f : 1.f;
// "Teco" pro lado: pico curto de aceleração lateral vira o Oba pra lá.
// A inclinação lenta não passa pelo filtro, só o tranco.
static constexpr float    TAP_G           = 0.40f;  // g acima da média recente
static constexpr float    TAP_SIGN        = 1.f;    // inverta se ele virar pro lado contrário
static constexpr uint32_t TAP_COOLDOWN_MS = 600;    // ignora o tranco de volta (quando a placa para)
static constexpr uint32_t TAP_QUIET_MS    = 600;    // barulho perto do teco (antes ou depois) não conta

Pet pet;

// Olhar
static float saccadeX = 0, saccadeY = 0;
static uint32_t nextSaccade = 0;
static float glanceX = 0, glanceY = 0;
static uint32_t glanceUntil = 0;
static uint32_t nextBlink = 2000;

// IMU
static bool imuSeeded = false;
static uint32_t imuSeededAt = 0;
static float restAx = 0, restAy = 0;
static float biasGx = 0, biasGy = 0, biasGz = 0;
static float tiltX = 0, tiltY = 0;
static float gyroRaw = 0, gyroEnergy = 0;
static float axFast = 0, tapPeak = 0;
static uint32_t lastTap = 0;

// Microfone
static float micRms = 0;
static float micBase = 150;

// Humor que um barulho interrompeu: se o "barulho" era o som do próprio teco
// na caixa, a reação é desfeita
static Mood moodBeforeLoud = Mood::Idle;
static bool moodFromLoud = false;

static PetListener listener = nullptr;

// Para onde os humores passageiros voltam (petSetRest)
static Mood restMood = Mood::Idle;

static void emit(Event e, uint32_t now, float dir = 0) {
  if (listener) listener(e, dir, now);
}

void petListen(PetListener fn) { listener = fn; }

static float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

static float randf(float lo, float hi) { return lo + (hi - lo) * (random(10001) / 10000.f); }

void petEnterMood(Mood m, uint32_t now) {
  pet.mood = m;
  pet.moodStart = now;
  moodFromLoud = false;
}

void petSetRest(Mood m, uint32_t now) {
  if (m == restMood) return;
  restMood = m;
  pet.lastActivity = now;  // mudou o que está acontecendo: acorda
  // Um passageiro (happy, scared...) termina sozinho e cai no descanso novo
  if (pet.mood == Mood::Idle || pet.mood == Mood::Sleepy || moodExternal(pet.mood)) petEnterMood(m, now);
}

void petBegin(uint32_t now) {
  pet.lastActivity = now;
  pet.facing = 1.f;
  pet.turnStart = 0;
  petEnterMood(Mood::Happy, now);
}

// ---------------------------------------------------------------- reflexos

static const Reflex* match(Event e) {
  for (const Reflex& r : oba().reflexes) {
    if (r.on == e && (r.moods >> (int)pet.mood & 1)) return &r;
  }
  return nullptr;
}

// O som do teco pode ter chegado antes do pico no acelerômetro
static void undoLoud(uint32_t now) {
  if (moodFromLoud && now - pet.moodStart < TAP_QUIET_MS) petEnterMood(moodBeforeLoud, now);
}

// "Hm? Quem falou?": olha rápido para um lado aleatório
static void glance(uint32_t now) {
  if (!(now > glanceUntil + 400)) return;
  glanceX = random(2) ? randf(0.6f, 1.f) : -randf(0.6f, 1.f);
  glanceY = randf(-0.5f, 0.2f);
  glanceUntil = now + 900;
}

// Volume dos sons dos reflexos
static constexpr uint8_t REFLEX_VOLUME = 128;

static void apply(const Reflex& r, Event e, uint32_t now, float dir) {
  if (r.sound.length()) {
    String err;
    soundPlay(r.sound.c_str(), REFLEX_VOLUME, err);  // com o REC ligado não toca, e tudo bem
  }
  if (r.vibrate.steps) vibrationStart(r.vibrate, now);
  switch (r.act) {
    case Reflex::ToMood: {
      Mood before = pet.mood;
      petEnterMood(r.mood, now);
      if (e == Event::SoundLoud) {
        moodBeforeLoud = before;
        moodFromLoud = true;
      }
      break;
    }
    case Reflex::Glance: glance(now); break;
    case Reflex::Turn:   petTurnTo(dir ? dir : -pet.facing, now); break;
    case Reflex::None:   break;
  }
}

bool petReact(Event e, uint32_t now, float dir) {
  const Reflex* r = match(e);
  if (r) apply(*r, e, now, dir);
  emit(e, now, dir);
  return r;
}

bool petAct(const char* what, uint32_t now) {
  Mood m;
  if (moodByName(what, &m)) {
    if (moodExternal(m)) return false;
    if (m != Mood::Sleepy) pet.lastActivity = now;  // o sonolento acorda com atividade
    petEnterMood(m, now);
  } else if (!strcmp(what, "glance")) {
    glance(now);
  } else if (!strcmp(what, "turn")) {
    petTurnTo(-pet.facing, now);
  } else {
    return false;
  }
  return true;
}

void petLookAt(float x, float y, uint32_t ms, uint32_t now) {
  glanceX = clampf(x, -1.f, 1.f) * pet.facing;  // o olhar é no referencial do Oba
  glanceY = clampf(y, -1.f, 1.f);
  glanceUntil = now + ms;
}

void petMicLevel(float* rms, float* base) {
  *rms = micRms;
  *base = micBase;
}

void petTurnTo(float dir, uint32_t now) {
  pet.lastActivity = now;
  undoLoud(now);
  if (dir == pet.facing) {
    glanceX = 1.f;  // já estava virado pra lá: só olha naquela direção
    glanceY = 0.f;
    glanceUntil = now + 600;
    return;
  }
  pet.facing = dir;
  pet.turnStart = now;
}

// ---------------------------------------------------------------- sensores

static void handleSound(uint32_t now) {
  if (!audioTakeLevel(&micRms)) return;
  if (vibrationActive(now)) return;  // o zumbido do próprio motor não é barulho

  float voiceTh = fmaxf(micBase * VOICE_FACTOR, VOICE_MIN);
  float loudTh  = fmaxf(micBase * LOUD_FACTOR, LOUD_MIN);

  // Ambiente adapta rápido no silêncio e devagar quando há som, para um
  // lugar movimentado não ficar disparando o tempo todo.
  float k = micRms < voiceTh ? 0.02f : 0.004f;
  micBase += (fminf(micRms, loudTh) - micBase) * k;

  if (micRms > loudTh) {
    pet.lastLoud = now;
    pet.lastActivity = now;
    // O barulho do próprio teco na caixa não conta
    if (now - lastTap >= TAP_QUIET_MS) petReact(Event::SoundLoud, now);
  } else if (micRms > voiceTh) {
    pet.lastActivity = now;
    petReact(Event::SoundVoice, now);
  }
}

static void handleImu(uint32_t now) {
  float ax, ay, az, gx, gy, gz;
  if (!M5.Imu.getAccel(&ax, &ay, &az) || !M5.Imu.getGyro(&gx, &gy, &gz)) return;

  if (!imuSeeded) {
    imuSeeded = true;
    imuSeededAt = now;
    restAx = ax; restAy = ay;
    axFast = ax;
    biasGx = gx; biasGy = gy; biasGz = gz;
    return;
  }

  // Teco: a média rápida acompanha a inclinação, então só o tranco sobra.
  // O primeiro pico aponta pro lado do empurrão; o de volta cai no cooldown.
  float hp = ax - axFast;
  tapPeak = fmaxf(tapPeak, fabsf(hp));
  // O tremor do próprio motor não é teco nem chacoalhão, nem fica nas médias
  // para virar um depois que a vibração acaba
  bool buzzing = vibrationActive(now);
  axFast = buzzing ? ax : axFast + hp * 0.25f;
  if (fabsf(hp) > TAP_G && now - lastTap > TAP_COOLDOWN_MS && !buzzing) {
    lastTap = now;
    float dir = (hp > 0 ? 1.f : -1.f) * TAP_SIGN * IMU_FLIP;
    Serial.printf("[imu] teco para a %s (%.2f g)\n", dir > 0 ? "direita" : "esquerda", fabsf(hp));
    pet.lastActivity = now;
    const Reflex* tap = match(Event::ImuTap);
    if (tap && tap->act != Reflex::Turn) undoLoud(now);
    if (tap) apply(*tap, Event::ImuTap, now, dir);
    emit(Event::ImuTap, now, dir);
  }

  // Posição de repouso é reaprendida devagar (rápido nos primeiros segundos),
  // então funciona tanto com a placa deitada quanto em pé num suporte.
  float kRest = now - imuSeededAt < 2000 ? 0.1f : 0.004f;
  restAx += (ax - restAx) * kRest;
  restAy += (ay - restAy) * kRest;
  tiltX = clampf((ax - restAx) * TILT_GAIN * TILT_SIGN_X * IMU_FLIP, -1.f, 1.f);
  tiltY = clampf((ay - restAy) * TILT_GAIN * TILT_SIGN_Y * IMU_FLIP, -1.f, 1.f);

  // O giroscópio tem um offset que não é zero com a placa parada; ele é
  // rastreado e descontado, aprendendo mais rápido quando está quieto.
  float dx = gx - biasGx, dy = gy - biasGy, dz = gz - biasGz;
  gyroRaw = sqrtf(gx * gx + gy * gy + gz * gz);
  float g = sqrtf(dx * dx + dy * dy + dz * dz);
  float kBias = g < 40.f ? 0.02f : 0.001f;
  biasGx += dx * kBias; biasGy += dy * kBias; biasGz += dz * kBias;

  gyroEnergy = gyroEnergy * 0.8f + (buzzing ? 0.f : g * 0.2f);
  if (gyroEnergy > SHAKE_DPS && !buzzing) {
    pet.lastActivity = now;
    petReact(Event::ImuShake, now);
  }
}

void petSense(uint32_t now) {
  handleSound(now);
  handleImu(now);
}

void petTouch(bool pressed, bool tapped, int x, int y, uint32_t now) {
  pet.touching = pressed;
  if (pressed) {
    pet.touchX = x;
    pet.touchY = y;
    pet.lastActivity = now;
  }
  if (tapped) {
    pet.touchX = x;  // onde tocou, para o evento
    pet.touchY = y;
    pet.lastActivity = now;
    petReact(Event::TouchTap, now);
  }
}

// ---------------------------------------------------------------- humor e olhar

static void updateMood(uint32_t now) {
  const MoodSpec& m = oba().mood(pet.mood);
  uint32_t age = now - pet.moodStart;
  switch (pet.mood) {
    case Mood::Idle:
      if (restMood != Mood::Idle) petEnterMood(restMood, now);
      else if (m.ms && now - pet.lastActivity > m.ms) petEnterMood(m.then, now);
      break;
    case Mood::Sleepy:
      if (restMood != Mood::Idle) petEnterMood(restMood, now);
      else if (now - pet.lastActivity < 200) petEnterMood(m.then, now);  // acordou com toque/voz
      break;
    case Mood::Busy:
    case Mood::Alert:
      if (pet.mood != restMood) petEnterMood(restMood, now);
      break;
    default:
      if (m.ms && age > m.ms && (!m.quietMs || now - pet.lastLoud > m.quietMs)) {
        petEnterMood(m.then == Mood::Idle ? restMood : m.then, now);
      }
      break;
  }
}

static void updateLook(uint32_t now, bool lookAtBubble) {
  const auto& eyes = oba().look.eyes;
  float tx, ty;
  float speed = 0.18f;
  if (pet.touching) {
    float eyeCx = pet.x + pet.facing * (eyes.left + eyes.right) * 0.5f * pet.scale;
    tx = (pet.touchX - eyeCx) / 90.f * pet.facing;  // olhar é no referencial do Oba
    ty = (pet.touchY - (pet.y + eyes.y * pet.scale)) / 70.f;
    speed = 0.35f;
  } else if (lookAtBubble) {
    tx = pet.facing;  // olha para o balão (que fica à direita)
    ty = -0.3f;
    speed = 0.2f;
  } else if (now < glanceUntil) {
    tx = glanceX;
    ty = glanceY;
    speed = 0.4f;
  } else {
    if (now > nextSaccade) {
      if (random(10) < 3) {
        saccadeX = saccadeY = 0;   // volta para a pose do desenho
      } else {
        saccadeX = randf(-0.8f, 0.8f);
        saccadeY = randf(-0.6f, 0.6f);
      }
      nextSaccade = now + random(700, 3000);
    }
    tx = saccadeX;
    ty = saccadeY;
  }
  tx = clampf(tx + tiltX * pet.facing, -1.f, 1.f);
  ty = clampf(ty + tiltY, -1.f, 1.f);
  pet.lookX += (tx - pet.lookX) * speed;
  pet.lookY += (ty - pet.lookY) * speed;

  if (now > nextBlink) {
    pet.blinkStart = now;
    nextBlink = now + (random(5) == 0 ? 350 : random(2000, 6000));  // às vezes pisca duas vezes
  }
}

void petUpdate(uint32_t now, bool lookAtBubble) {
  updateMood(now);
  updateLook(now, lookAtBubble);
}

float petEyeOpen(uint32_t now) {
  if (oba().mood(pet.mood).eyes == Eyes::Closing) {  // pegando no sono
    float age = (now - pet.moodStart) / 3000.f;
    return clampf(1.f - age, 0.f, 1.f) * 0.6f;
  }
  float p = (now - pet.blinkStart) / 90.f;
  if (p < 2.f) return fabsf(1.f - p);
  return 1.f;
}

void petLog() {
  Serial.printf("mood=%s mic=%.0f base=%.0f gyro_raw=%.0f gyro=%.0f tilt=(%.2f,%.2f) tap=%.2f bat=%d%% %dmV %s\n",
                moodName(pet.mood), micRms, micBase, gyroRaw, gyroEnergy, tiltX, tiltY, tapPeak,
                M5.Power.getBatteryLevel(), M5.Power.getBatteryVoltage(),
                M5.Power.isCharging() == m5::Power_Class::is_charging ? "carregando" : "");
  tapPeak = 0;
}
