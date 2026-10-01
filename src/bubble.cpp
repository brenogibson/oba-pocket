#include "bubble.h"
#include <M5Unified.h>
#include "behavior.h"
#include "bubble_fonts.h"
#include "cloud.h"
#include "ext.h"
#include "protocol.h"
#include "rig.h"
#include "ui.h"

// O balão ocupa a direita da tela e o Oba encolhe no canto esquerdo, virado
// para ele. O QR precisa de módulos de ~4 px para o celular ler.
static constexpr int BUBBLE_X = 92, BUBBLE_Y = 8, BUBBLE_W = 222, BUBBLE_H = 224;
static constexpr int BUBBLE_PAD = 12, BUBBLE_R = 18;
static constexpr int QR_SIZE = 168;
static constexpr int IMAGE_SIZE = 100;          // ícone de 80 px ampliado
static constexpr float SIDE_X = 46, SIDE_Y = 168, SIDE_SCALE = 0.5f;
static constexpr uint32_t BUBBLE_DELAY_MS = 260;  // o Oba chega no canto antes do balão abrir
static constexpr uint32_t BUBBLE_POP_MS = 240;
static constexpr uint32_t BUBBLE_CLOSE_MS = 180;
static constexpr uint32_t BUBBLE_GAP_MS = 600;    // respiro entre dois balões seguidos
static constexpr uint32_t BUBBLE_FLIP_MS = 360;   // vira da fala para o QR/ícone
static constexpr uint32_t IMAGE_MS = 9000;
static constexpr uint32_t QR_MS = 15000;
static const char* const QR_CAPTION = "Aponta a câmera aqui!";

enum class Phase : uint8_t { None, Waiting, Opening, Open, Flip, Closing, Gap };

// Pré-desenhado num sprite ao abrir; a cor-chave marca os cantos transparentes
static M5Canvas* spr = nullptr;
static uint16_t key = 0;
static Bubble* bubble = nullptr;
static Phase phase = Phase::None;
static uint32_t phaseAt = 0, pageMs = 0;
static uint8_t page = 0, pages = 1;

static float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

void bubbleBegin(M5Canvas* screen) {
  spr = new M5Canvas(screen);
  spr->setColorDepth(16);
  spr->setPsram(true);
  if (!spr->createSprite(BUBBLE_W, BUBBLE_H)) Serial.println("falha ao criar o buffer do balão");
}

// Escreve o texto centralizado em até maxLines linhas a partir de y (corta com "...")
static int drawLines(M5Canvas& c, const String& text, int y, int maxLines) {
  auto lines = wrapText(c, text, BUBBLE_W - 2 * BUBBLE_PAD);
  if ((int)lines.size() > maxLines) {
    lines.resize(maxLines);
    lines.back() += "...";
  }
  int lh = c.fontHeight() + 2;
  c.setTextDatum(top_center);
  for (const String& l : lines) {
    c.drawString(l, BUBBLE_W / 2, y);
    y += lh;
  }
  return y;
}

// Fonte suavizada do balão (as efont do M5GFX não têm ç, ã, õ)
static void bubbleFont(M5Canvas& c, const uint8_t* vlw) {
  c.unloadFont();
  c.loadFont(vlw);
}

static void renderText(M5Canvas& c, const String& text) {
  // Fonte grande se couber, senão a menor
  const int inner = BUBBLE_H - 2 * BUBBLE_PAD;
  bubbleFont(c, BUBBLE_FONT_BIG);
  if ((int)wrapText(c, text, BUBBLE_W - 2 * BUBBLE_PAD).size() * (c.fontHeight() + 2) > inner) {
    bubbleFont(c, BUBBLE_FONT_SMALL);
  }
  int h = wrapText(c, text, BUBBLE_W - 2 * BUBBLE_PAD).size() * (c.fontHeight() + 2);
  drawLines(c, text, BUBBLE_PAD + max(0, (inner - h) / 2), inner / (c.fontHeight() + 2));
}

// Linhas de legenda que cabem embaixo do ícone
static int imageCaptionLines(M5Canvas& c) {
  bubbleFont(c, BUBBLE_FONT_SMALL);
  return (BUBBLE_H - 2 * BUBBLE_PAD - IMAGE_SIZE - 10) / (c.fontHeight() + 2);
}

// Quantas páginas o balão precisa: QR sempre separa a fala (a legenda embaixo
// dele só tem uma linha); ícone só separa se a fala não couber embaixo dele
static uint8_t pageCount(const Bubble& b) {
  if (!b.text.length() || b.kind == BubbleKind::Text) return 1;
  if (b.kind == BubbleKind::Qr) return 2;
  M5Canvas& c = *spr;
  int n = imageCaptionLines(c);
  return (int)wrapText(c, b.text, BUBBLE_W - 2 * BUBBLE_PAD).size() > n ? 2 : 1;
}

static void render(const Bubble& b, uint8_t pg) {
  const auto& color = oba().look.color;
  // Cor-chave que não aparece no balão
  key = 0xF81F;
  while (key == color.bubble || key == color.ink) --key;

  M5Canvas& c = *spr;
  c.fillScreen(key);  // cantos transparentes
  c.fillRoundRect(0, 0, BUBBLE_W, BUBBLE_H, BUBBLE_R, color.bubble);
  c.setTextColor(color.ink, color.bubble);
  c.setTextSize(1);
  const int inner = BUBBLE_H - 2 * BUBBLE_PAD;
  bool split = pages > 1;

  if (b.kind == BubbleKind::Text || (split && pg == 0)) {
    renderText(c, b.text);
    return;
  }
  if (b.kind == BubbleKind::Image) {
    int maxLines = imageCaptionLines(c);  // carrega a fonte pequena
    int n = split ? 0 : min((int)wrapText(c, b.text, BUBBLE_W - 2 * BUBBLE_PAD).size(), maxLines);
    int lh = c.fontHeight() + 2;
    int gap = n ? 10 : 0;
    int y = BUBBLE_PAD + max(0, (inner - IMAGE_SIZE - gap - n * lh) / 2);
    c.drawPng(b.png, b.pngLen, (BUBBLE_W - IMAGE_SIZE) / 2, y, 0, 0, 0, 0, IMAGE_SIZE / 80.f);
    if (n) drawLines(c, b.text, y + IMAGE_SIZE + gap, n);
    return;
  }
  // QR: a fala já foi na página anterior; aqui só o convite para escanear
  c.qrcode(b.url.c_str(), (BUBBLE_W - QR_SIZE) / 2, BUBBLE_PAD - 2, QR_SIZE);
  bubbleFont(c, BUBBLE_FONT_SMALL);
  drawLines(c, split ? String(QR_CAPTION) : b.text, BUBBLE_PAD + QR_SIZE, 1);
}

static uint32_t textDuration(const String& text) {
  return constrain(3500 + 55 * text.length(), 5000u, 12000u);
}

static uint32_t duration(const Bubble& b, uint8_t pg) {
  if (b.kind == BubbleKind::Text || (pages > 1 && pg == 0)) return textDuration(b.text);
  return b.kind == BubbleKind::Qr ? QR_MS : IMAGE_MS;
}

static void setPhase(Phase p, uint32_t now) {
  phase = p;
  phaseAt = now;
}

void bubbleUpdate(uint32_t now) {
  uint32_t age = now - phaseAt;
  switch (phase) {
    case Phase::None:
      if (extAsking()) break;  // pedido de aprovação primeiro: o balão espera na fila
      bubble = cloudTakeBubble();
      if (!bubble) break;
      cloudSetBubbleBusy(true);
      page = 0;
      pages = pageCount(*bubble);
      render(*bubble, 0);
      pageMs = duration(*bubble, 0);
      Serial.printf("[ui] balão %s: %s\n", bubble->id.c_str(), bubble->text.c_str());
      setPhase(Phase::Waiting, now);
      pet.lastActivity = now;
      petReact(Event::BubbleOpen, now);
      if (pet.facing < 0) petTurnTo(1.f, now);  // vira para o lado do balão
      break;
    case Phase::Waiting:
      if (age > BUBBLE_DELAY_MS) setPhase(Phase::Opening, now);
      break;
    case Phase::Opening:
      if (age > BUBBLE_POP_MS) setPhase(Phase::Open, now);
      break;
    case Phase::Open:
      pet.lastActivity = now;  // não dorme no meio da fala
      if (age > pageMs) setPhase(page + 1 < pages ? Phase::Flip : Phase::Closing, now);
      break;
    case Phase::Flip:
      pet.lastActivity = now;
      // Na metade do giro o balão está "de lado": troca o conteúdo
      if (page == 0 && age > BUBBLE_FLIP_MS / 2) {
        page = 1;
        render(*bubble, 1);
        pageMs = duration(*bubble, 1);
      }
      if (age > BUBBLE_FLIP_MS) setPhase(Phase::Open, now);
      break;
    case Phase::Closing:
      if (age > BUBBLE_CLOSE_MS) {
        protoBubbleDone(bubble->id);
        delete bubble;
        bubble = nullptr;
        setPhase(Phase::Gap, now);
      }
      break;
    case Phase::Gap:
      if (age > BUBBLE_GAP_MS) {
        phase = Phase::None;
        cloudSetBubbleBusy(false);
      }
      break;
  }

  // O Oba desliza entre o centro e o canto (fica lá se já tem outro balão na fila),
  // ou para o lado do pedido de aprovação
  bool side = phase != Phase::None && (phase != Phase::Gap || cloudBubblePending());
  float tx = HOME_X, ty = HOME_Y, ts = 1.f, k = 0.2f;
  if (side) {
    tx = SIDE_X;
    ty = SIDE_Y;
    ts = SIDE_SCALE;
  } else {
    extPlacement(&tx, &ty, &ts);
  }
  pet.x += (tx - pet.x) * k;
  pet.y += (ty - pet.y) * k;
  pet.scale += (ts - pet.scale) * k;
}

void bubbleDraw(M5Canvas& canvas, uint32_t now) {
  const auto& color = oba().look.color;
  float zoom;
  uint32_t age = now - phaseAt;
  switch (phase) {
    case Phase::Opening: {
      // "pop" com um pouquinho de sobra no fim (easeOutBack)
      float p = clampf(age / (float)BUBBLE_POP_MS, 0.f, 1.f) - 1.f;
      zoom = 1.f + 2.2f * p * p * p + 1.2f * p * p;
      break;
    }
    case Phase::Open: zoom = 1.f; break;
    case Phase::Flip: {
      float zx = fabsf(cosf(PI * clampf(age / (float)BUBBLE_FLIP_MS, 0.f, 1.f)));
      if (zx < 0.04f) return;
      spr->pushRotateZoom(&canvas, BUBBLE_X + BUBBLE_W / 2, BUBBLE_Y + BUBBLE_H / 2, 0.f, zx, 1.f, key);
      return;
    }
    case Phase::Closing: zoom = 1.f - 0.8f * clampf(age / (float)BUBBLE_CLOSE_MS, 0.f, 1.f); break;
    default: return;
  }
  int cx = BUBBLE_X + BUBBLE_W / 2, cy = BUBBLE_Y + BUBBLE_H / 2;
  if (zoom > 0.97f && zoom < 1.03f) {
    canvas.fillRoundRect(BUBBLE_X + 4, BUBBLE_Y + 4, BUBBLE_W, BUBBLE_H, BUBBLE_R, color.shadow);
    // Rabinho apontando para a cabeça do Oba
    int tipX, tipY;
    rigPoint(oba().look.bubble.x, oba().look.bubble.y, &tipX, &tipY);
    canvas.fillTriangle(BUBBLE_X + 10, BUBBLE_Y + BUBBLE_H - 96, BUBBLE_X + 10, BUBBLE_Y + BUBBLE_H - 66,
                        tipX, tipY, color.bubble);
    spr->pushSprite(&canvas, BUBBLE_X, BUBBLE_Y, key);
  } else {
    spr->pushRotateZoom(&canvas, cx, cy, 0.f, zoom, zoom, key);
  }
}

bool bubbleTap(int x, int y, uint32_t now) {
  if (phase != Phase::Open || x < BUBBLE_X || y < BUBBLE_Y || y >= BUBBLE_Y + BUBBLE_H) return false;
  // Vai para o QR/ícone, ou fecha antes da hora
  setPhase(page + 1 < pages ? Phase::Flip : Phase::Closing, now);
  return true;
}

bool bubbleShowing() { return phase == Phase::Open || phase == Phase::Opening || phase == Phase::Flip; }

bool bubbleIdle() { return phase == Phase::None; }

void bubbleTestOpen(Bubble* b, uint8_t pg, uint32_t now) {
  bubble = b;
  pages = pageCount(*bubble);
  page = pg < pages ? pg : 0;
  render(*bubble, page);
  setPhase(Phase::Open, now);
  pet.x = SIDE_X;
  pet.y = SIDE_Y;
  pet.scale = SIDE_SCALE;
}

void bubbleTestClose() {
  delete bubble;
  bubble = nullptr;
  phase = Phase::None;
}
