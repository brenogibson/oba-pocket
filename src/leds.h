// Barra de LEDs laterais: cada humor do Oba escolhe um efeito (oba.h, LedFx)
#pragma once
#include "oba.h"

void ledsBegin();
void ledsShow(const LedFx& fx, uint32_t now, float t);
