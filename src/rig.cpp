#include "rig.h"
#include <math.h>
#include "behavior.h"
#include "oba.h"
#include "sprite.h"

static M5Canvas* cv = nullptr;

// Pose do Oba no quadro atual (centro + escala), usada pelo corpo e rosto
static float poseX = HOME_X, poseY = HOME_Y, poseSX = 1.f, poseSY = 1.f;

// Converte um ponto do Oba para a tela
static int px(float x) { return (int)lroundf(poseX + x * poseSX); }
static int py(float y) { return (int)lroundf(poseY + y * poseSY); }
// Tamanho fixo (raio, espessura) na escala atual do Oba
static int sc(float v) { return max(1, (int)lroundf(v * pet.scale)); }

void rigPoint(float x, float y, int* sx, int* sy) {
  *sx = px(x);
  *sy = py(y);
}

static float channel(const Motion& m, float t) {
  float v;
  switch (m.kind) {
    case Motion::Sin:    v = sinf(t * m.a) * m.b; break;
    case Motion::Bounce: v = -fabsf(sinf(t * m.a)) * m.b; break;
    case Motion::Jitter: return random((long)m.a, (long)m.b + 1);
    default:             return m.a;
  }
  if (m.c != 0.f) v += m.c;
  return v;
}

// Preenche o contorno por scanline (o polígono pode ser côncavo, então não dá
// para usar triângulos em leque). A parte de baixo balança com o tempo.
static void drawBody(const Look& L, float t, float wave) {
  static float xs[OBA_OUTLINE_MAX], ys[OBA_OUTLINE_MAX];
  const int n = L.outline.size();
  float minY = 1e9f, maxY = -1e9f;
  for (int i = 0; i < n; ++i) {
    float x = L.outline[i].x, y = L.outline[i].y;
    if (y > L.wave.from) x += sinf(t * L.wave.speed - y * L.wave.phase) * wave * ((y - L.wave.from) / L.wave.span);
    xs[i] = poseX + x * poseSX;
    ys[i] = poseY + y * poseSY;
    minY = fminf(minY, ys[i]);
    maxY = fmaxf(maxY, ys[i]);
  }
  int y0 = max(0, (int)ceilf(minY)), y1 = min(cv->height() - 1, (int)floorf(maxY));
  float hits[16];
  for (int y = y0; y <= y1; ++y) {
    float sy = y + 0.5f;
    int k = 0;
    for (int i = 0, j = n - 1; i < n; j = i++) {
      if ((ys[i] <= sy) != (ys[j] <= sy) && k < 16) {
        hits[k++] = xs[j] + (sy - ys[j]) * (xs[i] - xs[j]) / (ys[i] - ys[j]);
      }
    }
    for (int a = 1; a < k; ++a) {  // insertion sort, k é pequeno
      float v = hits[a];
      int b = a - 1;
      while (b >= 0 && hits[b] > v) { hits[b + 1] = hits[b]; --b; }
      hits[b + 1] = v;
    }
    for (int a = 0; a + 1 < k; a += 2) {
      int xa = (int)lroundf(hits[a]), xb = (int)lroundf(hits[a + 1]);
      if (xb > xa) cv->drawFastHLine(xa, y, xb - xa, L.color.body);
    }
  }
}

static void drawEye(const Look& L, float x, float y, float open) {
  int cx = px(x), cy = py(y);
  if (open < 0.15f) {
    cv->fillRoundRect(cx - sc(L.eyes.rx), cy - sc(2), sc(L.eyes.rx) * 2, sc(4), sc(2), L.color.eye);
  } else {
    cv->fillEllipse(cx, cy, sc(L.eyes.rx), sc(L.eyes.ry * open), L.color.eye);
  }
}

static void drawHappyEye(const Look& L, float x, float y) {
  cv->fillArc(px(x), py(y) + sc(6), sc(7), sc(11), 200, 340, L.color.eye);
}

static void drawSpiralEye(const Look& L, float x, float y, float t) {
  float rot = t * 540.f;
  for (int i = 0; i < 3; ++i) {
    float a0 = rot + i * 70.f;
    cv->fillArc(px(x), py(y), sc(2 + i * 4), sc(4 + i * 4), a0, a0 + 250.f, L.color.eye);
  }
}

// Olhos arregalados, com brilho e boquinha de "oh!"
static void drawScaredFace(const Look& L, float y) {
  float l = L.eyes.left, r = L.eyes.right;
  cv->fillCircle(px(l - 2), py(y), sc(11), L.color.eye);
  cv->fillCircle(px(r + 2), py(y), sc(11), L.color.eye);
  cv->fillCircle(px(l + 1), py(y - 4), sc(3), L.color.body);
  cv->fillCircle(px(r + 5), py(y - 4), sc(3), L.color.body);
  cv->fillEllipse(px((l + r) * 0.5f), py(y + 26), sc(6), sc(8), L.color.eye);
}

// Bracinho em arco centrado em (ax, ay), da base até a ponta (graus, 0 =
// direita, 270 = cima). Da cor do corpo, com contorno só nas laterais e na
// ponta arredondada. A base fica "aberta" e se funde ao corpo, desenhando um
// "C" em vez de uma forma fechada.
static void drawArm(const Look& L, float ax, float ay, int r, float base, float tip) {
  const int ARM_W = sc(30);
  const int LINE = sc(3);
  constexpr float BASE_EXT = 12.f;  // quanto o preenchimento passa da base (graus)
  int cx = px(ax), cy = py(ay);
  r = sc(r);
  if (poseSX < 0) {  // espelhado: ângulos refletem em torno do eixo vertical
    base = 180.f - base;
    tip = 180.f - tip;
    if (fminf(base, tip) < 0) { base += 360.f; tip += 360.f; }
  }
  int r0 = r - ARM_W / 2, r1 = r0 + ARM_W;
  float rm = (r0 + r1) * 0.5f;
  int tx = cx + (int)lroundf(cosf(tip * DEG_TO_RAD) * rm);
  int ty = cy + (int)lroundf(sinf(tip * DEG_TO_RAD) * rm);
  float lo = fminf(base, tip), hi = fmaxf(base, tip);
  cv->fillArc(cx, cy, r0 - LINE, r1 + LINE, lo, hi, L.color.outline);
  cv->fillCircle(tx, ty, ARM_W / 2 + LINE, L.color.outline);
  // O preenchimento se estende além da base e apaga o contorno ali
  if (tip > base) lo -= BASE_EXT; else hi += BASE_EXT;
  cv->fillArc(cx, cy, r0, r1, lo, hi, L.color.body);
  cv->fillCircle(tx, ty, ARM_W / 2, L.color.body);
}

// Braços cobrindo os olhos, cada um preso a um olho
static void drawArms(const Look& L, float y) {
  const Look::Arm& a = L.arms[0];
  const Look::Arm& b = L.arms[1];
  drawArm(L, L.eyes.left + a.dx, y + a.dy, a.r, a.from, a.to);
  drawArm(L, L.eyes.right + b.dx, y + b.dy, b.r, b.from, b.to);
}

static void drawCheeks(const Look& L, float y) {
  const auto& c = L.cheeks;
  cv->fillEllipse(px(L.eyes.left - c.dx), py(y + c.dy), sc(c.rx), sc(c.ry), L.color.blush);
  cv->fillEllipse(px(L.eyes.right + c.dx), py(y + c.dy), sc(c.rx), sc(c.ry), L.color.blush);
}

// Estrelinhas girando em volta da cabeça
static void drawStars(const Look& L, float t) {
  const auto& s = L.stars;
  for (int i = 0; i < 3; ++i) {
    float a = t * 4.f + i * 2.094f;
    cv->fillCircle(px(s.x + cosf(a) * s.rx), py(s.y + sinf(a) * s.ry), sc(5), L.color.star);
  }
}

// "z" subindo e crescendo
static void drawZzz(const Look& L, float t) {
  const auto& z = L.zzz;
  cv->setTextColor(L.color.text);
  cv->setTextDatum(middle_center);
  for (int i = 0; i < 3; ++i) {
    float p = fmodf(t * 0.45f + i / 3.f, 1.f);
    cv->setTextSize((1.5f + p * 2.5f) * pet.scale);
    cv->drawString("z", px(z.x + p * z.dx), py(z.y + p * z.dy));
  }
}

// Rosto: olhos, bochechas e braços do humor, nesta ordem
static void drawFace(const Look& L, const MoodSpec& m, uint32_t now, float t, float ex, float ey) {
  float fy = L.eyes.y + m.dy;
  float lx = L.eyes.left, rx = L.eyes.right;
  switch (m.eyes) {
    case Eyes::Open: {
      float open = petEyeOpen(now);
      drawEye(L, lx + ex, fy + ey, open);
      drawEye(L, rx + ex, fy + ey, open);
      break;
    }
    case Eyes::Closing: {
      float open = petEyeOpen(now);
      drawEye(L, lx, fy, open);
      drawEye(L, rx, fy, open);
      break;
    }
    case Eyes::Happy:
      drawHappyEye(L, lx, fy);
      drawHappyEye(L, rx, fy);
      break;
    case Eyes::Scared: drawScaredFace(L, fy); break;
    case Eyes::Spiral:
      drawSpiralEye(L, lx, fy, t);
      drawSpiralEye(L, rx, fy, t);
      break;
    case Eyes::Covered: break;
  }
  if (m.cheeks == Cheeks::On || (m.cheeks == Cheeks::Touch && pet.touching)) drawCheeks(L, fy);
  if (m.eyes == Eyes::Covered) drawArms(L, fy);
}

void rigDraw(M5Canvas& c, uint32_t now, float t) {
  cv = &c;
  const Look& L = oba().look;
  const MoodSpec& m = oba().mood(pet.mood);

  // Movimento do corpo no humor atual
  float bob = channel(m.bob, t);
  float swayX = channel(m.sway, t);
  float squash = channel(m.squash, t);
  float wave = channel(m.wave, t);

  // Virada: a largura passa de um lado pro outro (fica fininho no meio,
  // como quem gira) com um pulinho
  float face = pet.facing, hop = 0;
  if (pet.turnStart) {
    float p = (now - pet.turnStart) / (float)TURN_MS;
    if (p >= 1.f) {
      pet.turnStart = 0;
    } else {
      face = -pet.facing * cosf(p * PI);
      if (fabsf(face) < 0.18f) face = face < 0 ? -0.18f : 0.18f;
      hop = sinf(p * PI) * 12.f;
      squash -= sinf(p * PI) * 0.05f;
    }
  }
  poseX = pet.x + swayX * pet.scale;
  poseY = pet.y + (bob - hop) * pet.scale;
  poseSY = (1.f + squash) * pet.scale;
  poseSX = (1.f - squash * 0.6f) * face * pet.scale;

  float ex = pet.lookX * (pet.lookX < 0 ? L.eyes.lookLeft : L.eyes.lookRight);
  float ey = pet.lookY * L.eyes.lookY;

  c.fillScreen(L.color.bg);

  // Sombra no "chão": menor quando ele sobe
  const auto& sh = L.shadow;
  int shadowW = sc(sh.rx + (bob - hop) * sh.bob);
  c.fillEllipse((int)(pet.x + (swayX * 0.5f - sh.dx * face) * pet.scale), (int)(pet.y + sh.y * pet.scale),
                shadowW, sc(sh.ry), L.color.shadow);

  // Corpo e rosto: o contorno do rig ou o quadro do sprite, na mesma pose
  if (oba().lookType == LookType::Sprites) {
    spriteDraw(c, oba().sprites, pet.mood, now - pet.moodStart, poseX, poseY, poseSX, poseSY);
  } else {
    drawBody(L, t, wave);
    drawFace(L, m, now, t, ex, ey);
  }

  // O enfeite do humor
  if (m.extra == Extra::Stars) drawStars(L, t);
  if (m.extra == Extra::Zzz) drawZzz(L, t);
}
