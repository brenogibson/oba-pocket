// Desenho do Oba: fundo, sombra, corpo (polígono preenchido por scanline) e o
// rosto do humor atual, tudo a partir do look do Oba ativo. Nos sprites, a
// mesma pose (sobe/desce, balanço, achatar, virada) desenha o quadro do humor
// no lugar do corpo e do rosto (sprite.h).
#pragma once
#include <M5Unified.h>

// Desenha o quadro no canvas (sem mandar para a tela)
void rigDraw(M5Canvas& c, uint32_t now, float t);

// Ponto do Oba (coordenadas do look) na tela, na pose do último quadro
void rigPoint(float x, float y, int* sx, int* sy);
