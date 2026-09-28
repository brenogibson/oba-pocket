// Oba "sprites": quadros PNG de pixel art. Ao carregar, cada folha é
// decodificada uma vez para a PSRAM; a cada quadro, o rig chama spriteDraw na
// mesma pose que usaria para o corpo (centro, escala, espelho e achatado).
#pragma once
#include <M5Unified.h>
#include <memory>
#include "oba.h"

// Desenha o quadro atual do humor m (age: ms desde que o humor começou) com o
// centro do Oba em (cx, cy) e escala (sx, sy) na tela (sx < 0 = espelhado).
// Vizinho mais próximo, direto no buffer do canvas.
void spriteDraw(M5Canvas& c, const SpriteLook& s, Mood m, uint32_t age, float cx, float cy, float sx, float sy);

// Decodifica uma folha PNG (tira de N quadros w x h, 1 <= N <= 32, sem
// entrelaçamento) para RGB565 + máscara na PSRAM. budget: bytes que ainda
// cabem (largura x altura x 3). Nulo e err se não der.
std::shared_ptr<SpriteSheet> spriteDecode(ObaReader& in, uint16_t w, uint16_t h, size_t budget, String& err);
