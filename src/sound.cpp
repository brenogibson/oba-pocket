#include "sound.h"
#include <M5Unified.h>
#include <algorithm>
#include <vector>
#include "audio.h"
#include "cloud.h"

// Quando isPlaying cai, o fim do som ainda está no DMA do I2S (~43 ms)
static constexpr uint32_t DRAIN_MS = 60;

static bool speakerOn = false;  // o alto-falante está com o I2S
static bool draining = false;
static uint32_t quietSince = 0;
// O que já foi para o alto-falante fica seguro aqui até ele desligar, mesmo
// se o Oba for trocado no meio
static std::vector<std::shared_ptr<const SoundClip>> held;

static void speakerOff() {
  M5.Speaker.end();
  speakerOn = false;
  draining = false;
  held.clear();
  audioResume();
}

bool soundPlay(const char* name, uint8_t volume, String& err) {
  if (cloudRecording()) return err = "com o REC ligado o Oba fica em silêncio", false;
  std::shared_ptr<const SoundClip> clip = oba().sound(name ? name : "");
  if (!clip || !clip->pcm) return err = "som desconhecido", false;
  // Sem o alto-falante na config, o begin mexeria no I2S do mic à toa
  if (!M5.Speaker.isEnabled()) return err = "sem alto-falante", false;
  if (!speakerOn) {
    audioPause();
    if (!M5.Speaker.begin()) {
      M5.Speaker.end();
      audioResume();
      return err = "o alto-falante não ligou", false;
    }
    speakerOn = true;
  }
  M5.Speaker.setVolume(volume);
  if (!M5.Speaker.playRaw(clip->pcm, clip->samples, OBA_SOUND_RATE, false, 1, 0, true)) {
    return err = "não deu para tocar", false;  // o soundUpdate desliga
  }
  if (std::find(held.begin(), held.end(), clip) == held.end()) held.push_back(clip);
  draining = false;
  return true;
}

void soundUpdate(uint32_t now) {
  if (!speakerOn) return;
  if (cloudRecording()) {
    soundStop();
    return;
  }
  if (M5.Speaker.isPlaying()) {
    draining = false;
    return;
  }
  if (!draining) {
    draining = true;
    quietSince = now;
    return;
  }
  if (now - quietSince >= DRAIN_MS) speakerOff();
}

bool soundPlaying() { return speakerOn; }

void soundStop() {
  if (speakerOn) speakerOff();
}

// ---------------------------------------------------------------- WAV

static uint16_t le16(const uint8_t* p) { return p[0] | p[1] << 8; }
static uint32_t le32(const uint8_t* p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }

// Pula o resto de um bloco (blocos de tamanho ímpar têm um byte a mais)
static bool skip(ObaReader& in, uint64_t n) { return n <= OBA_FILE_MAX && in.read(nullptr, n) == n; }

bool soundDecode(ObaReader& in, size_t budget, int16_t** pcm, size_t* samples, String& err) {
  *pcm = nullptr;
  *samples = 0;
  uint8_t h[16];
  if (in.read(h, 12) != 12 || memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4)) return err = "não é WAV (RIFF/WAVE)", false;
  bool fmt = false;
  for (;;) {
    if (in.read(h, 8) != 8) return err = fmt ? "WAV sem dados" : "WAV sem fmt", false;
    uint32_t size = le32(h + 4);
    uint64_t padded = (uint64_t)size + (size & 1);
    if (!memcmp(h, "fmt ", 4)) {
      if (size < 16 || in.read(h, 16) != 16) return err = "fmt do WAV cortado", false;
      if (le16(h) != 1 || le16(h + 2) != 1 || le32(h + 4) != OBA_SOUND_RATE || le16(h + 14) != 16) {
        return err = "o WAV precisa ser PCM, mono, 16000 Hz e 16 bits", false;
      }
      if (!skip(in, padded - 16)) return err = "WAV cortado", false;
      fmt = true;
    } else if (!memcmp(h, "data", 4)) {
      if (!fmt) return err = "WAV com dados antes do fmt", false;
      size_t n = size / 2;
      if (!n) return err = "som vazio", false;
      if (n > OBA_SOUND_RATE / 1000 * OBA_SOUND_MAX_MS) return err = "som com mais de " + String(OBA_SOUND_MAX_MS / 1000) + " s", false;
      if (n * 2 > budget) return err = "sons grandes demais (até " + String(OBA_SOUNDS_MAX / 1024) + " KB no total)", false;
      int16_t* buf = (int16_t*)ps_malloc(n * 2);
      if (!buf) return err = "sem memória", false;
      if (in.read((uint8_t*)buf, n * 2) != n * 2) {
        free(buf);
        return err = "WAV cortado", false;
      }
      *pcm = buf;
      *samples = n;
      return true;
    } else if (!skip(in, padded)) {
      return err = "WAV cortado", false;
    }
  }
}
