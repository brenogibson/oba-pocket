#pragma once
#include <M5Unified.h>
#include <vector>

// Quebra o texto em linhas que cabem em maxW com a fonte atual do sprite
std::vector<String> wrapText(M5Canvas& c, const String& text, int maxW);

// Telas do sistema, fora do Oba: fundo escuro e a fonte do balão (com acentos).
// Desenham no canvas e já mandam para a tela.

// Aviso sem resposta, como "Formatando o cartão…"
void uiNotice(M5Canvas& c, const char* title, const char* body);

// Pergunta para ações que apagam coisas: só aceita segurando o botão por 3 s.
// Soltar antes zera; tocar fora dele ou esperar 30 s recusa. Bloqueia até decidir.
bool uiConfirmHold(M5Canvas& c, const char* title, const char* body, const char* action);

// Pergunta com dois botões por toque: yes à direita, no à esquerda. 'Y'/'N' na
// serial também respondem. Sem resposta em timeoutMs = não. No body, '\n'
// separa parágrafos. Bloqueia até decidir.
bool uiConfirm(M5Canvas& c, const char* title, const char* body, const char* yes, const char* no,
               uint32_t timeoutMs);

// Manda o quadro do canvas (RGB565 como está no buffer) em base64 pela serial,
// uma linha por linha da tela; tools/monitor.py monta o PNG (com o nome, se vier)
void uiDumpScreen(M5Canvas& c, const char* name = "");
