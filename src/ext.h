// Fontes externas (docs/protocol.md, "Fontes externas"): outros programas, como
// a ponte do Claude Code, contam o que estão fazendo e pedem aprovação pela placa.
//   - status: o humor de repouso do Oba (idle, busy ou alert) e um rótulo no alto
//     da tela
//   - ask: um pedido num cartão, com Negar e Aprovar por toque. O botão do meio
//     manda para o terminal (skip). Aprovar só vale pelo toque na placa.
// As mensagens chegam de cloud.cpp (cloudTakeExt) e as respostas voltam por
// cloudSendExt. Tudo aqui roda no loop.
#pragma once
#include <M5Unified.h>
#include <ArduinoJson.h>

void extBegin(M5Canvas* screen);
// Mensagens novas, validade das fontes e dos pedidos, humor de repouso e o
// próximo pedido (só com a tela livre de balões)
void extUpdate(uint32_t now);
// O cartão e os botões do pedido, ou o rótulo das fontes (depois do balão)
void extDraw(M5Canvas& c, uint32_t now);
// Toque com o pedido na tela: true = era dele (o Oba não recebe)
bool extTouch(const m5::touch_detail_t& td, uint32_t now);
// Botão do meio: com o pedido na tela, responde skip. true = tinha pedido.
bool extSkip(uint32_t now);

bool extShowing();  // o cartão na tela (ou abrindo, ou fechando)
bool extAsking();   // o cartão na tela ou um pedido esperando: balão novo não abre
// Onde o Oba fica enquanto tem pedido (false = sem pedido)
bool extPlacement(float* x, float* y, float* scale);
// As fontes no state (state.ext); sem fontes, a chave não vai
void extState(JsonDocument& d);

// Teste pela serial: como se json tivesse chegado em ext/<src>, e a resposta
// ao pedido na tela sem o toque ('Y'/'N')
void extInject(const String& src, const String& json, uint32_t now);
bool extAnswer(bool allow, uint32_t now);
