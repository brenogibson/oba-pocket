// Oba: o bichinho que mora na placa, descrito por um JSON (schema/oba.schema.json).
// Ele diz como o corpo é (contorno e olhos do rig, ou quadros PNG), como cada
// humor se mexe, quanto dura e o que acende nos LEDs, quais reflexos e sons ele
// tem. O motor (rig.cpp, sprite.cpp, behavior.cpp, bubble.cpp, sound.cpp) só lê daqui.
//
// A biblioteca fica no cartão, em /obas/<id>/ (oba.json, sprites/, sons/). O Oba
// ativo é carregado inteiro na memória ao ativar (quadros já decodificados e sons
// na PSRAM): a animação nunca lê do cartão. Sem cartão, vale o Oba embutido no
// firmware (builtin_oba.h).
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <mbedtls/sha256.h>
#include <memory>
#include <utility>
#include <vector>

// Os humores do motor. Cada Oba escolhe como eles aparecem.
enum class Mood : uint8_t { Idle, Scared, Shy, Happy, Dizzy, Sleepy };
static constexpr int MOOD_COUNT = 6;
const char* moodName(Mood m);
bool moodByName(const char* name, Mood* out);

// O que a placa percebe e os reflexos podem tratar
enum class Event : uint8_t { TouchTap, SoundLoud, SoundVoice, ImuShake, ImuTap, Wake, BubbleOpen };
static constexpr int EVENT_COUNT = 7;
const char* eventName(Event e);

// Um canal de movimento: constante (a), senoide sinf(t*a)*b + c,
// quique -|sinf(t*a)|*b + c, ou tremido inteiro sorteado entre a e b
struct Motion {
  enum Kind : uint8_t { Const, Sin, Bounce, Jitter };
  Kind kind = Const;
  float a = 0, b = 0, c = 0;
};

enum class Eyes : uint8_t { Open, Closing, Happy, Scared, Spiral, Covered };
enum class Cheeks : uint8_t { Off, On, Touch };
enum class Extra : uint8_t { None, Stars, Zzz };

struct LedFx {
  enum Kind : uint8_t { Off, Solid, Breathe, Rainbow, Strobe, Chase };
  Kind kind = Off;
  uint32_t color = 0, trail = 0;  // 0xRRGGBB
  float min = 0, max = 1, speed = 1;
  uint32_t ms = 100;
};

struct MoodSpec {
  Motion bob, sway, squash, wave;  // sobe/desce, lado a lado, achata, balanço da barra
  Eyes eyes = Eyes::Open;
  Cheeks cheeks = Cheeks::Off;
  Extra extra = Extra::None;
  float dy = 0;  // o rosto todo desce (ou sobe) esse tanto
  LedFx leds;
  // Quanto dura e para onde vai depois. No idle, ms é o tempo sem interação
  // até dormir (0 = nunca); o sonolento acorda com qualquer atividade.
  uint32_t ms = 0, quietMs = 0;
  Mood then = Mood::Idle;
};

struct Reflex {
  enum Act : uint8_t { ToMood, Glance, Turn, None };
  Event on;
  Act act;
  Mood mood;      // ToMood
  uint8_t moods;  // em quais humores vale (bit 1 << humor)
  String sound;   // nome de um som do Oba; "" = sem som
};

struct Pt { float x, y; };

// Aparência "rig": corpo vetorial e rosto paramétrico. Coordenadas em pixels,
// relativas ao centro do Oba, com y para baixo.
struct Look {
  struct { uint16_t bg, shadow, body, outline, eye, blush, star, text, bubble, ink, rec, wait, off; } color;  // RGB565
  std::vector<Pt> outline;
  struct { float from, span, speed, phase; } wave;  // a barra balança abaixo de from
  struct { float left, right, y, rx, ry, lookLeft, lookRight, lookY; } eyes;
  struct { float dx, dy, rx, ry; } cheeks;  // a partir de cada olho
  struct Arm { float dx, dy; int r; float from, to; } arms[2];  // a partir de cada olho
  struct { float y, rx, ry, dx, bob; } shadow;
  Pt bubble;  // ponta do rabinho do balão
  struct { float x, y, rx, ry; } stars;
  struct { float x, y, dx, dy; } zzz;
};

enum class LookType : uint8_t { Rig, Sprites };

// Uma folha de quadros (tira horizontal de count quadros w x h) decodificada na
// PSRAM. Os pixels já estão na ordem de bytes do M5Canvas de 16 bits (RGB565
// big-endian) e a máscara diz quais são opacos.
struct SpriteSheet {
  uint16_t w = 0, h = 0, count = 0;
  uint16_t* pixels = nullptr;  // (w * count) x h, linha a linha
  uint8_t* mask = nullptr;     // 1 = opaco
  size_t bytes() const { return (size_t)w * count * h * 3; }
  ~SpriteSheet() {
    free(pixels);
    free(mask);
  }
};

// Aparência "sprites": quadros PNG de pixel art com os movimentos do rig
struct SpriteLook {
  uint16_t w = 0, h = 0;      // um quadro, em pixels do PNG
  float originX = 0, originY = 0;  // ponto do quadro que fica no centro do Oba
  uint8_t scale = 1;          // pixels da tela por pixel do PNG (com pet.scale = 1)
  struct Frames {
    String sheet;  // caminho relativo do PNG
    uint8_t fps = 6;
    std::shared_ptr<const SpriteSheet> data;  // nulo até carregar do cartão
  } frames[MOOD_COUNT];  // humor sem entrada: os do idle
};

// Um som já na PSRAM (PCM 16 bits, 16 kHz, mono). Quem está tocando segura o
// shared_ptr, então o som dura mesmo se o Oba for trocado no meio.
struct SoundClip {
  String name, path;
  const int16_t* pcm = nullptr;  // nulo até carregar do cartão
  size_t samples = 0;
  ~SoundClip() { free((void*)pcm); }
};

// Um arquivo lido aos pedaços, para os decodificadores de quadros e sons.
// buf nulo = pular len bytes. Devolve quantos bytes leu (ou pulou): menos que
// len só no fim do arquivo.
class ObaReader {
 public:
  virtual size_t read(uint8_t* buf, size_t len) = 0;

 protected:
  ~ObaReader() = default;
};

struct ObaSpec {
  String id, name, version;
  String sha256;  // dos bytes do oba.json (ou do BUILTIN_OBA), hex minúsculo
  LookType lookType = LookType::Rig;
  Look look;         // no sprites: só cores, sombra e âncoras
  SpriteLook sprites;
  std::vector<std::shared_ptr<const SoundClip>> sounds;
  std::vector<std::pair<String, String>> files;  // caminho relativo, sha256 (hex minúsculo)
  MoodSpec moods[MOOD_COUNT];
  std::vector<Reflex> reflexes;  // o primeiro que casa ganha
  std::vector<String> wakeWords;
  std::vector<String> requires;
  std::vector<String> agentEvents;  // eventos que acordam o agente (agent.triggers[].on)
  bool builtin = false;

  const MoodSpec& mood(Mood m) const { return moods[(int)m]; }
  // Som pelo nome (nulo se não tem)
  std::shared_ptr<const SoundClip> sound(const char* name) const;
  // sha256 de um arquivo de files ("" se não está lá)
  const String& fileSha(const String& path) const;
};

static constexpr const char* OBAS_DIR = "/obas";
static constexpr size_t OBA_JSON_MAX = 64 * 1024;
static constexpr size_t OBA_OUTLINE_MAX = 256;
static constexpr size_t OBA_FILE_MAX = 2 * 1024 * 1024;  // cada arquivo da pasta
static constexpr size_t OBA_FILES_MAX = 63;               // itens de files (fora o oba.json)
static constexpr int OBA_FILE_DIRS_MAX = 4;               // pastas num caminho de files (apagar é recursivo)
static constexpr size_t OBA_SPRITES_MAX = 1024 * 1024;   // decodificado (RGB565 + máscara)
static constexpr size_t OBA_SOUNDS_MAX = 512 * 1024;     // PCM
static constexpr size_t OBA_SOUND_COUNT_MAX = 16;
static constexpr uint32_t OBA_SOUND_MAX_MS = 10000;
static constexpr uint32_t OBA_SOUND_RATE = 16000;

// Lê o JSON (sem ler nada do cartão: quadros e sons ficam só com o caminho).
// Campos que faltam ficam com o padrão do motor; os obrigatórios (id, nome,
// paleta, contorno e olhos do rig, size e frames.idle dos sprites) dão erro.
bool obaParse(const char* json, size_t len, ObaSpec& out, String& err);

// Efeito de LEDs no formato de "leds" dos humores. O que falta fica como em f.
bool obaParseLeds(JsonObjectConst o, LedFx& f);

// Ativa o Oba salvo na NVS (do cartão) ou o embutido
void obaBegin();
const ObaSpec& oba();

// Lê o Oba do cartão (ou o embutido, se o id for o dele), sem ativar
ObaSpec* obaLoad(const String& id, String& err);
// Lê e carrega um Oba de uma pasta qualquer do cartão (p.ex. "/obas/.tmp-bit"):
// o JSON, as folhas (PNG decodificado na PSRAM) e os sons (WAV), conferindo o
// sha256 de cada arquivo lido contra files. id: o que o JSON precisa ter.
ObaSpec* obaLoadDir(const String& dir, const String& id, String& err);
// Troca o Oba ativo pelo do cartão e lembra dele na NVS
bool obaActivate(const String& id, String& err);
// Troca o Oba ativo por este (já lido); persist grava o id na NVS
void obaUse(ObaSpec* spec, bool persist);

// Grava um arquivo em /obas/<path> (a pasta do Oba é criada se precisar)
bool obaWriteFile(const String& path, const uint8_t* data, size_t len, String& err);
// Apaga /obas/<id>/ inteira. O ativo não pode ser apagado.
bool obaRemove(const String& id, String& err);
// Apaga um arquivo ou uma pasta inteira do cartão (caminho completo)
bool obaRemoveTree(const String& fullPath);

// id de Oba (e nome de som): 1 a 31 de a-z, 0-9, "-" e "_"
bool obaValidId(const String& id);
// "<id>/<arquivo>": sem "..", só letras, números, ".", "-", "_" e "/"
bool obaValidPath(const String& path);
// Caminho de um arquivo de files, relativo à pasta do Oba: só [A-Za-z0-9._/-],
// sem "..", sem "//", sem começar com "/" ou ".", sem terminar em "/", até 64
// caracteres e até OBA_FILE_DIRS_MAX pastas
bool obaValidFile(const String& rel);

// sha256 em hex minúsculo, pedaço a pedaço (mbedtls)
class ObaSha256 {
 public:
  ObaSha256();
  ~ObaSha256();
  ObaSha256(const ObaSha256&) = delete;
  ObaSha256& operator=(const ObaSha256&) = delete;
  void update(const void* data, size_t len);
  String hex();  // termina; depois disso, só um objeto novo
 private:
  mbedtls_sha256_context ctx;
};
String obaSha256(const void* data, size_t len);

struct ObaInfo {
  String id, name, version;
  bool onCard;  // false: o embutido (não dá para remover)
};
// Obas do cartão (só o cabeçalho de cada JSON); o embutido vem primeiro. Pastas
// que começam com "." (instalação em andamento) ficam de fora.
std::vector<ObaInfo> obaList();
