// Oba Pocket — um bichinho (o Oba) que mora no M5Stack Core2 for AWS
//
// O Oba ativo vem de um JSON (oba.h): o corpo (rig ou quadros PNG), como cada
// humor se mexe, os reflexos e os sons. Sem nuvem, ele reage só com o hardware
// da placa:
//   - microfone: fala por perto faz ele olhar em volta; barulho alto assusta
//     e ele cobre o rosto até ficar quieto
//   - toque: segurar o dedo na tela faz os olhos seguirem o dedo; um toque
//     rápido deixa ele feliz (e acalma quando está escondido)
//   - IMU: inclinar a placa puxa o olhar; chacoalhar deixa ele tonto; um teco
//     de lado faz ele virar
//   - sem interação por um tempo, ele dorme
//   - barra de LEDs laterais acompanha o humor
//   - sons curtos nos reflexos e no comando play, só com o REC desligado (o
//     alto-falante e o mic dividem o I2S; com o REC ligado ele fica em silêncio)
//   - botão do meio: tela de escolha de Obas (só com o REC desligado)
//   - transcrição ao vivo: o áudio vai direto para o Amazon Transcribe e,
//     quando alguém fala o nome do Oba, ele comemora e o portal (portal/web)
//     mostra um card. A bolinha vermelha indica gravação.
//   - agente (Bedrock AgentCore): acompanha a conversa e manda balões de fala
//     com dicas, logos de serviços e QR codes. O Oba encolhe para o canto e
//     "fala" o balão; cartas guardadas na manga disparam quando alguém volta
//     ao assunto.
//   - fontes externas (ext.h), como a ponte do Claude Code: um rótulo no alto
//     mostra o que estão fazendo e os pedidos de aprovação abrem num cartão com
//     Negar e Aprovar por toque (o botão do meio devolve para o terminal)
// O harness fala com a placa pelo Oba Protocol v1 (docs/protocol.md, protocol.h).
// Obas novos chegam pelo ar (oba.install, install.h): a placa grava no cartão,
// confere e pergunta na tela antes de instalar.
//
// Pela serial: 'S' tira print, 'R' liga o REC, 'T<frase>' simula fala, 'P'/'H'
// desenham um quadro fixo para comparar prints (ver freezeShot), 'U' grava um
// arquivo de Oba no cartão, 'A<id>' ativa um Oba, 'L' lista os instalados, 'O'
// abre a tela de escolha (e fecha, se já estiver nela), 'E<evento>' finge um
// evento da placa (touch.tap, button.a...), 'C' tenta o cartão de novo, 'F'
// mostra o tempo de quadro desde o último 'F', 'X<fonte> <json>' finge uma
// mensagem em ext/<fonte> e 'Y'/'N' respondem à pergunta da instalação (ou ao
// pedido na tela).

#include <M5Unified.h>
#include <math.h>
#include <esp_rom_crc.h>
#include "audio.h"
#include "behavior.h"
#include "bubble.h"
#include "cloud.h"
#include "ext.h"
#include "install.h"
#include "leds.h"
#include "oba.h"
#include "picker.h"
#include "protocol.h"
#include "rig.h"
#include "screen.h"
#include "sdcard.h"
#include "sound.h"
#include "ui.h"
#include "vibration.h"

// Brilho da tela (0-255)
static constexpr uint8_t SCREEN_BRIGHTNESS = 128;  // 50%

// Maior arquivo que a serial aceita de uma vez (vai inteiro para a PSRAM)
static constexpr size_t UPLOAD_MAX = 2 * 1024 * 1024;
static constexpr size_t UPLOAD_CHUNK = 1024;
static constexpr uint32_t UPLOAD_TIMEOUT_MS = 3000;

static M5Canvas canvas(&M5.Display);

// Tempo de quadro para o 'F': do começo de um loop ao do próximo. Os que
// passaram por uma tela que segura o loop (escolha, pergunta, serial) ficam de fora.
static uint32_t frameAt = 0, frameCount = 0, frameMax = 0;
static uint64_t frameSum = 0;
static bool frameSkip = true;

// ---------------------------------------------------------------- input

// Botão de gravação no canto superior esquerdo (área de toque com folga; menor
// com o cartão de pedido na tela, que começa logo ao lado)
static constexpr int REC_BTN_X = 6, REC_BTN_Y = 6, REC_BTN_W = 78, REC_BTN_H = 22;
static bool inRecButton(int x, int y) {
  int m = extShowing() ? 4 : 16;
  return x >= 0 && y >= 0 && x < REC_BTN_X + REC_BTN_W + m && y < REC_BTN_Y + REC_BTN_H + m;
}

static void handleTouch(uint32_t now) {
  const auto& td = M5.Touch.getDetail();
  if (td.wasClicked() && inRecButton(td.x, td.y)) {
    cloudSetRecording(!cloudRecording());
    return;
  }
  if (extTouch(td, now)) {  // o pedido na tela fica com o toque
    petTouch(false, false, td.x, td.y, now);
    return;
  }
  if (td.wasClicked() && bubbleTap(td.x, td.y, now)) return;
  // Fora de 0..240 ficam os botões virtuais do Core2 (com a tela girada,
  // o toque neles chega com y negativo)
  bool onScreen = td.y >= 0 && td.y < SCREEN_H && !inRecButton(td.x, td.y);
  petTouch(td.isPressed() && onScreen, td.wasClicked() && onScreen, td.x, td.y, now);
}

// ---------------------------------------------------------------- drawing

// Botão de gravação: desligado (padrão) não sai áudio nenhum da placa.
// Ligado, a bolinha fica vermelha pulsando (amarela enquanto conecta), para
// deixar claro para quem está na sala.
static void drawCloudStatus(uint32_t now) {
  const auto& color = oba().look.color;
  CloudState st = cloudState();
  bool on = cloudRecording();
  bool rec = on && st == CloudState::Streaming;
  uint16_t dot = !on ? color.off : rec ? color.rec : color.wait;
  const char* label = !on ? "MIC OFF" : rec ? "REC" : st == CloudState::Offline ? "WiFi..." : "...";

  canvas.fillRoundRect(REC_BTN_X, REC_BTN_Y, REC_BTN_W, REC_BTN_H, REC_BTN_H / 2, color.shadow);
  int cx = REC_BTN_X + 12, cy = REC_BTN_Y + REC_BTN_H / 2;
  if (on) {
    float pulse = rec ? 0.5f + 0.5f * sinf(now / 250.f) : 0.f;
    canvas.fillCircle(cx, cy, 5 + (int)(pulse * 2), dot);
  } else {
    canvas.drawCircle(cx, cy, 5, dot);
  }
  canvas.setTextColor(on ? color.text : color.off);
  canvas.setTextDatum(middle_left);
  canvas.setTextSize(1);
  canvas.drawString(label, cx + 11, cy + 1);
}

static void render(uint32_t now, float t) {
  rigDraw(canvas, now, t);
  bubbleDraw(canvas, now);
  extDraw(canvas, now);
  drawCloudStatus(now);
  installDrawStatus(canvas);  // só durante uma instalação
  canvas.pushSprite(0, 0);
}

// ---------------------------------------------------------------- screenshot

// 'P<nome> <humor> <ms> <olharX> <olharY> <lado> [extra]' na serial: desenha um
// quadro com o estado fixo (sem sorteio, sensores nem piscada) e manda o CRC32
// dele e o print. 'H' faz o mesmo sem o print (só o CRC, bem mais rápido).
// extra: '-', 'touch' (bochechas do toque), 'turn' (no meio da virada), 'closed'
// (humor antigo: sonolento de olho fechado), 'text' e 'long' (balão de fala curta
// e longa), 'qr0' e 'qr1' (balão com QR, páginas 0 e 1). Serve para conferir que
// mudanças no motor não mexem em nenhum pixel.
static void freezeShot(const String& args, bool dump) {
  char name[32] = "", moodArg[12] = "", extra[12] = "-";
  unsigned tms = 0;
  float lx = 0, ly = 0, face = 1;
  if (sscanf(args.c_str(), "%31s %11s %u %f %f %f %11s", name, moodArg, &tms, &lx, &ly, &face, extra) < 6) {
    Serial.println("[teste] uso: P<nome> <humor> <ms> <olharX> <olharY> <lado> [extra]");
    return;
  }
  Mood m;
  bool longText = !strcmp(extra, "long");
  bool withBubble = !strcmp(extra, "text") || longText || !strncmp(extra, "qr", 2);
  if (!moodByName(moodArg, &m) || (withBubble && !bubbleIdle())) {
    Serial.printf("[teste] humor desconhecido ou balão na tela: %s\n", moodArg);
    return;
  }
  petEnterMood(m, tms - (strcmp(extra, "closed") ? 1000 : 3000));  // sonolento: 1/3 ou todo fechado
  pet.lookX = lx;
  pet.lookY = ly;
  pet.facing = face;
  pet.turnStart = !strcmp(extra, "turn") ? tms - TURN_MS * 3 / 10 : 0;
  pet.blinkStart = 0;
  pet.touching = !strcmp(extra, "touch");
  pet.x = HOME_X;
  pet.y = HOME_Y;
  pet.scale = 1.f;
  if (withBubble) {
    Bubble* b = new Bubble();
    b->kind = extra[0] == 'q' ? BubbleKind::Qr : BubbleKind::Text;
    b->text = "Vocês falaram de filas: o Amazon SQS desacopla os serviços e segura os picos sem perder "
              "nenhuma mensagem.";
    if (longText) {
      b->text += " Dá para começar com uma fila padrão, ligar uma DLQ para as mensagens que falham e "
                 "escalar os consumidores pela profundidade da fila, sem mexer no produtor.";
    }
    b->url = "https://aws.amazon.com/sqs/";
    bubbleTestOpen(b, extra[2] == '1' ? 1 : 0, tms);
  }
  randomSeed(1);  // o tremido do susto usa random()
  render(tms, tms / 1000.f);
  Serial.printf("@@HASH %s %08x\n", name,
                esp_rom_crc32_le(0, (const uint8_t*)canvas.getBuffer(), SCREEN_W * SCREEN_H * 2));
  if (dump) uiDumpScreen(canvas, name);
  randomSeed(esp_random());
  pet.touching = false;
  pet.turnStart = 0;
  if (withBubble) bubbleTestClose();
}

// ---------------------------------------------------------------- obas

// Oba novo (ou o mesmo de novo): palavras de ativação dele e um "oi!"
static void applyOba() {
  cloudSetOba(oba().id, oba().wakeWords);
  protoObasChanged();
  petBegin(millis());
}

// 'U<id>/<arquivo> <tamanho>' na serial: grava um arquivo de Oba no cartão
// (tools/monitor.py, comando u). Ele chega em pedaços de até 1 KB e cada
// "@@GO" pede o próximo, para o buffer da serial não transbordar. No fim,
// "@@OK" ou "@@ERR <motivo>".
static void receiveFile(const String& args) {
  vibrationStop();  // o upload segura o loop
  int sp = args.lastIndexOf(' ');
  String path = sp > 0 ? args.substring(0, sp) : "";
  long size = sp > 0 ? args.substring(sp + 1).toInt() : 0;
  if (!path.length() || size <= 0 || (size_t)size > UPLOAD_MAX) {
    Serial.println("@@ERR uso: U<id>/<arquivo> <tamanho> (até 2 MB)");
    return;
  }
  uint8_t* buf = (uint8_t*)ps_malloc(size);
  if (!buf) {
    Serial.println("@@ERR sem memória");
    return;
  }
  size_t got = 0;
  while (got < (size_t)size) {
    Serial.println("@@GO");
    size_t want = min(UPLOAD_CHUNK, (size_t)size - got), n = 0;
    uint32_t last = millis();
    while (n < want && millis() - last < UPLOAD_TIMEOUT_MS) {
      int avail = Serial.available();
      if (avail <= 0) {
        delay(1);
        continue;
      }
      n += Serial.readBytes(buf + got + n, min((size_t)avail, want - n));
      last = millis();
    }
    if (n < want) {
      free(buf);
      Serial.printf("@@ERR parou em %u de %ld bytes\n", (unsigned)(got + n), size);
      return;
    }
    got += n;
  }
  String err;
  bool ok = obaWriteFile(path, buf, size, err);
  free(buf);
  if (ok) {
    protoObasChanged();
    Serial.printf("@@OK %s (%ld bytes)\n", path.c_str(), size);
  } else {
    Serial.printf("@@ERR %s\n", err.c_str());
  }
}

// Botão do meio: tela de escolha. Com o REC ligado ela não abre, para a troca
// não cortar a transcrição no meio da reunião.
static void openPicker() {
  vibrationStop();  // a tela de escolha segura o loop, que é quem anda com a vibração
  if (cloudRecording()) {
    uiNotice(canvas, "Desligue o REC antes", "A troca de Oba só abre com o microfone desligado.");
    delay(1800);
    return;
  }
  pickerRun(canvas);
  applyOba();  // o Oba volta do canto da prévia para o centro (e a lista pode ter mudado)
}

static void activate(String id) {
  id.trim();
  String err;
  if (!obaActivate(id, err)) {
    Serial.printf("@@ERR %s: %s\n", id.c_str(), err.c_str());
    return;
  }
  applyOba();
  Serial.printf("@@OK ativo: %s\n", id.c_str());
}

static void frameStats() {
  if (!frameCount) {
    Serial.println("[quadro] nenhum quadro medido ainda");
  } else {
    Serial.printf("[quadro] %u quadros, média %.1f ms, máximo %.1f ms\n", (unsigned)frameCount,
                  frameSum / 1000.0 / frameCount, frameMax / 1000.0);
  }
  frameCount = frameMax = 0;
  frameSum = 0;
}

static void listObas() {
  for (const ObaInfo& o : obaList()) {
    Serial.printf("[oba] %s%s \"%s\" %s\n", o.id == oba().id ? "* " : "  ", o.id.c_str(), o.name.c_str(),
                  o.version.c_str());
  }
}

// ---------------------------------------------------------------- main

// O cartão SD guarda os Obas. Sem FAT32 a placa pergunta antes de formatar,
// porque formatar apaga tudo o que tem nele.
static void setupCard() {
  vibrationStop();  // a pergunta segura o loop
  if (sdBegin() != SdState::Unformatted) return;
  if (!uiConfirmHold(canvas, "Formatar o cartão?",
                     "Ele não está em FAT32, e os Obas ficam nele. Formatar apaga tudo o que tem no cartão.",
                     "Segure para formatar")) {
    return;
  }
  uiNotice(canvas, "Formatando o cartão…", "Leva alguns segundos. Não tire o cartão.");
  bool ok = sdFormat();
  uiNotice(canvas, ok ? "Cartão pronto!" : "Não deu para formatar",
           ok ? "Formatado em FAT32. Os Obas instalados vão ficar nele."
              : "Tente formatar no computador, em FAT32. A placa segue com o Oba embutido.");
  delay(2500);
}

void setup() {
  // Antes do M5.begin (que abre a serial): o upload manda 1 KB de uma vez
  Serial.setRxBufferSize(4096);
  auto cfg = M5.config();
  cfg.output_power = true;       // barramento 5V que alimenta a barra de LEDs
  cfg.internal_spk = true;       // o alto-falante só liga para tocar um som (sound.h)
  cfg.serial_baudrate = 921600;  // rápida para os prints da tela (tools/monitor.py)
  M5.begin(cfg);
  M5.Display.setRotation(SCREEN_ROTATION);
  M5.Display.setBrightness(SCREEN_BRIGHTNESS);

  canvas.setColorDepth(16);
  canvas.setPsram(true);
  if (!canvas.createSprite(SCREEN_W, SCREEN_H)) {
    Serial.println("falha ao criar o buffer da tela");
  }
  bubbleBegin(&canvas);
  extBegin(&canvas);
  setupCard();
  installRecover();  // sobras de uma instalação que não terminou
  obaBegin();

  ledsBegin();
  if (!audioBegin()) Serial.println("falha ao iniciar o microfone");
  cloudBegin();
  randomSeed(esp_random());
  protoBegin();
  applyOba();
}

void loop() {
  uint32_t us = micros();
  if (!frameSkip && frameAt) {
    uint32_t d = us - frameAt;
    ++frameCount;
    frameSum += d;
    if (d > frameMax) frameMax = d;
  }
  frameAt = us;
  frameSkip = false;

  M5.update();
  uint32_t now = millis();
  float t = now / 1000.f;

  petSense(now);
  handleTouch(now);
  if (M5.BtnA.wasClicked() && !extShowing()) protoButton('a', now);
  if (M5.BtnB.wasClicked() && !extSkip(now) && bubbleIdle() && !extAsking()) {
    openPicker();
    frameSkip = true;
  }
  if (M5.BtnC.wasClicked() && !extShowing()) protoButton('c', now);
  if (protoUpdate(now)) applyOba();  // o harness trocou o Oba
  if (installUpdate(canvas, now)) applyOba();  // instalou o ativo (ou pediram para ativar)
  if (installBlocked()) frameSkip = true;
  soundUpdate(now);
  if (cloudTakeKeywordHits()) {  // ouviu o nome do Oba
    pet.lastActivity = now;
    petReact(Event::Wake, now);
  }
  extUpdate(now);  // antes do balão: pedido na fila segura o próximo balão
  bubbleUpdate(now);
  petUpdate(now, bubbleShowing() || extShowing());
  render(now, t);
  // Serial para testes: 'S' tira print, 'R' liga/desliga a gravação,
  // 'T<frase>' finge que alguém falou a frase (dispara cartas da manga)
  if (Serial.available()) {
    int c = Serial.read();
    frameSkip = true;  // os comandos de teste podem segurar o loop (print, upload...)
    if (c == 'S') uiDumpScreen(canvas);
    if (c == 'C') {  // tenta o cartão de novo, sem reiniciar
      setupCard();
      installRecover();
      if (oba().builtin) obaBegin();
      applyOba();
    }
    if (c == 'F') frameStats();
    if (c == 'R') cloudSetRecording(!cloudRecording());
    if (c == 'T') cloudSimulateLine(Serial.readStringUntil('\n'));
    if (c == 'P' || c == 'H') freezeShot(Serial.readStringUntil('\n'), c == 'P');
    if (c == 'U') receiveFile(Serial.readStringUntil('\n'));
    if (c == 'A') activate(Serial.readStringUntil('\n'));
    if (c == 'L') listObas();
    if (c == 'O' && bubbleIdle() && !extAsking()) openPicker();  // como o botão do meio
    if (c == 'X') {
      String line = Serial.readStringUntil('\n');
      int sp = line.indexOf(' ');
      if (sp > 0) extInject(line.substring(0, sp), line.substring(sp + 1), now);
    }
    if (c == 'Y' || c == 'N') extAnswer(c == 'Y', now);
    if (c == 'E') {
      String type = Serial.readStringUntil('\n');
      type.trim();
      protoSimulate(type, now);
    }
  }
  ledsShow(protoLeds(oba().mood(pet.mood).leds, now), now, t);

  static uint32_t lastLog = 0;
  if (now - lastLog > 500) {
    lastLog = now;
    petLog();
  }
}
