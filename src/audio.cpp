#include "audio.h"
#include <M5Unified.h>
#include <math.h>

// Anel de buffers: dois ficam com o driver do mic, o resto espera na fila
// até a task de rede enviar. A fila é menor que o anel, então um buffer
// nunca é regravado enquanto ainda está na fila.
static constexpr int RING = 26;
static constexpr int QUEUE_LEN = RING - 3;

static int16_t* ring[RING];
static volatile int nextSlot = 0;
static QueueHandle_t queue;

static volatile float levelMax = 0;
static volatile bool levelFresh = false;

static bool ready = false;   // audioBegin deu certo
static bool paused = false;  // o alto-falante está com o I2S
static constexpr uint32_t RESUME_MUTE_MS = 300;
static bool muted = false;
static uint32_t muteUntil = 0;

static void onBufferFull(void*, void* data, size_t length) {
  const int16_t* s = (const int16_t*)data;
  int32_t sum = 0;
  for (size_t i = 0; i < length; ++i) sum += s[i];
  float mean = sum / (float)length;
  float acc = 0;
  for (size_t i = 0; i < length; ++i) {
    float d = s[i] - mean;
    acc += d * d;
  }
  float rms = sqrtf(acc / length);
  if (!levelFresh || rms > levelMax) levelMax = rms;
  levelFresh = true;

  xQueueSend(queue, &data, 0);  // fila cheia (rede travada): o bloco é perdido

  M5.Mic.record(ring[nextSlot], AUDIO_CHUNK);
  nextSlot = (nextSlot + 1) % RING;
}

bool audioBegin() {
  for (int i = 0; i < RING; ++i) {
    ring[i] = (int16_t*)heap_caps_malloc(AUDIO_CHUNK * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!ring[i]) return false;
  }
  queue = xQueueCreate(QUEUE_LEN, sizeof(int16_t*));

  auto cfg = M5.Mic.config();
  cfg.sample_rate = AUDIO_RATE;
  M5.Mic.config(cfg);
  M5.Mic.setBufferReleaseCallback(nullptr, onBufferFull);
  if (!M5.Mic.begin()) return false;

  nextSlot = 2;
  ready = M5.Mic.record(ring[0], AUDIO_CHUNK, AUDIO_RATE) &&
          M5.Mic.record(ring[1], AUDIO_CHUNK, AUDIO_RATE);
  return ready;
}

bool audioTakeLevel(float* rms) {
  if (muted) {
    if ((int32_t)(millis() - muteUntil) < 0) {
      levelFresh = false;
      return false;
    }
    muted = false;
  }
  if (!levelFresh) return false;
  *rms = levelMax;
  levelFresh = false;
  return true;
}

bool audioPop(const int16_t** chunk, uint32_t waitMs) {
  return xQueueReceive(queue, (void*)chunk, pdMS_TO_TICKS(waitMs)) == pdTRUE;
}

void audioFlush() {
  if (queue) xQueueReset(queue);
}

void audioPause() {
  if (!ready || paused) return;
  M5.Mic.end();  // espera a task do mic parar; os dois blocos com o driver se perdem
  paused = true;
}

void audioResume() {
  if (!paused) return;
  paused = false;
  audioFlush();
  muted = true;
  muteUntil = millis() + RESUME_MUTE_MS;
  // Recomeça o anel como no audioBegin: dois blocos com o driver
  int slot = nextSlot;
  nextSlot = (slot + 2) % RING;
  if (!M5.Mic.begin() || !M5.Mic.record(ring[slot], AUDIO_CHUNK, AUDIO_RATE) ||
      !M5.Mic.record(ring[(slot + 1) % RING], AUDIO_CHUNK, AUDIO_RATE)) {
    Serial.println("[audio] o mic não voltou");
  }
}
