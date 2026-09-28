#pragma once
#include <Arduino.h>

// Cartão microSD: a biblioteca de Obas (/obas/<id>/). Ele divide o SPI com a
// tela, mas cada acesso é uma transação com a mesma trava do HAL que o M5GFX
// usa, então os dois nunca se atropelam. O framework só lê FAT32 (exFAT vem
// desligado).
enum class SdState : uint8_t { None, Ready, Unformatted };

SdState sdBegin();  // monta; cartão sem FAT32 fica Unformatted, sem apagar nada
bool sdFormat();    // apaga tudo, formata em FAT32 e monta
SdState sdState();
