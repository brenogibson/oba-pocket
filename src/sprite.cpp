#include "sprite.h"
#include <math.h>
#include <algorithm>
#include <lgfx/utility/lgfx_miniz.h>
#include <lgfx/utility/lgfx_pngle.h>

static constexpr int SPRITE_FRAMES_MAX = 32;

// ---------------------------------------------------------------- PNG

// O pngle trava num laço sem fim se o zlib do IDAT acaba antes dos dados do
// IDAT (PNG estragado). Por isso cada pedaço do arquivo passa antes por um
// segundo inflador, que só confere o zlib e joga a saída fora; se ele achar
// problema, a leitura devolve 0 e o pngle para com erro.
struct ZlibCheck {
  lgfx_tinfl_decompressor inf;
  uint8_t dict[TINFL_LZ_DICT_SIZE];
};

struct Decode {
  ObaReader* in;
  SpriteSheet* sheet;
  ZlibCheck* z;
  size_t out = 0;  // posição de saída no dict
  enum { Sig, Head, Data, Crc } phase = Sig;
  uint32_t left = 8;  // bytes que faltam na fase
  uint8_t head[8];    // tamanho e tipo do bloco
  bool idat = false, done = false, bad = false;

  // Um pedaço de IDAT pelo inflador de conferência
  void inflate(const uint8_t* p, size_t n) {
    int st = TINFL_STATUS_NEEDS_MORE_INPUT;
    while (!bad && (n || st == TINFL_STATUS_HAS_MORE_OUTPUT)) {
      if (done) {
        bad = true;  // dados depois do fim do zlib
        return;
      }
      size_t inBytes = n, outBytes = TINFL_LZ_DICT_SIZE - out;
      st = lgfx_tinfl_decompress(&z->inf, p, &inBytes, z->dict, z->dict + out, &outBytes,
                                 TINFL_FLAG_HAS_MORE_INPUT | TINFL_FLAG_PARSE_ZLIB_HEADER);
      p += inBytes;
      n -= inBytes;
      out = (out + outBytes) & (TINFL_LZ_DICT_SIZE - 1);
      // Parado sem entrada não é erro: num bloco stored o dict pode encher junto
      // com o fim do pedaço, e aí a volta seguinte só pede mais entrada
      bool stuck = !inBytes && !outBytes && (n || st == TINFL_STATUS_HAS_MORE_OUTPUT);
      if (st < TINFL_STATUS_DONE || stuck) bad = true;
      else if (st == TINFL_STATUS_DONE) done = true;
    }
  }

  // Acompanha os blocos do PNG em tudo que passa (p nulo = pulado)
  void feed(const uint8_t* p, size_t n) {
    while (n && !bad) {
      size_t k = std::min<size_t>(n, left);
      if (phase == Sig || phase == Head || (phase == Data && idat)) {
        if (!p) {
          bad = true;  // o pngle nunca pula cabeçalho nem IDAT
          return;
        }
        if (phase == Head) memcpy(head + 8 - left, p, k);
        else if (phase == Data) inflate(p, k);
      }
      if (p) p += k;
      n -= k;
      left -= k;
      if (left) continue;
      switch (phase) {
        case Sig: phase = Head, left = 8; break;
        case Head:
          left = (uint32_t)head[0] << 24 | head[1] << 16 | head[2] << 8 | head[3];
          idat = !memcmp(head + 4, "IDAT", 4);
          phase = Data;
          if (!left) phase = Crc, left = 4;
          break;
        case Data: phase = Crc, left = 4; break;
        case Crc: phase = Head, left = 8; break;
      }
    }
  }
};

// buf nulo: o pngle pede para pular (o resto de um bloco que ele não usa)
static uint32_t pngRead(void* user, uint8_t* buf, uint32_t len) {
  Decode* d = (Decode*)user;
  if (d->bad) return 0;
  size_t n = d->in->read(buf, len);
  d->feed(buf, n);
  return d->bad ? 0 : n;
}

// Um pedaço de linha: cada pixel vem como uint32 com A, R, G, B do byte 0 ao 3.
// Sem entrelaçamento, div_x é sempre 1.
static void pngDraw(void* user, uint32_t x, uint32_t y, uint_fast8_t, size_t len, const uint8_t* argb) {
  SpriteSheet* s = ((Decode*)user)->sheet;
  const uint32_t W = (uint32_t)s->w * s->count;
  if (y >= s->h || x >= W) return;
  if (len > W - x) len = W - x;
  size_t i = (size_t)y * W + x;
  for (size_t k = 0; k < len; ++k, ++i, argb += 4) {
    if (argb[0] < 128) continue;  // transparente
    uint16_t c = ((argb[1] & 0xF8) << 8) | ((argb[2] & 0xFC) << 3) | (argb[3] >> 3);
    s->pixels[i] = __builtin_bswap16(c);  // ordem de bytes do M5Canvas
    s->mask[i] = 1;
  }
}

std::shared_ptr<SpriteSheet> spriteDecode(ObaReader& in, uint16_t w, uint16_t h, size_t budget, String& err) {
  ZlibCheck* z = (ZlibCheck*)ps_malloc(sizeof(ZlibCheck));
  pngle_t* p = z ? lgfx_pngle_new() : nullptr;
  if (!p) {
    free(z);
    return err = "sem memória", nullptr;
  }
  lgfx_tinfl_init(&z->inf);
  std::shared_ptr<SpriteSheet> out;
  Decode d;
  d.in = &in;
  d.sheet = nullptr;
  d.z = z;
  if (lgfx_pngle_prepare(p, pngRead, &d) < 0) {
    err = "PNG inválido";
  } else {
    uint32_t W = lgfx_pngle_get_width(p), H = lgfx_pngle_get_height(p);
    uint32_t n = W / w;
    if (lgfx_pngle_get_ihdr(p)->interlace) {
      err = "PNG entrelaçado não vale";
    } else if (H != h || W % w || n < 1 || n > SPRITE_FRAMES_MAX) {
      err = "a folha precisa ter altura " + String(h) + " e largura N x " + String(w) + " (N de 1 a " +
            String(SPRITE_FRAMES_MAX) + ")";
    } else if ((size_t)W * H * 3 > budget) {
      err = "quadros grandes demais (até " + String(OBA_SPRITES_MAX / 1024) + " KB decodificados)";
    } else {
      auto s = std::make_shared<SpriteSheet>();
      s->w = w;
      s->h = h;
      s->count = n;
      s->pixels = (uint16_t*)ps_calloc((size_t)W * H, sizeof(uint16_t));
      s->mask = (uint8_t*)ps_calloc((size_t)W * H, 1);
      d.sheet = s.get();
      if (!s->pixels || !s->mask) err = "sem memória";
      else if (lgfx_pngle_decomp(p, pngDraw) < 0 || !d.done) err = "PNG inválido ou cortado";
      else out = s;
    }
  }
  lgfx_pngle_destroy(p);
  free(z);
  return out;
}

// ---------------------------------------------------------------- desenho

void spriteDraw(M5Canvas& c, const SpriteLook& s, Mood m, uint32_t age, float cx, float cy, float sx, float sy) {
  const SpriteLook::Frames& F = s.frames[(int)m];
  const SpriteSheet* sh = F.data.get();
  if (!sh || !sh->count) return;
  const int frame = (int)((uint64_t)age * F.fps / 1000 % sh->count);

  // Um pixel do PNG na tela
  const float kx = sx * s.scale, ky = sy * s.scale;
  if (fabsf(kx) < 1e-3f || fabsf(ky) < 1e-3f) return;

  // Caixa do quadro na tela
  float x0 = cx - s.originX * kx, x1 = cx + (s.w - s.originX) * kx;
  float y0 = cy - s.originY * ky, y1 = cy + (s.h - s.originY) * ky;
  if (x0 > x1) std::swap(x0, x1);
  if (y0 > y1) std::swap(y0, y1);
  const int W = c.width(), H = c.height();
  static constexpr int SIDE_MAX = 512;
  const int xa = std::max(0, (int)floorf(x0)), xb = std::min(std::min(W, SIDE_MAX) - 1, (int)ceilf(x1));
  const int ya = std::max(0, (int)floorf(y0)), yb = std::min(std::min(H, SIDE_MAX) - 1, (int)ceilf(y1));
  if (xa > xb || ya > yb) return;

  // Vizinho mais próximo: o centro de cada pixel da tela diz qual pixel do
  // quadro vale (-1 = fora). Com sx < 0 as colunas saem de trás para frente.
  static int16_t cols[SIDE_MAX], rows[SIDE_MAX];
  for (int x = xa; x <= xb; ++x) {
    int u = (int)floorf(s.originX + (x + 0.5f - cx) / kx);
    cols[x - xa] = (u >= 0 && u < s.w) ? frame * s.w + u : -1;
  }
  for (int y = ya; y <= yb; ++y) {
    int v = (int)floorf(s.originY + (y + 0.5f - cy) / ky);
    rows[y - ya] = (v >= 0 && v < s.h) ? v : -1;
  }

  // Canvas de 16 bits sem rotação: escreve direto no buffer (os pixels já estão
  // na ordem de bytes dele). Senão, pixel a pixel.
  uint16_t* buf = (uint16_t*)c.getBuffer();
  const bool direct = buf && c.getColorDepth() == lgfx::rgb565_2Byte && c.getRotation() == 0;
  const size_t pitch = direct ? c.bufferLength() / 2 / H : 0;
  const size_t stride = (size_t)sh->w * sh->count;
  for (int y = ya; y <= yb; ++y) {
    int v = rows[y - ya];
    if (v < 0) continue;
    const uint16_t* src = sh->pixels + v * stride;
    const uint8_t* mask = sh->mask + v * stride;
    uint16_t* dst = direct ? buf + y * pitch : nullptr;
    for (int x = xa; x <= xb; ++x) {
      int u = cols[x - xa];
      if (u < 0 || !mask[u]) continue;
      if (dst) dst[x] = src[u];
      else c.drawPixel(x, y, (uint16_t)__builtin_bswap16(src[u]));
    }
  }
}
