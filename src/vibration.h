// Motor de vibração: um toque só (comando vibrate, pedido na tela) ou um padrão
// liga, desliga, liga... (reflexos do Oba). Quem anda com ele é o loop.
#pragma once
#include <Arduino.h>

static constexpr uint8_t VIBRATION_STEPS_MAX = 8;
static constexpr uint32_t VIBRATION_STEP_MIN_MS = 20;  // menos que isso o motor nem gira
static constexpr uint32_t VIBRATION_MS_MAX = 2000;     // somando todos os passos
static constexpr uint8_t VIBRATION_LEVEL = 200;        // força padrão (1..255)

// ms[0] liga, ms[1] desliga, ms[2] liga... steps = 0: sem vibração
struct VibPattern {
  uint16_t ms[VIBRATION_STEPS_MAX];
  uint8_t steps;
  uint8_t level;
};

// Troca o que estiver vibrando por este padrão
void vibrationStart(const VibPattern& p, uint32_t now);
// Passa para o próximo passo e desliga no fim (todo quadro)
void vibrationUpdate(uint32_t now);
// Desliga agora (antes de algo que segura o loop)
void vibrationStop();
// Vibrando, ou parou há pouco: o IMU e o microfone sentem o próprio motor
bool vibrationActive(uint32_t now);
