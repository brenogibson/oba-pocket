// Oba Protocol v1, o lado do loop (docs/protocol.md). O que a placa conta ao
// harness (state, eventos, respostas) e os comandos que mexem no Oba ou leem
// os sensores. O transporte e as regras armadas ficam em cloud.cpp.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include "oba.h"

static constexpr const char* FW_VERSION = "0.5.1";

// Ouve os eventos do Oba (behavior.h) e publica o primeiro state
void protoBegin();

// Comandos do harness, state quando pedido ou quando algo mudou, passos da
// vibração. Devolve true se um comando trocou o Oba ativo (o loop chama applyOba).
bool protoUpdate(uint32_t now);

// Os Obas instalados ou o ativo mudaram: lê a lista de novo e publica o state
void protoObasChanged();
// Algo que entra no state mudou (as fontes externas): publica de novo
void protoStateChanged();

// Efeitos que o agente e as fontes externas pedem: react, vibrate, leds e play.
// false = não é um desses; err diz por que um deles não rodou.
bool protoEffect(const JsonDocument& cmd, uint32_t now, String& err);
void protoVibrate(uint32_t ms, int level, uint32_t now);

// Botões virtuais da esquerda e da direita ('a' e 'c')
void protoButton(char which, uint32_t now);
// O balão com esse id fechou
void protoBubbleDone(const String& id);
// Resposta a um comando (reply). Sem id só vai para o log. extra: campos a mais
// (stage, next, active...), copiados como estão.
void protoReply(const char* id, const char* re, bool ok, const String& error = "",
                const JsonDocument* extra = nullptr);
// Teste pela serial: publica o evento como se a placa tivesse percebido
void protoSimulate(const String& type, uint32_t now);

// Efeito dos LEDs: o pedido pelo agente enquanto vale, senão o do humor
const LedFx& protoLeds(const LedFx& mood, uint32_t now);
