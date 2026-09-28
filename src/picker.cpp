#include "picker.h"
#include <M5Unified.h>
#include "behavior.h"
#include "bubble_fonts.h"
#include "leds.h"
#include "oba.h"
#include "rig.h"
#include "sound.h"
#include "ui.h"

// O Oba fica um pouco menor e mais alto, para caber o nome e as dicas
static constexpr float PREVIEW_Y = 108, PREVIEW_SCALE = 0.7f;
static constexpr int ARROW_ZONE = 72;              // faixa de toque de cada seta
static constexpr int FLICK_MIN = 40;               // arrasto mínimo para trocar (px)
static constexpr uint32_t PICKER_IDLE_MS = 60000;  // fecha sozinha sem toque

// Deixa o Oba da lista como ativo só na memória (a NVS fica com o de antes)
// e começa de novo, feliz, no lugar da prévia
static void show(const ObaInfo& info, uint32_t now) {
  if (oba().id != info.id) {
    String err;
    ObaSpec* s = obaLoad(info.id, err);
    if (s) obaUse(s, false);
    else Serial.printf("[picker] %s: %s\n", info.id.c_str(), err.c_str());
  }
  petBegin(now);
  pet.x = HOME_X;
  pet.y = PREVIEW_Y;
  pet.scale = PREVIEW_SCALE;
}

static int indexOf(const std::vector<ObaInfo>& list, const String& id) {
  for (int k = 0; k < (int)list.size(); ++k) if (list[k].id == id) return k;
  return 0;
}

static void drawArrow(M5Canvas& c, int cx, int dir) {
  const auto& color = oba().look.color;
  int cy = (int)PREVIEW_Y;
  c.fillCircle(cx, cy, 16, color.shadow);
  c.fillTriangle(cx + 6 * dir, cy, cx - 4 * dir, cy - 8, cx - 4 * dir, cy + 8, color.text);
}

static void draw(M5Canvas& c, const std::vector<ObaInfo>& list, int i, bool inUse, uint32_t now) {
  rigDraw(c, now, now / 1000.f);  // com a fonte padrão (o "z" do sonolento usa ela)
  const auto& color = oba().look.color;
  if (list.size() > 1) {
    drawArrow(c, 24, -1);
    drawArrow(c, c.width() - 24, 1);
  }

  // Nome numa pílula no topo, na cor do Oba
  c.loadFont(BUBBLE_FONT_BIG);
  int w = c.textWidth(list[i].name) + 36;
  c.fillRoundRect((c.width() - w) / 2, 8, w, 34, 17, color.shadow);
  c.setTextColor(color.text);
  c.setTextDatum(middle_center);
  c.drawString(list[i].name, c.width() / 2, 26);

  // Embaixo: onde ele está e o que dá para fazer
  c.loadFont(BUBBLE_FONT_SMALL);
  c.fillRoundRect(10, 190, c.width() - 20, 44, 14, color.shadow);
  String info = String(i + 1) + " de " + list.size() + " · " + (list[i].onCard ? "no cartão" : "embutido");
  if (list[i].version.length()) info += " · v" + list[i].version;
  if (inUse) info += " · em uso";
  c.drawString(info, c.width() / 2, 202);
  c.drawString(list[i].onCard && !inUse ? "Toque para usar · segure para remover" : "Toque para usar",
               c.width() / 2, 222);
  c.unloadFont();
  c.pushSprite(0, 0);
}

// Segurou no Oba: só os do cartão saem, e nunca o que está em uso
static void askRemove(M5Canvas& c, std::vector<ObaInfo>& list, int& i, const String& inUse) {
  const ObaInfo target = list[i];
  while (M5.Touch.getDetail().isPressed()) {  // o "segure" da confirmação é outro toque
    M5.update();
    delay(10);
  }
  if (!target.onCard || target.id == inUse) {
    String title = target.onCard ? "O " + target.name + " está em uso" : "O " + target.name + " vem na placa";
    uiNotice(c, title.c_str(),
             target.onCard ? "Escolha outro Oba antes de remover este." : "Dá para remover só os Obas do cartão.");
    delay(2000);
    return;
  }
  String title = "Remover o " + target.name + "?";
  if (!uiConfirmHold(c, title.c_str(), "Apaga a pasta dele do cartão. Para ter ele de volta, instale de novo.",
                     "Segure para remover")) {
    return;
  }
  obaBegin();  // o Oba ativo não pode ser apagado: volta o que está em uso
  String err;
  bool ok = obaRemove(target.id, err);
  Serial.printf("[picker] remover %s: %s\n", target.id.c_str(), ok ? "ok" : err.c_str());
  uiNotice(c, ok ? (target.name + " removido").c_str() : "Não deu para remover", ok ? "" : err.c_str());
  delay(1500);
  list = obaList();
  i = indexOf(list, inUse);
}

bool pickerRun(M5Canvas& c) {
  const String inUse = oba().id;
  std::vector<ObaInfo> list = obaList();
  int i = indexOf(list, inUse);
  Serial.printf("[picker] aberto, %u Obas\n", (unsigned)list.size());
  uint32_t lastTouch = millis();
  bool changed = false;
  show(list[i], lastTouch);

  for (;;) {
    M5.update();
    uint32_t now = millis();
    const auto& td = M5.Touch.getDetail();
    if (td.isPressed()) lastTouch = now;
    // Pela serial: 'S' tira print e 'O' fecha, como o botão do meio
    int key = Serial.available() ? Serial.read() : -1;
    if (key == 'S') uiDumpScreen(c, "picker");
    if (key == 'O' || M5.BtnB.wasClicked() || now - lastTouch > PICKER_IDLE_MS) break;

    // Fora de 0..240 ficam os botões virtuais do Core2
    bool onScreen = td.y >= 0 && td.y < c.height();
    int step = 0;
    if (td.wasFlicked() && abs(td.distanceX()) >= FLICK_MIN) step = td.distanceX() < 0 ? 1 : -1;
    else if (td.wasClicked() && onScreen && td.x < ARROW_ZONE) step = -1;
    else if (td.wasClicked() && onScreen && td.x >= c.width() - ARROW_ZONE) step = 1;

    if (step) {
      if (list.size() > 1) {
        i = (i + step + list.size()) % list.size();
        show(list[i], now);
      }
    } else if (td.wasClicked() && onScreen) {  // tocou no Oba: fica com ele
      String err;
      if (list[i].id == inUse) break;
      changed = obaActivate(list[i].id, err);
      if (!changed) Serial.printf("[picker] %s: %s\n", list[i].id.c_str(), err.c_str());
      break;
    } else if (td.wasHold() && onScreen) {
      askRemove(c, list, i, inUse);
      lastTouch = millis();
      show(list[i], lastTouch);
      continue;
    }

    soundUpdate(now);  // um som que começou antes de abrir termina e devolve o mic
    petUpdate(now, false);
    draw(c, list, i, list[i].id == inUse, now);
    ledsShow(oba().mood(pet.mood).leds, now, now / 1000.f);
  }

  if (!changed && oba().id != inUse) obaBegin();  // fechou sem escolher: volta o de antes
  Serial.printf("[picker] fechado, ativo: %s\n", oba().id.c_str());
  return changed;
}
