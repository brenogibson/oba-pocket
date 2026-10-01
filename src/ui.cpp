#include "ui.h"
#include <mbedtls/base64.h>
#include "bubble_fonts.h"
#include "screen.h"
#include "sound.h"

static constexpr int BTN_X = 36, BTN_Y = 150, BTN_W = 248, BTN_H = 46;
static constexpr uint32_t HOLD_MS = 3000;
static constexpr uint32_t ASK_TIMEOUT_MS = 30000;
// uiConfirm: dois botões lado a lado embaixo; o texto para antes deles
static constexpr int PAIR_Y = 172, PAIR_H = 44, PAIR_W = 136, PAIR_GAP = 16, PAIR_TOP = 20;

std::vector<String> wrapText(M5Canvas& c, const String& text, int maxW) {
  std::vector<String> lines;
  String line;
  int start = 0;
  while (start < (int)text.length()) {
    int end = text.indexOf(' ', start);
    if (end < 0) end = text.length();
    String word = text.substring(start, end);
    start = end + 1;
    if (!word.length()) continue;
    String next = line.length() ? line + " " + word : word;
    if (line.length() && c.textWidth(next) > maxW) {
      lines.push_back(line);
      line = word;
    } else {
      line = next;
    }
  }
  if (line.length()) lines.push_back(line);
  return lines;
}

// Texto centralizado a partir de y ('\n' separa parágrafos); devolve onde a
// próxima linha começaria
static int drawCentered(M5Canvas& c, const char* text, int y, int maxW) {
  int lh = c.fontHeight() + 2;
  c.setTextDatum(top_center);
  String all = text;
  for (int start = 0;;) {
    int end = all.indexOf('\n', start);
    for (const String& l : wrapText(c, all.substring(start, end < 0 ? all.length() : end), maxW)) {
      c.drawString(l, c.width() / 2, y);
      y += lh;
    }
    if (end < 0) break;
    start = end + 1;
  }
  return y;
}

// Título e texto; deixa a fonte pequena carregada. bottom > 0: o texto não
// passa dessa linha.
static void drawPage(M5Canvas& c, const char* title, const char* body, int top = 28, int bottom = 0) {
  c.fillScreen(C_UI_BG);
  c.loadFont(BUBBLE_FONT_BIG);
  c.setTextColor(C_UI_TEXT);
  int y = drawCentered(c, title, top, c.width() - 32);
  c.loadFont(BUBBLE_FONT_SMALL);
  c.setTextColor(C_UI_DIM);
  if (bottom > 0) c.setClipRect(0, 0, c.width(), bottom);
  drawCentered(c, body, y + 10, c.width() - 48);
  if (bottom > 0) c.clearClipRect();
}

void uiNotice(M5Canvas& c, const char* title, const char* body) {
  drawPage(c, title, body);
  c.unloadFont();
  c.pushSprite(0, 0);
}

bool uiConfirmHold(M5Canvas& c, const char* title, const char* body, const char* action) {
  drawPage(c, title, body);
  c.setTextColor(C_UI_DIM);
  c.setTextDatum(top_center);
  c.drawString("Toque fora para pular", c.width() / 2, BTN_Y + BTN_H + 14);
  c.loadFont(BUBBLE_FONT_BIG);

  uint32_t t0 = millis(), holdAt = 0;
  bool yes = false;
  for (;;) {
    M5.update();
    uint32_t now = millis();
    const auto& td = M5.Touch.getDetail();
    bool onBtn = td.x >= BTN_X && td.x < BTN_X + BTN_W && td.y >= BTN_Y - 12 && td.y < BTN_Y + BTN_H + 12;
    if (!td.isPressed() || !onBtn) holdAt = 0;
    else if (!holdAt) holdAt = now;
    float p = holdAt ? (now - holdAt) / (float)HOLD_MS : 0.f;
    if (p >= 1.f) { yes = true; break; }
    // Fora de 0..240 ficam os botões virtuais do Core2: não contam como "pular"
    bool tapOut = td.wasClicked() && !onBtn && td.y >= 0 && td.y < c.height();
    if (tapOut || now - t0 > ASK_TIMEOUT_MS) break;

    // O botão enche da esquerda para a direita enquanto o dedo segura
    c.fillRect(BTN_X - 2, BTN_Y - 2, BTN_W + 4, BTN_H + 4, C_UI_BG);
    c.fillRoundRect(BTN_X, BTN_Y, BTN_W, BTN_H, BTN_H / 2, C_UI_BTN);
    if (p > 0.f) {
      c.setClipRect(BTN_X, BTN_Y, (int)(BTN_W * p), BTN_H);
      c.fillRoundRect(BTN_X, BTN_Y, BTN_W, BTN_H, BTN_H / 2, C_UI_RED);
      c.clearClipRect();
    }
    c.drawRoundRect(BTN_X, BTN_Y, BTN_W, BTN_H, BTN_H / 2, C_UI_RED);
    c.setTextColor(C_UI_TEXT);
    c.setTextDatum(middle_center);
    c.drawString(action, c.width() / 2, BTN_Y + BTN_H / 2);
    c.pushSprite(0, 0);
    soundUpdate(now);
    delay(10);
  }
  c.unloadFont();
  return yes;
}

// 0: nenhum; 1: o da esquerda (não); 2: o da direita (sim). Com folga em volta.
static int pairHit(M5Canvas& c, int x, int y) {
  if (y < PAIR_Y - 12 || y >= c.height()) return 0;
  int left = (c.width() - 2 * PAIR_W - PAIR_GAP) / 2;
  if (x >= left - 12 && x < left + PAIR_W + PAIR_GAP / 2) return 1;
  if (x >= left + PAIR_W + PAIR_GAP / 2 && x < left + 2 * PAIR_W + PAIR_GAP + 12) return 2;
  return 0;
}

static void drawPairButton(M5Canvas& c, int x, const char* label, uint16_t color, bool pressed) {
  c.fillRoundRect(x, PAIR_Y, PAIR_W, PAIR_H, PAIR_H / 2, pressed ? color : C_UI_BTN);
  c.drawRoundRect(x, PAIR_Y, PAIR_W, PAIR_H, PAIR_H / 2, color);
  c.setTextColor(pressed ? C_UI_BG : C_UI_TEXT);
  c.setTextDatum(middle_center);
  c.drawString(label, x + PAIR_W / 2, PAIR_Y + PAIR_H / 2);
}

bool uiConfirm(M5Canvas& c, const char* title, const char* body, const char* yes, const char* no,
               uint32_t timeoutMs) {
  uint32_t t0 = millis();
  int down = 0, shown = -1;   // botão onde o dedo desceu; o que está desenhado apertado
  uint32_t shownSecs = 0;
  bool armed = false;         // o dedo que já estava na tela não conta
  int answer = -1;
  while (answer < 0) {
    M5.update();
    uint32_t now = millis();
    if (now - t0 >= timeoutMs) break;
    const auto& td = M5.Touch.getDetail();
    if (!armed) {
      armed = !td.isPressed();
    } else {
      if (td.wasPressed()) down = pairHit(c, td.x, td.y);
      if (td.wasReleased()) {
        if (down && pairHit(c, td.x, td.y) == down) answer = down == 2;
        down = 0;
      }
    }
    while (Serial.available()) {
      int k = Serial.read();
      if (k == 'Y' || k == 'N') answer = k == 'Y';
      if (k == 'S') uiDumpScreen(c, "confirm");
    }
    if (answer >= 0) break;

    // Só redesenha quando o botão apertado ou os segundos que faltam mudam
    int pressed = td.isPressed() && down && pairHit(c, td.x, td.y) == down ? down : 0;
    uint32_t secs = (timeoutMs - (now - t0) + 999) / 1000;
    if (pressed != shown || secs != shownSecs) {
      shown = pressed;
      shownSecs = secs;
      drawPage(c, title, body, PAIR_TOP, PAIR_Y - 6);
      c.setTextColor(C_UI_DIM);
      c.setTextDatum(top_center);
      String hint = "Sem resposta em " + String((unsigned)secs) + " s: " + no;
      c.drawString(hint, c.width() / 2, PAIR_Y + PAIR_H + 4);
      c.loadFont(BUBBLE_FONT_BIG);
      int left = (c.width() - 2 * PAIR_W - PAIR_GAP) / 2;
      drawPairButton(c, left, no, C_UI_DIM, pressed == 1);
      drawPairButton(c, left + PAIR_W + PAIR_GAP, yes, C_UI_GREEN, pressed == 2);
      c.unloadFont();
      c.pushSprite(0, 0);
    }
    soundUpdate(now);
    delay(10);
  }
  return answer == 1;
}

// Cada linha sai numa escrita só, para o log da task de rede não se misturar no meio
void uiDumpScreen(M5Canvas& c, const char* name) {
  const uint8_t* buf = (const uint8_t*)c.getBuffer();
  const int w = c.width(), h = c.height();
  static uint8_t line[SCREEN_W * 2 * 4 / 3 + 16];
  Serial.printf("\n@@BEGIN %d %d %s\n", w, h, name);
  for (int y = 0; y < h; ++y) {
    int head = snprintf((char*)line, sizeof line, "@@%d:", y);
    size_t n = 0;
    mbedtls_base64_encode(line + head, sizeof line - head - 1, &n, buf + y * w * 2, w * 2);
    line[head + n] = '\n';
    Serial.write(line, head + n + 1);
  }
  Serial.println("@@END");
}
