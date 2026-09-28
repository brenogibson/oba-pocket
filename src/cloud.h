// Conexão com o harness, numa task própria no core 0 (a animação fica no core 1):
//   - WiFi + NTP
//   - MQTT (AWS IoT Core) no Oba Protocol v1 (docs/protocol.md), nos tópicos
//     <prefixo>/<placa>/...
//   - credenciais temporárias via IoT credentials provider (certificado)
//   - áudio do mic -> Amazon Transcribe streaming (WebSocket), só com o REC
//   - regras armadas pelo agente ("quando X, faça Y"), casadas aqui mesmo para
//     disparar na hora
// Os comandos que mexem no Oba (react, look, leds, play, oba.install...) vão
// para o loop pela fila de cloudTakeCommand; o que o loop publica passa por cloudSend.
#pragma once
#include <stdint.h>
#include <vector>
#include "bubble.h"

// Offline: sem WiFi. Ready: conectado, gravação desligada (nenhum áudio sai
// da placa). Connecting/Streaming: gravação ligada.
enum class CloudState : uint8_t { Offline, Ready, Connecting, Streaming };

void cloudBegin();
CloudState cloudState();
bool cloudOnline();  // MQTT conectado

// Liga/desliga a gravação (stream para o Transcribe). Começa desligada.
// O harness pode desligar (comando rec), nunca ligar.
void cloudSetRecording(bool on);
bool cloudRecording();

// Oba ativo: o id vai em todas as mensagens, e as palavras de ativação (minúsculas)
// são procuradas na transcrição
void cloudSetOba(const String& id, const std::vector<String>& wakeWords);
// Quantas vezes uma palavra de ativação foi ouvida desde a última chamada
uint32_t cloudTakeKeywordHits();

// Próximo balão para mostrar (ou nullptr). Quem recebe libera com delete.
Bubble* cloudTakeBubble();
bool cloudBubblePending();

// A animação avisa se tem balão na tela: regra de fala só dispara com a tela livre
void cloudSetBubbleBusy(bool busy);

// Próximo comando do harness para o loop (o JSON inteiro), ou nullptr. Quem
// recebe libera com delete.
String* cloudTakeCommand();

// O harness pediu o state (conectou agora ou mandou o comando state)
bool cloudTakeStateRequest();

// Publica uma mensagem do protocolo que o loop montou. Nos eventos, evt é o type,
// para as regras armadas; publish = false só passa pelas regras (o Oba não pediu
// esse evento ao harness).
enum class Channel : uint8_t { Evt, Reply, State };
void cloudSend(Channel ch, const String& json, const char* evt = nullptr, bool publish = true);

// Hora de verdade (NTP) em ms, ou 0 enquanto não sincronizou
uint64_t cloudEpochMs();

// Teste: trata o texto como se o Transcribe tivesse ouvido (só dispara regras de fala)
void cloudSimulateLine(const String& text);
