// Captura contínua do microfone em blocos de 32 ms, sem buracos entre eles.
// O nível (RMS) de cada bloco alimenta as reações do Oba e as amostras
// vão para o Transcribe.
#pragma once
#include <stdint.h>
#include <stddef.h>

static constexpr uint32_t AUDIO_RATE  = 16000;
static constexpr size_t   AUDIO_CHUNK = 512;   // amostras por bloco (~32 ms)

// Chame depois de M5.begin(); o mic fica rodando em background.
bool audioBegin();

// Maior RMS desde a última chamada; false se não chegou bloco novo.
bool audioTakeLevel(float* rms);

// Próximo bloco para o streaming (AUDIO_CHUNK amostras, mono, 16 bits).
// O ponteiro é válido por ~800 ms, bem mais que o tempo de envio.
bool audioPop(const int16_t** chunk, uint32_t waitMs);

// Descarta os blocos acumulados (ex.: antes de abrir um stream novo).
void audioFlush();

// Para o mic e devolve o I2S (para o alto-falante tocar um som).
void audioPause();
// Liga o mic de novo, descarta o que estava na fila e ignora o nível por
// ~300 ms (o fim do som não assusta o Oba).
void audioResume();
