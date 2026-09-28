// Comportamento do Oba: humores, olhar, piscada, virada e os reflexos que
// ligam o que a placa percebe (som, toque, IMU) a uma reação. O que cada
// reflexo faz e quanto cada humor dura vem do Oba ativo (oba.h).
#pragma once
#include <Arduino.h>
#include "oba.h"

// Onde o Oba mora quando não tem balão na tela
static constexpr float HOME_X = 160, HOME_Y = 116;
static constexpr uint32_t TURN_MS = 340;

struct Pet {
  Mood mood = Mood::Idle;
  uint32_t moodStart = 0, lastActivity = 0, lastLoud = 0;
  float lookX = 0, lookY = 0;  // -1..1, no referencial do Oba
  uint32_t blinkStart = 0;
  bool touching = false;
  int touchX = 0, touchY = 0;
  float facing = 1.f;      // 1 = como desenhado, -1 = espelhado
  uint32_t turnStart = 0;  // 0 = sem virada em andamento
  float x = HOME_X, y = HOME_Y, scale = 1.f;  // centro e tamanho na tela
};
extern Pet pet;

// Começa de novo, feliz ("oi!"): ao ligar e ao trocar de Oba
void petBegin(uint32_t now);
void petEnterMood(Mood m, uint32_t now);

// Microfone e IMU; disparam os reflexos de som e movimento
void petSense(uint32_t now);
// Cada evento percebido (com reflexo ou não) também vai para quem escuta,
// para o agente saber (protocol.cpp). dir é o lado do teco (imu.tap).
using PetListener = void (*)(Event e, float dir, uint32_t now);
void petListen(PetListener fn);
// Toque na área do Oba: segurar puxa o olhar, tocar dispara touch.tap
void petTouch(bool pressed, bool tapped, int x, int y, uint32_t now);
// Aplica o primeiro reflexo do Oba que casa com o evento no humor atual.
// dir é o lado do teco (imu.tap). Devolve false se nenhum casou.
bool petReact(Event e, uint32_t now, float dir = 0);
void petTurnTo(float dir, uint32_t now);
// Pedidos do agente: um humor, "glance" ou "turn" (como nos reflexos), e
// olhar para (x, y), de -1 a 1 na tela, por ms
bool petAct(const char* what, uint32_t now);
void petLookAt(float x, float y, uint32_t ms, uint32_t now);
// Nível do microfone agora e do ambiente (a média que o limiar acompanha)
void petMicLevel(float* rms, float* base);

// Fim de cada humor, olhar e piscada. lookAtBubble: o balão está aberto.
void petUpdate(uint32_t now, bool lookAtBubble);
// Abertura dos olhos: 1 = aberto, 0 = fechado
float petEyeOpen(uint32_t now);

// Linha de log com o humor e os sensores
void petLog();
