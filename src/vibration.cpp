#include "vibration.h"
#include <M5Unified.h>

// Depois de desligar, o motor ainda gira um pouco
static constexpr uint32_t SETTLE_MS = 150;

static VibPattern pat{};
static uint8_t step = 0;  // passos pares ligam, ímpares desligam
static uint32_t stepEnd = 0;
static bool running = false;
static bool settling = false;  // parou há pouco: os sensores ainda sentem o motor
static uint32_t stoppedAt = 0;

static void stop(uint32_t now) {
  running = false;
  M5.Power.setVibration(0);
  settling = true;
  stoppedAt = now;
}

void vibrationStart(const VibPattern& p, uint32_t now) {
  if (!p.steps) return;
  pat = p;
  if (pat.steps > VIBRATION_STEPS_MAX) pat.steps = VIBRATION_STEPS_MAX;
  if (!pat.level) pat.level = 1;
  step = 0;
  stepEnd = now + pat.ms[0];
  running = true;
  M5.Power.setVibration(pat.level);
  uint32_t total = 0;
  for (uint8_t i = 0; i < pat.steps; i++) total += pat.ms[i];
  Serial.printf("[vib] %u passos, %u ms, força %u\n", pat.steps, (unsigned)total, pat.level);
}

void vibrationUpdate(uint32_t now) {
  // Acomodou: sai do settling logo, para o stoppedAt velho não dar a volta com o millis
  if (settling && (int32_t)(now - stoppedAt) >= (int32_t)SETTLE_MS) settling = false;
  while (running && (int32_t)(now - stepEnd) >= 0) {
    if (++step >= pat.steps) {
      stop(now);
      break;
    }
    M5.Power.setVibration(step % 2 ? 0 : pat.level);
    stepEnd += pat.ms[step];
  }
}

void vibrationStop() {
  if (running) stop(millis());
}

bool vibrationActive(uint32_t now) {
  // Negativo: o vibrationStop usou um millis() mais novo que o now do quadro
  return running || (settling && (int32_t)(now - stoppedAt) < (int32_t)SETTLE_MS);
}
