#include "ext.h"
#include <vector>
#include "behavior.h"
#include "bubble.h"
#include "bubble_fonts.h"
#include "cloud.h"
#include "oba.h"
#include "protocol.h"
#include "rig.h"
#include "screen.h"
#include "ui.h"

// Limites do contrato
static constexpr size_t MAX_SOURCES = 4, MAX_ASKS = 4, MAX_ANSWERED = 16;
static constexpr uint32_t SRC_TTL_S = 180, SRC_TTL_MIN_S = 10, SRC_TTL_MAX_S = 3600;
static constexpr uint32_t ASK_TTL_S = 120, ASK_TTL_MIN_S = 10, ASK_TTL_MAX_S = 600;
static constexpr uint32_t LAG_MAX_S = 60;  // mensagens sem ttl_s
static constexpr size_t ID_MAX = 32, TOOL_MAX = 40, TITLE_MAX = 60, LABEL_MAX = 48, BODY_MAX = 1500;

// O Oba encolhe na coluna da esquerda, o cartão fica à direita, os botões e a
// dica embaixo
static constexpr float ASK_X = 44, ASK_Y = 104, ASK_SCALE = 0.42f;
static constexpr int CARD_X = 90, CARD_Y = 6, CARD_W = 224, CARD_H = 156, CARD_R = 14, CARD_PAD = 10;
static constexpr int BTN_Y = 168, BTN_H = 38, BTN_W = 148, DENY_X = 8, ALLOW_X = 164;
static constexpr int HINT_H = 22, HINT_Y = SCREEN_H - HINT_H - 4, HINT_MAX_W = SCREEN_W - 12;
static constexpr int PILL_H = 22, PILL_Y = 6, PILL_RIGHT = 314, PILL_MAX_W = 222;
static constexpr int MAX_PAGES = 12;
static constexpr uint32_t WAIT_MS = 260;          // o Oba chega no canto antes do cartão abrir
static constexpr uint32_t POP_MS = 240, CLOSE_MS = 180, GAP_MS = 400;
static constexpr uint32_t ARM_MS = 600;           // toque logo que abre não conta
static constexpr uint32_t ALLOW_GUARD_MS = 500;   // o Aprovar acabou de acender
static constexpr uint32_t HOLD_MS = 1500;         // danger: segurar o Aprovar
static constexpr uint32_t BUZZ_MS = 150;
static constexpr int BUZZ_LEVEL = 180;
static const uint16_t C_AMBER = M5GFX::color565(0xFF, 0xB0, 0x00);

struct Source {
  String src, label;
  Mood mood;
  uint32_t until;
};
struct Ask {
  String src, id, tool, title, body, label;
  bool danger = false;
  uint32_t until = 0;
};
struct Answer {
  String src, id, choice;
};

static std::vector<Source> sources;
static std::vector<Ask> asks;  // em ordem de chegada
static Answer answered[MAX_ANSWERED];
static size_t answeredNext = 0;
static bool stateDirty = false;

enum class Phase : uint8_t { None, Waiting, Opening, Open, Closing, Gap };
static Phase phase = Phase::None;
static uint32_t phaseAt = 0;
static String shownSrc, shownId;  // o pedido do cartão

// O cartão é pré-desenhado num sprite; a cor-chave marca os cantos transparentes
static M5Canvas* card = nullptr;
static uint16_t key = 0;
static String cardFor;  // sha256 do Oba das cores do cartão
static bool danger = false, titleBig = true;
static std::vector<String> titleLines, lines;
static int page = 0, pages = 1, cap0 = 0, capN = 0;
static bool seenAll = true;  // já passou pela última página: o Aprovar acende
static uint32_t allowAt = 0;

enum class Hit : uint8_t { Nothing, Card, Deny, Allow };
static Hit held = Hit::Nothing;  // onde o dedo desceu
static uint32_t heldAt = 0;
static bool armed = false;       // o dedo saiu da tela depois que o cartão abriu

static bool after(uint32_t now, uint32_t t) { return (int32_t)(now - t) >= 0; }
static float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

static void setPhase(Phase p, uint32_t now) {
  phase = p;
  phaseAt = now;
}

// ---------------------------------------------------------------- texto

static int utf8Len(uint8_t lead) { return lead < 0x80 ? 1 : lead < 0xE0 ? 2 : lead < 0xF0 ? 3 : 4; }

static void putUtf8(String& out, uint32_t cp) {
  char b[4] = {};
  if (cp < 0x80) {
    b[0] = cp;
  } else if (cp < 0x800) {
    b[0] = 0xC0 | cp >> 6, b[1] = 0x80 | (cp & 0x3F);
  } else {
    b[0] = 0xE0 | cp >> 12, b[1] = 0x80 | (cp >> 6 & 0x3F), b[2] = 0x80 | (cp & 0x3F);
  }
  out += b;
}

// Só o que a fonte do balão desenha (ASCII, Latin-1 e a pontuação de
// tools/make_font.py); o resto vira '?'. Tab vira espaço; '\n' só com keepLines.
// Mais de maxChars: corta com "…".
static String clean(const char* s, size_t maxChars, bool keepLines) {
  static const uint16_t EXTRA[] = {0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026};
  String out;
  size_t chars = 0;
  const uint8_t* p = (const uint8_t*)s;
  while (*p) {
    int n = utf8Len(*p);
    bool ok = !(*p >= 0x80 && *p < 0xC2) && *p < 0xF5;  // byte de continuação ou fora do UTF-8
    if (!ok) n = 1;
    uint32_t cp = n == 1 ? *p : *p & (0x7F >> n);
    for (int i = 1; ok && i < n; i++) {
      if ((p[i] & 0xC0) != 0x80) ok = false, n = i;  // sequência cortada: pula só o que veio
      else cp = cp << 6 | (p[i] & 0x3F);
    }
    p += n;
    if (!ok) cp = '?';
    if (cp == '\r') continue;
    if (cp == '\t' || (cp == '\n' && !keepLines)) cp = ' ';
    bool drawable = cp == '\n' || (cp >= 0x20 && cp < 0x7F) || (cp >= 0xA0 && cp <= 0xFF);
    for (uint16_t e : EXTRA) drawable |= cp == e;
    if (!drawable) cp = '?';
    if (++chars > maxChars) {
      out += "…";
      break;
    }
    putUtf8(out, cp);
  }
  out.trim();
  return out;
}

static bool validId(const String& id) {
  if (!id.length() || id.length() > ID_MAX) return false;
  for (char ch : id) {
    if (!isalnum((unsigned char)ch) && ch != '_' && ch != '-') return false;
  }
  return true;
}

// O maior começo da palavra que cabe em maxW, de preferência logo depois de
// / - _ . (caminhos e comandos)
static int fitPrefix(M5Canvas& c, const String& w, int maxW) {
  int best = 0, soft = 0;
  for (int i = 0; i < (int)w.length();) {
    int n = utf8Len(w[i]);
    if (c.textWidth(w.substring(0, i + n)) > maxW) break;
    i += n;
    best = i;
    if (strchr("/-_.,=&|;:", w[i - 1])) soft = i;
  }
  if (!best) best = utf8Len(w[0]);  // nem um caractere cabe: vai assim mesmo
  return soft > best / 2 ? soft : best;
}

// Como wrapText, mas '\n' quebra a linha e palavra maior que a linha é cortada
static void wrapHard(M5Canvas& c, const String& text, int maxW, std::vector<String>& out) {
  int start = 0, len = text.length();
  for (;;) {
    int nl = text.indexOf('\n', start);
    if (nl < 0) nl = len;
    String para = text.substring(start, nl), line;
    for (int i = 0; i < (int)para.length();) {
      int sp = para.indexOf(' ', i);
      if (sp < 0) sp = para.length();
      String word = para.substring(i, sp);
      i = sp + 1;
      if (!word.length()) continue;
      String next = line.length() ? line + " " + word : word;
      if (c.textWidth(next) <= maxW) {
        line = next;
        continue;
      }
      if (line.length()) out.push_back(line);
      while (c.textWidth(word) > maxW) {
        int cut = fitPrefix(c, word, maxW);
        out.push_back(word.substring(0, cut));
        word = word.substring(cut);
      }
      line = word;
    }
    // Linha em branco só uma, e nenhuma no começo
    if (line.length() || (out.size() && out.back().length())) out.push_back(line);
    if (nl >= len) break;
    start = nl + 1;
  }
  while (out.size() && !out.back().length()) out.pop_back();
}

// Corta o fim com "…" até caber em maxW
static String ellipsize(M5Canvas& c, String s, int maxW) {
  if (c.textWidth(s) <= maxW) return s;
  while (s.length() && c.textWidth(s + "…") > maxW) {
    int i = s.length() - 1;
    while (i > 0 && ((uint8_t)s[i] & 0xC0) == 0x80) --i;  // não parte um caractere
    s.remove(i);
  }
  s.trim();
  return s + "…";
}

// ---------------------------------------------------------------- fontes e pedidos

static Source* findSource(const String& src) {
  for (Source& s : sources) {
    if (s.src == src) return &s;
  }
  return nullptr;
}

static int findAsk(const String& src, const String& id) {
  for (size_t i = 0; i < asks.size(); i++) {
    if (asks[i].src == src && asks[i].id == id) return i;
  }
  return -1;
}

static bool isShown(const Ask& a) {
  return phase != Phase::None && phase != Phase::Gap && a.src == shownSrc && a.id == shownId;
}

static const Answer* findAnswer(const String& src, const String& id) {
  for (const Answer& a : answered) {
    if (a.id == id && a.src == src) return &a;
  }
  return nullptr;
}

static void reply(const String& src, const String& id, const char* choice, const char* error = nullptr) {
  JsonDocument r;
  r["v"] = 1;
  r["type"] = "reply";
  r["ts"] = cloudEpochMs();
  r["oba"] = oba().id;
  r["re"] = "ask";
  r["id"] = id;
  r["choice"] = choice;
  if (error) r["error"] = error;
  String out;
  serializeJson(r, out);
  cloudSendExt(src, out);
  Serial.printf("[ext] %s %s: %s%s%s\n", src.c_str(), id.c_str(), choice, error ? " " : "", error ? error : "");
}

// A resposta vale para sempre: o mesmo pedido de novo recebe a mesma
static void answerAndForget(size_t i, const char* choice, const char* error = nullptr) {
  reply(asks[i].src, asks[i].id, choice, error);
  answered[answeredNext] = {asks[i].src, asks[i].id, choice};
  answeredNext = (answeredNext + 1) % MAX_ANSWERED;
  asks.erase(asks.begin() + i);
  stateDirty = true;
}

static void dropSource(String src) {  // cópia: o erase apaga o original
  for (size_t i = asks.size(); i-- > 0;) {
    if (asks[i].src == src) asks.erase(asks.begin() + i);
  }
  for (size_t i = 0; i < sources.size(); i++) {
    if (sources[i].src == src) {
      sources.erase(sources.begin() + i);
      stateDirty = true;
      Serial.printf("[ext] %s saiu\n", src.c_str());
      return;
    }
  }
}

static uint32_t ttlMs(JsonVariantConst v, uint32_t def, uint32_t lo, uint32_t hi) {
  uint32_t s = v | def;
  return (s < lo ? lo : s > hi ? hi : s) * 1000;
}

static void onAsk(const String& src, const JsonDocument& d, uint32_t ttl, uint32_t now) {
  String id = d["id"] | "";
  if (!validId(id)) {
    Serial.printf("[ext] %s: pedido com id inválido\n", src.c_str());
    return;
  }
  if (const Answer* r = findAnswer(src, id)) return reply(src, id, r->choice.c_str());
  Source* s = findSource(src);
  if (!s) {
    if (sources.size() >= MAX_SOURCES) return reply(src, id, "skip", "fontes demais");
    sources.push_back({src, "", Mood::Idle, now + SRC_TTL_S * 1000});
    s = &sources.back();
    stateDirty = true;
  }
  Ask a;
  a.src = src;
  a.id = id;
  a.tool = clean(d["tool"] | "", TOOL_MAX, false);
  a.title = clean(d["title"] | "", TITLE_MAX, false);
  a.body = clean(d["body"] | "", BODY_MAX, true);
  a.label = clean(d["label"] | "", LABEL_MAX, false);
  a.danger = d["danger"] | false;
  a.until = now + ttl;
  if (!a.title.length()) return reply(src, id, "skip", "falta o title");
  if (!after(s->until, a.until)) s->until = a.until;  // a fonte dura pelo menos o pedido

  int i = findAsk(src, id);
  if (i >= 0) {
    if (isShown(asks[i])) asks[i].until = a.until;  // o da tela não muda embaixo do dedo
    else asks[i] = a;
    return;
  }
  if (asks.size() >= MAX_ASKS) return reply(src, id, "skip", "fila cheia");
  asks.push_back(a);
  stateDirty = true;
  Serial.printf("[ext] %s pede %s: %s\n", src.c_str(), id.c_str(), a.title.c_str());
}

static void handle(const String& src, const String& json, uint32_t now) {
  JsonDocument d;
  if (deserializeJson(d, json) || (d["v"] | 1) != 1) {
    Serial.printf("[ext] %s: mensagem inválida\n", src.c_str());
    return;
  }
  const char* type = d["type"] | "";
  // Atraso pelo relógio (os dois com NTP): chegou tarde demais, não vale mais
  uint64_t ts = d["ts"] | (uint64_t)0, epoch = cloudEpochMs();
  int64_t lag = ts && epoch ? (int64_t)(epoch - ts) : 0;
  uint32_t lagMs = lag > 0 ? (uint32_t)min<int64_t>(lag, 3600000) : 0;
  bool isAsk = !strcmp(type, "ask"), isStatus = !strcmp(type, "status");
  uint32_t ttl = isAsk ? ttlMs(d["ttl_s"], ASK_TTL_S, ASK_TTL_MIN_S, ASK_TTL_MAX_S)
               : isStatus ? ttlMs(d["ttl_s"], SRC_TTL_S, SRC_TTL_MIN_S, SRC_TTL_MAX_S)
               : LAG_MAX_S * 1000;
  if (lagMs >= ttl) {
    Serial.printf("[ext] %s: %s atrasado %u ms, fora\n", src.c_str(), type, lagMs);
    return;
  }
  ttl -= lagMs;

  if (isStatus) {
    if (d["online"].is<bool>() && !d["online"].as<bool>()) return dropSource(src);
    const char* name = d["mood"] | "";
    Mood mood;
    if (!moodByName(name, &mood) || (mood != Mood::Idle && !moodExternal(mood))) {
      Serial.printf("[ext] %s: humor %s não vale\n", src.c_str(), name);
      return;
    }
    Source* s = findSource(src);
    if (!s) {
      if (sources.size() >= MAX_SOURCES) {
        Serial.printf("[ext] %s: fontes demais\n", src.c_str());
        return;
      }
      sources.push_back({src, "", Mood::Idle, 0});
      s = &sources.back();
      stateDirty = true;
      Serial.printf("[ext] %s entrou\n", src.c_str());
    }
    stateDirty |= s->mood != mood;
    s->mood = mood;
    s->label = clean(d["label"] | "", LABEL_MAX, false);
    s->until = now + ttl;
  } else if (isAsk) {
    onAsk(src, d, ttl, now);
  } else if (!strcmp(type, "ask.cancel")) {
    int i = findAsk(src, d["id"] | "");
    if (i >= 0) {
      asks.erase(asks.begin() + i);
      stateDirty = true;
    }
  } else {
    String err;
    if (!protoEffect(d, now, err)) err = "type desconhecido";
    if (err.length()) Serial.printf("[ext] %s: %s: %s\n", src.c_str(), type, err.c_str());
  }
}

// O mais urgente entre as fontes; pedido na fila conta como alert
static int urgency(Mood m) { return m == Mood::Alert ? 2 : m == Mood::Busy ? 1 : 0; }

static Mood restMood() {
  if (!asks.empty()) return Mood::Alert;
  Mood m = Mood::Idle;
  for (const Source& s : sources) {
    if (urgency(s.mood) > urgency(m)) m = s.mood;
  }
  return m;
}

// ---------------------------------------------------------------- cartão

static uint16_t mix(uint16_t a, uint16_t b, float t) {
  auto ch = [&](int shift, int mask) {
    int x = (a >> shift) & mask, y = (b >> shift) & mask;
    return ((int)(x + (y - x) * t) & mask) << shift;
  };
  return ch(11, 0x1F) | ch(5, 0x3F) | ch(0, 0x1F);
}

static int lineHeight(M5Canvas& c) { return c.fontHeight() + 1; }

// Quebra o título e o corpo e conta as páginas: a primeira tem o título, as
// outras só o corpo. Com mais de uma página, o rodapé come uma linha.
static void layout(const Ask& a) {
  M5Canvas& c = *card;
  const int w = CARD_W - 2 * CARD_PAD;
  c.loadFont(BUBBLE_FONT_SMALL);
  int lh = lineHeight(c);
  titleLines.clear();
  c.loadFont(BUBBLE_FONT_BIG);
  int lhTitle = lineHeight(c);
  wrapHard(c, a.title, w, titleLines);
  titleBig = titleLines.size() <= (a.body.length() ? 1 : 2);  // com corpo, o comando aparece já na 1ª página
  if (!titleBig) {
    c.loadFont(BUBBLE_FONT_SMALL);
    lhTitle = lh;
    titleLines.clear();
    wrapHard(c, a.title, w, titleLines);
    if (titleLines.size() > 3) {
      titleLines.resize(3);
      titleLines.back() = ellipsize(c, titleLines.back() + " …", w);
    }
  }
  c.loadFont(BUBBLE_FONT_SMALL);
  lines.clear();
  wrapHard(c, a.body, w, lines);
  int top = CARD_PAD + lh + 2, bottom = CARD_H - CARD_PAD;
  int titleH = titleLines.size() * lhTitle + 4;
  int fit = max(0, (bottom - top - titleH) / lh);
  if ((int)lines.size() <= fit) {
    pages = 1;
    cap0 = fit;
  } else {
    cap0 = max(0, (bottom - lh - top - titleH) / lh);
    capN = (bottom - lh - top) / lh;
    pages = 1 + ((int)lines.size() - cap0 + capN - 1) / capN;
    if (pages > MAX_PAGES) {  // cortado: o fim não aparece, então aprovar pede a segurada
      pages = MAX_PAGES;
      lines.resize(cap0 + (MAX_PAGES - 1) * capN);
      lines.back() = "…";
      danger = true;
    }
  }
  c.unloadFont();
}

static void render(const Ask& a) {
  const auto& color = oba().look.color;
  key = 0xF81F;
  while (key == color.bubble || key == color.ink || key == C_UI_RED) --key;
  cardFor = oba().sha256;
  M5Canvas& c = *card;
  const int w = CARD_W - 2 * CARD_PAD;
  const uint16_t dim = mix(color.ink, color.bubble, 0.45f);
  c.fillScreen(key);
  c.fillRoundRect(0, 0, CARD_W, CARD_H, CARD_R, color.bubble);
  if (danger) {
    c.drawRoundRect(0, 0, CARD_W, CARD_H, CARD_R, C_UI_RED);
    c.drawRoundRect(1, 1, CARD_W - 2, CARD_H - 2, CARD_R - 1, C_UI_RED);
  }
  c.setTextSize(1);
  c.setTextDatum(top_left);

  // Cabeçalho: a ferramenta e de onde veio
  c.loadFont(BUBBLE_FONT_SMALL);
  int lh = lineHeight(c), y = CARD_PAD;
  String head = a.tool;
  if (head.length() && a.label.length()) head += " · ";
  head += a.label;
  if (!head.length()) head = "Pedido";
  c.setTextColor(danger ? C_UI_RED : dim, color.bubble);
  c.drawString(ellipsize(c, head, w), CARD_PAD, y);
  y += lh + 2;

  if (page == 0) {
    c.loadFont(titleBig ? BUBBLE_FONT_BIG : BUBBLE_FONT_SMALL);
    c.setTextColor(color.ink, color.bubble);
    for (const String& l : titleLines) {
      c.drawString(l, CARD_PAD, y);
      y += lineHeight(c);
    }
    y += 4;
    c.loadFont(BUBBLE_FONT_SMALL);
  }
  c.setTextColor(color.ink, color.bubble);
  int first = page == 0 ? 0 : cap0 + (page - 1) * capN, count = page == 0 ? cap0 : capN;
  for (int i = first; i < first + count && i < (int)lines.size(); i++) {
    c.drawString(lines[i], CARD_PAD, y);
    y += lh;
  }

  if (pages > 1) {
    int fy = CARD_H - CARD_PAD - lh + 2;
    c.setTextColor(dim, color.bubble);
    if (page + 1 < pages) c.drawString("toque para ler mais", CARD_PAD, fy);
    c.setTextDatum(top_right);
    c.drawString(String(page + 1) + "/" + String(pages), CARD_W - CARD_PAD, fy);
  }
  c.unloadFont();
}

static const Ask* shownAsk() {
  int i = findAsk(shownSrc, shownId);
  return i >= 0 ? &asks[i] : nullptr;
}

static void open(const Ask& a, uint32_t now) {
  shownSrc = a.src;
  shownId = a.id;
  danger = a.danger;
  layout(a);
  page = 0;
  seenAll = pages == 1;
  allowAt = now;
  held = Hit::Nothing;
  armed = false;
  render(a);
  setPhase(Phase::Waiting, now);
  cloudSetBubbleBusy(true);  // regra de fala espera o pedido
  pet.lastActivity = now;
  if (pet.facing < 0) petTurnTo(1.f, now);  // vira para o cartão
  protoVibrate(BUZZ_MS, BUZZ_LEVEL, now);
  Serial.printf("[ext] na tela: %s %s (%d páginas)\n", a.src.c_str(), a.id.c_str(), pages);
}

static void nextPage(uint32_t now) {
  if (pages < 2) return;
  page = (page + 1) % pages;
  if (page == pages - 1 && !seenAll) {
    seenAll = true;
    allowAt = now + ALLOW_GUARD_MS;
  }
  if (const Ask* a = shownAsk()) render(*a);
}

// Resposta pelo toque (ou pela serial) ao pedido na tela
static void decide(const char* choice, uint32_t now) {
  int i = findAsk(shownSrc, shownId);
  if (i < 0 || phase != Phase::Open) return;
  answerAndForget(i, choice);
  if (!strcmp(choice, "allow")) petEnterMood(Mood::Happy, now);
  if (!strcmp(choice, "deny")) petEnterMood(Mood::Shy, now);
  held = Hit::Nothing;
  setPhase(Phase::Closing, now);
}

// ---------------------------------------------------------------- API

void extBegin(M5Canvas* screen) {
  card = new M5Canvas(screen);
  card->setColorDepth(16);
  card->setPsram(true);
  if (!card->createSprite(CARD_W, CARD_H)) Serial.println("falha ao criar o buffer do pedido");
}

void extInject(const String& src, const String& json, uint32_t now) { handle(src, json, now); }

void extUpdate(uint32_t now) {
  for (int n = 0; n < 8; n++) {
    ExtMessage* m = cloudTakeExt();
    if (!m) break;
    handle(m->src, m->json, now);
    delete m;
  }

  // Validade: pedido vencido volta para o terminal; fonte calada sai com os pedidos
  for (size_t i = asks.size(); i-- > 0;) {
    if (after(now, asks[i].until)) answerAndForget(i, "skip", "venceu");
  }
  for (size_t i = sources.size(); i-- > 0;) {
    if (after(now, sources[i].until)) dropSource(sources[i].src);
  }

  petSetRest(restMood(), now);
  if (stateDirty) {
    protoStateChanged();
    stateDirty = false;
  }

  uint32_t age = now - phaseAt;
  switch (phase) {
    case Phase::None:
      if (!asks.empty() && bubbleIdle()) open(asks.front(), now);
      break;
    case Phase::Waiting:
    case Phase::Opening:
    case Phase::Open:
      pet.lastActivity = now;  // não dorme com o pedido na tela
      if (!shownAsk()) {       // respondido em outro lugar, vencido ou a fonte saiu
        setPhase(phase == Phase::Waiting ? Phase::Gap : Phase::Closing, now);  // não abriu: nada a fechar
        break;
      }
      if (cardFor != oba().sha256) render(*shownAsk());  // trocou o Oba: as cores novas
      if (phase == Phase::Waiting && age > WAIT_MS) setPhase(Phase::Opening, now);
      else if (phase == Phase::Opening && age > POP_MS) setPhase(Phase::Open, now);
      break;
    case Phase::Closing:
      if (age > CLOSE_MS) setPhase(Phase::Gap, now);
      break;
    case Phase::Gap:
      if (age > GAP_MS) {
        phase = Phase::None;
        if (bubbleIdle()) cloudSetBubbleBusy(false);
      }
      break;
  }
}

static Hit hitAt(int x, int y) {
  if (y < 0 || y >= SCREEN_H) return Hit::Nothing;  // botões virtuais do Core2
  if (x >= CARD_X && y >= CARD_Y && y < CARD_Y + CARD_H) return Hit::Card;
  if (y >= BTN_Y - 6 && y < BTN_Y + BTN_H + 6) {
    if (x >= DENY_X - 6 && x < DENY_X + BTN_W) return Hit::Deny;
    if (x >= ALLOW_X && x < ALLOW_X + BTN_W + 6) return Hit::Allow;
  }
  return Hit::Nothing;
}

static bool allowReady(uint32_t now) { return seenAll && after(now, allowAt); }

bool extTouch(const m5::touch_detail_t& td, uint32_t now) {
  if (!extShowing()) {
    held = Hit::Nothing;
    return false;
  }
  if (phase != Phase::Open) return true;
  // Toque novo: o dedo que já estava na tela quando o cartão abriu não conta
  if (!armed) {
    armed = !td.isPressed() && now - phaseAt >= ARM_MS;
    return true;
  }
  if (td.wasPressed()) {
    held = hitAt(td.x, td.y);
    heldAt = now;
  }
  if (held == Hit::Nothing) return true;
  Hit at = hitAt(td.x, td.y);
  if (td.isPressed()) {
    if (at != held) {
      held = Hit::Nothing;  // saiu de cima: desiste
    } else if (held == Hit::Allow && danger && allowReady(now) && !after(allowAt, heldAt + 1) &&
               now - heldAt >= HOLD_MS) {
      decide("allow", now);
    }
    return true;
  }
  Hit h = held;
  held = Hit::Nothing;
  if (!td.wasReleased() || at != h) return true;
  if (h == Hit::Card) nextPage(now);
  if (h == Hit::Deny) decide("deny", now);
  if (h == Hit::Allow) {
    if (!seenAll) nextPage(now);
    else if (!danger && allowReady(now) && !after(allowAt, heldAt + 1)) decide("allow", now);
  }
  return true;
}

bool extSkip(uint32_t now) {
  if (!extShowing()) return false;
  if (phase == Phase::Open && now - phaseAt >= ARM_MS) decide("skip", now);
  return true;
}

bool extAnswer(bool allow, uint32_t now) {
  if (phase != Phase::Open) return false;
  decide(allow ? "allow" : "deny", now);
  return true;
}

bool extShowing() {
  return phase == Phase::Waiting || phase == Phase::Opening || phase == Phase::Open || phase == Phase::Closing;
}

bool extAsking() { return phase != Phase::None || !asks.empty(); }

bool extPlacement(float* x, float* y, float* scale) {
  if (!extShowing() && !(phase == Phase::Gap && !asks.empty())) return false;
  *x = ASK_X;
  *y = ASK_Y;
  *scale = ASK_SCALE;
  return true;
}

void extState(JsonDocument& d) {
  if (sources.empty()) return;
  JsonArray list = d["ext"].to<JsonArray>();
  for (const Source& s : sources) {
    JsonObject o = list.add<JsonObject>();
    o["src"] = s.src;
    o["mood"] = moodName(s.mood);
    int n = 0;
    for (const Ask& a : asks) n += a.src == s.src;
    o["asks"] = n;
  }
}

// ---------------------------------------------------------------- desenho

static void drawButton(M5Canvas& c, int x, const char* label, uint16_t accent, bool on, bool pressed, float fill) {
  c.fillRoundRect(x, BTN_Y, BTN_W, BTN_H, BTN_H / 2, pressed && fill <= 0 ? accent : C_UI_BTN);
  if (fill > 0) {
    c.setClipRect(x, BTN_Y, (int)(BTN_W * fill), BTN_H);
    c.fillRoundRect(x, BTN_Y, BTN_W, BTN_H, BTN_H / 2, accent);
    c.clearClipRect();
  }
  c.drawRoundRect(x, BTN_Y, BTN_W, BTN_H, BTN_H / 2, on ? accent : C_UI_DIM);
  c.setTextColor(pressed && fill <= 0 ? C_UI_BG : on ? C_UI_TEXT : C_UI_DIM);
  c.setTextDatum(middle_center);
  c.drawString(label, x + BTN_W / 2, BTN_Y + BTN_H / 2 + 1);
}

// Pílula com fundo de sombra, como a do REC: o fundo do Oba muda de um para outro
// right < 0: centralizada
static void drawPill(M5Canvas& c, int right, int y, int maxW, const String& text, uint16_t dot, bool pulse,
                     uint32_t now) {
  const auto& color = oba().look.color;
  static constexpr int PAD = 10, DOT = 12;
  int dotW = dot ? DOT : 0;
  String t = ellipsize(c, text, maxW - 2 * PAD - dotW);
  int w = c.textWidth(t) + 2 * PAD + dotW;
  int x = right < 0 ? (SCREEN_W - w) / 2 : right - w;
  c.fillRoundRect(x, y, w, PILL_H, PILL_H / 2, color.shadow);
  if (dot) {
    float p = pulse ? 0.5f + 0.5f * sinf(now / 200.f) : 0.f;
    c.fillCircle(x + PAD + 3, y + PILL_H / 2, 4 + (int)(p * 1.5f), dot);
  }
  c.setTextColor(color.text);
  c.setTextDatum(middle_left);
  c.drawString(t, x + PAD + dotW, y + PILL_H / 2 + 1);
}

static void drawAsk(M5Canvas& canvas, uint32_t now) {
  const auto& color = oba().look.color;
  uint32_t age = now - phaseAt;
  float zoom = 1.f;
  if (phase == Phase::Waiting) return;
  if (phase == Phase::Opening) {
    float p = clampf(age / (float)POP_MS, 0.f, 1.f) - 1.f;  // easeOutBack, como o balão
    zoom = 1.f + 2.2f * p * p * p + 1.2f * p * p;
  }
  if (phase == Phase::Closing) zoom = 1.f - 0.8f * clampf(age / (float)CLOSE_MS, 0.f, 1.f);
  if (zoom < 0.97f || zoom > 1.03f) {
    card->pushRotateZoom(&canvas, CARD_X + CARD_W / 2, CARD_Y + CARD_H / 2, 0.f, zoom, zoom, key);
    return;
  }
  canvas.fillRoundRect(CARD_X + 4, CARD_Y + 4, CARD_W, CARD_H, CARD_R, color.shadow);
  // Rabinho apontando para a cabeça do Oba: é ele quem pergunta
  int tipX, tipY;
  rigPoint(oba().look.bubble.x, oba().look.bubble.y, &tipX, &tipY);
  canvas.fillTriangle(CARD_X + 10, CARD_Y + CARD_H - 70, CARD_X + 10, CARD_Y + CARD_H - 44, tipX, tipY,
                      color.bubble);
  card->pushSprite(&canvas, CARD_X, CARD_Y, key);
  if (phase != Phase::Open) return;

  bool ready = allowReady(now);
  float fill = held == Hit::Allow && danger && ready && !after(allowAt, heldAt + 1)
                   ? clampf((now - heldAt) / (float)HOLD_MS, 0.f, 1.f)
                   : 0.f;
  canvas.loadFont(BUBBLE_FONT_BIG);
  drawButton(canvas, DENY_X, "Negar", C_UI_DIM, true, held == Hit::Deny, 0);
  drawButton(canvas, ALLOW_X, seenAll ? "Aprovar" : "Ler mais", seenAll ? C_UI_GREEN : C_UI_DIM, seenAll,
             held == Hit::Allow && (!danger || !seenAll), fill);

  // Dica embaixo: o que falta fazer, a fila e o tempo que resta
  canvas.loadFont(BUBBLE_FONT_SMALL);
  const Ask* a = shownAsk();
  String hint = !seenAll ? "Leia até o fim para aprovar"
                : danger && (now / 3000) % 2 ? "Segure Aprovar por 1,5 s"
                : "Terminal: botão do meio";
  uint32_t left = a && !after(now, a->until) ? (a->until - now) / 1000 : 0;
  char tail[24];
  snprintf(tail, sizeof tail, " · %u:%02u", (unsigned)(left / 60), (unsigned)(left % 60));
  String rest = tail;
  if (asks.size() > 1) rest = " · +" + String(asks.size() - 1) + rest;
  int room = HINT_MAX_W - 20 - canvas.textWidth(rest);
  drawPill(canvas, -1, HINT_Y, HINT_MAX_W, ellipsize(canvas, hint, room) + rest, 0, false, now);
  canvas.unloadFont();
}

// Rótulo das fontes no alto: a bolinha é o humor da mais urgente
static void drawSources(M5Canvas& canvas, uint32_t now) {
  const Source* top = &sources.front();
  for (const Source& s : sources) {
    if (urgency(s.mood) > urgency(top->mood)) top = &s;
  }
  static const char* const WORDS[] = {"livre", "trabalhando", "esperando você"};
  String text = top->label.length() ? top->label : String(WORDS[urgency(top->mood)]);
  if (sources.size() > 1) text += " +" + String(sources.size() - 1);
  uint16_t dot = top->mood == Mood::Alert ? C_AMBER : top->mood == Mood::Busy ? oba().look.color.star : C_UI_GREEN;
  canvas.loadFont(BUBBLE_FONT_SMALL);
  drawPill(canvas, PILL_RIGHT, PILL_Y, PILL_MAX_W, text, dot, top->mood == Mood::Alert, now);
  canvas.unloadFont();
}

void extDraw(M5Canvas& canvas, uint32_t now) {
  if (extShowing()) return drawAsk(canvas, now);
  if (phase == Phase::None && bubbleIdle() && !sources.empty()) drawSources(canvas, now);
}
