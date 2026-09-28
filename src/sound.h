// Sons do Oba ativo (WAV 16 kHz, mono, 16 bits, já na PSRAM). O alto-falante e
// o mic dividem o I2S (GPIO0) no Core2: para tocar, o mic para, o alto-falante
// liga, toca, desliga, e o mic volta. Com o REC ligado não toca nada.
#pragma once
#include <Arduino.h>
#include "oba.h"

// Toca um som do Oba ativo (não bloqueia). Um som novo corta o que estiver
// tocando. volume: 1 a 255.
bool soundPlay(const char* name, uint8_t volume, String& err);
// No loop: o fim do som devolve o I2S ao mic; se o REC ligou, corta
void soundUpdate(uint32_t now);
// true enquanto o alto-falante está com o I2S (tocando ou terminando)
bool soundPlaying();
// Corta o som e devolve o I2S ao mic na hora
void soundStop();

// Lê um WAV (RIFF, PCM formato 1, mono, 16000 Hz, 16 bits) para a PSRAM.
// budget: bytes de PCM que ainda cabem. Quem recebe o pcm libera com free.
bool soundDecode(ObaReader& in, size_t budget, int16_t** pcm, size_t* samples, String& err);
