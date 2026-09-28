// Instalação pelo ar (oba.install / oba.chunk, docs/protocol.md). Roda no loop.
//
// O cabeçalho traz a lista de arquivos (com o oba.json) e o sha256 de cada um.
// Os pedaços chegam em ordem e vão direto para /obas/.tmp-<id>/, com o sha256
// calculado no caminho. Com tudo lá, a placa carrega o Oba de teste, pergunta
// na tela e só então troca a pasta.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <M5Unified.h>

static constexpr size_t INSTALL_FILES_MAX = 64;                  // contando o oba.json
static constexpr size_t INSTALL_FILE_MAX = 2 * 1024 * 1024;      // cada arquivo (oba.json: OBA_JSON_MAX)
static constexpr size_t INSTALL_TOTAL_MAX = 6 * 1024 * 1024;
static constexpr size_t INSTALL_CHUNK_MAX = 8192;                // bytes de um pedaço, já decodificado
static constexpr uint32_t INSTALL_IDLE_MS = 30000;               // sem pedaço por isso: desiste
static constexpr uint32_t INSTALL_ASK_MS = 60000;                // sem resposta na tela: recusa

// No boot, antes de obaBegin: apaga /obas/.tmp-* e desfaz uma troca pela metade
void installRecover();
// oba.install e oba.chunk (o comando inteiro, como chegou)
void installCommand(JsonDocument& cmd, uint32_t now);
// Tempo esgotado, conferência, confirmação na tela e troca. Bloqueia enquanto
// pergunta. true = o Oba ativo mudou (o loop chama applyOba).
bool installUpdate(M5Canvas& c, uint32_t now);
// Linha de progresso embaixo da tela. Fora de uma instalação não desenha nada.
void installDrawStatus(M5Canvas& c);
// O último installUpdate segurou o loop (conferência ou pergunta na tela)
bool installBlocked();
