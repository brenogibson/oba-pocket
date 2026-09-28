// Balão de fala do Oba: o que o agente manda mostrar (comandos speak/arm
// em TOPIC_CMD). Criado pela task de rede e entregue à animação por uma fila;
// quem tira da fila é dono do balão e o libera com delete.
#pragma once
#include <Arduino.h>
#include <M5GFX.h>

enum class BubbleKind : uint8_t { Text, Image, Qr };

struct Bubble {
  BubbleKind kind = BubbleKind::Text;
  String id;
  String text;            // fala do Oba (UTF-8); legenda nos balões de imagem e QR
  String url;             // Qr
  uint8_t* png = nullptr;  // Image: PNG já decodificado do base64 (PSRAM)
  size_t pngLen = 0;

  Bubble() = default;
  Bubble(const Bubble&) = delete;
  Bubble& operator=(const Bubble&) = delete;
  ~Bubble() { free(png); }
};

// Ciclo do balão (bubble.cpp), dono: o loop. Pega o próximo da fila da nuvem,
// leva o Oba para o canto, abre, espera o tempo de leitura e fecha.
// Balão com fala longa + QR/ícone tem duas páginas: primeiro a fala inteira,
// depois vira e mostra o QR/ícone grande.
void bubbleBegin(M5Canvas* screen);
void bubbleUpdate(uint32_t now);
void bubbleDraw(M5Canvas& c, uint32_t now);
// Toque na tela: no balão aberto, vira a página ou fecha. true = era no balão.
bool bubbleTap(int x, int y, uint32_t now);
bool bubbleShowing();  // abrindo, aberto ou virando: o Oba olha para ele
bool bubbleIdle();     // nada na tela nem saindo

// Teste (freezeShot): abre este balão já aberto, na página dada, e o Oba já no canto
void bubbleTestOpen(Bubble* b, uint8_t page, uint32_t now);
void bubbleTestClose();
