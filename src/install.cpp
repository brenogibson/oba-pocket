#include "install.h"
#include <SD.h>
#include <mbedtls/base64.h>
#include <memory>
#include <vector>
#include "bubble.h"
#include "bubble_fonts.h"
#include "cloud.h"
#include "ext.h"
#include "oba.h"
#include "protocol.h"
#include "sdcard.h"
#include "ui.h"
#include "vibration.h"

static constexpr size_t SPACE_MARGIN = 64 * 1024;   // folga no cartão além do total
static constexpr uint32_t BUBBLE_WAIT_MS = 15000;   // a pergunta espera o balão fechar até isso

enum class Stage : uint8_t { Idle, Receiving, Checking };

struct Item {
  String path, sha;  // sha256 em hex minúsculo
  size_t size;
};

static Stage stage = Stage::Idle;
static String cmdId, target, label;  // label: o nome que aparece na linha de progresso
static bool activateAfter = false;
static std::vector<Item> items;      // na ordem em que chegam
static size_t total = 0, received = 0;
static size_t cur = 0, off = 0;      // o pedaço que a placa espera
static File out;
static std::unique_ptr<ObaSha256> sha;
static uint8_t* buf = nullptr;       // um pedaço decodificado (PSRAM)
static uint32_t lastChunk = 0, checkingSince = 0;
static bool checkingShown = false, blocked = false;

static String tmpDir(const String& id) { return String(OBAS_DIR) + "/.tmp-" + id; }
static String oldDir(const String& id) { return String(OBAS_DIR) + "/.old-" + id; }
static String liveDir(const String& id) { return String(OBAS_DIR) + "/" + id; }

// O exists antes evita o erro que o SD.open escreve no log quando falta
static bool removeIfThere(const String& path) { return !SD.exists(path) || obaRemoveTree(path); }

// Fecha tudo; wipe apaga a pasta temporária
static void reset(bool wipe) {
  if (out) out.close();
  sha.reset();
  free(buf);
  buf = nullptr;
  if (wipe && target.length() && !removeIfThere(tmpDir(target))) {
    Serial.printf("[install] não deu para apagar %s (sai no boot)\n", tmpDir(target).c_str());
  }
  stage = Stage::Idle;
  items.clear();
  items.shrink_to_fit();
  total = received = cur = off = 0;
  cmdId = target = label = "";
  activateAfter = false;
}

static void replyInstall(const String& id, bool ok, const char* st, const String& error = "") {
  JsonDocument x;
  x["stage"] = st;
  protoReply(id.c_str(), "oba.install", ok, error, &x);
}

// Erro que encerra a instalação: apaga a temporária e avisa quem mandou
static void fail(const String& why) {
  String id = cmdId;
  reset(true);
  replyInstall(id, false, "done", why);
}

// "next": o pedaço que a placa espera (null: já chegou tudo)
static void putNext(JsonDocument& x) {
  if (stage == Stage::Receiving) {
    x["next"]["path"] = items[cur].path;
    x["next"]["off"] = off;
  } else {
    x["next"] = nullptr;
  }
}

// 64 dígitos hex; maiúsculas viram minúsculas
static bool validSha(String& s) {
  if (s.length() != 64) return false;
  s.toLowerCase();
  for (char c : s) {
    if (!isdigit((unsigned char)c) && !(c >= 'a' && c <= 'f')) return false;
  }
  return true;
}

// O FAT tira o ponto do fim dos nomes: "a./b" viraria "a/b"
static bool fatSafe(const String& rel) {
  for (int i = rel.indexOf('.'); i >= 0; i = rel.indexOf('.', i + 1)) {
    if (i + 1 == (int)rel.length() || rel[i + 1] == '/') return false;
  }
  return true;
}

// ---------------------------------------------------------------- cabeçalho

static void startInstall(JsonDocument& cmd, uint32_t now) {
  String id = cmd["id"] | "", tgt = cmd["target"] | "";
  // Uma instalação nova cancela a que estiver em andamento
  if (stage != Stage::Idle) {
    if (id == cmdId) reset(true);
    else fail("outra instalação começou");
  }
  auto refuse = [&](const String& why) { replyInstall(id, false, "done", why); };
  if (!id.length()) return refuse("falta o id");  // só no log: sem id não tem para quem responder
  if (cloudRecording()) return refuse("desligue o REC antes");
  if (sdState() != SdState::Ready) return refuse("sem cartão");
  if (!obaValidId(tgt)) return refuse("target inválido");
  JsonArrayConst list = cmd["files"];
  if (list.size() == 0) return refuse("lista de arquivos vazia");
  if (list.size() > INSTALL_FILES_MAX) return refuse("arquivos demais (até 64)");

  std::vector<Item> got;
  size_t sum = 0;
  bool hasJson = false;
  for (JsonVariantConst f : list) {
    String path = f["path"] | "", hex = f["sha256"] | "";
    JsonVariantConst size = f["size"];
    if (!obaValidFile(path) || !obaValidPath(tgt + "/" + path) || !fatSafe(path)) {
      return refuse("caminho inválido: " + path);
    }
    if (!size.is<uint32_t>() || !size.as<uint32_t>()) return refuse("tamanho inválido: " + path);
    size_t n = size.as<uint32_t>();
    bool json = path == "oba.json";
    if (json && n > OBA_JSON_MAX) return refuse("oba.json grande demais (até 64 KB)");
    if (n > INSTALL_FILE_MAX) return refuse(path + ": grande demais (até 2 MB)");
    if (!validSha(hex)) return refuse("sha256 inválido: " + path);
    // O FAT não separa maiúsculas de minúsculas
    String low = path;
    low.toLowerCase();
    for (const Item& o : got) {
      String other = o.path;
      other.toLowerCase();
      if (other == low) return refuse("arquivo repetido: " + path);
      if (low.startsWith(other + "/") || other.startsWith(low + "/")) return refuse("caminhos em conflito: " + path);
    }
    hasJson |= json;
    sum += n;
    got.push_back({path, hex, n});
  }
  if (!hasJson) return refuse("falta o oba.json");
  if (sum > INSTALL_TOTAL_MAX) return refuse("grande demais (até 6 MB no total)");

  String tmp = tmpDir(tgt);
  if (!removeIfThere(tmp)) return refuse("não deu para apagar a pasta temporária");
  uint64_t room = SD.totalBytes() - SD.usedBytes();
  if (room < sum + SPACE_MARGIN) return refuse("pouco espaço no cartão");
  if ((!SD.exists(OBAS_DIR) && !SD.mkdir(OBAS_DIR)) || !SD.mkdir(tmp)) return refuse("erro de escrita no cartão");
  buf = (uint8_t*)ps_malloc(INSTALL_CHUNK_MAX + 4);  // folga: o base64 diz se passou de 8 KB
  if (!buf) {
    removeIfThere(tmp);
    return refuse("sem memória");
  }

  label = "";
  for (const ObaInfo& o : obaList()) {
    if (o.id == tgt) label = o.name;
  }
  if (!label.length()) {
    label = tgt;
    label[0] = toupper((unsigned char)label[0]);
  }
  cmdId = id;
  target = tgt;
  activateAfter = cmd["activate"] | false;
  items = std::move(got);
  total = sum;
  received = cur = off = 0;
  stage = Stage::Receiving;
  lastChunk = now;  // o now do quadro: com millis() aqui, o installUpdate do mesmo quadro daria a volta
  Serial.printf("[install] %s: recebendo %s (%u arquivos, %u KB)\n", id.c_str(), tgt.c_str(), (unsigned)items.size(),
                (unsigned)((total + 1023) / 1024));
  JsonDocument x;
  x["stage"] = "receiving";
  putNext(x);
  protoReply(id.c_str(), "oba.install", true, "", &x);
}

// ---------------------------------------------------------------- pedaços

// Abre o arquivo na pasta temporária, criando as subpastas
static bool openItem(const Item& it) {
  String base = tmpDir(target);
  for (int i = it.path.indexOf('/'); i > 0; i = it.path.indexOf('/', i + 1)) {
    String dir = base + "/" + it.path.substring(0, i);
    if (!SD.exists(dir) && !SD.mkdir(dir)) return false;
  }
  out = SD.open(base + "/" + it.path, FILE_WRITE);
  if (!out) return false;
  sha.reset(new ObaSha256());
  return true;
}

// Grava o pedaço esperado. false = erro fatal (a instalação acabou).
static bool writeChunk(JsonVariantConst data, uint32_t now) {
  if (!data.is<const char*>()) return fail("base64 inválido"), false;
  JsonString b64 = data.as<JsonString>();
  size_t n = 0;
  int r = mbedtls_base64_decode(buf, INSTALL_CHUNK_MAX + 4, &n, (const uint8_t*)b64.c_str(), b64.size());
  if (r == MBEDTLS_ERR_BASE64_BUFFER_TOO_SMALL || (!r && n > INSTALL_CHUNK_MAX)) {
    return fail("pedaço grande demais (até 8 KB)"), false;
  }
  if (r) return fail("base64 inválido"), false;
  const Item& it = items[cur];
  if (off + n > it.size) return fail("pedaço passando do tamanho do arquivo: " + it.path), false;
  if (!n) return true;
  if (!off && !openItem(it)) return fail("erro de escrita no cartão"), false;
  if (out.write(buf, n) != n) return fail("erro de escrita no cartão"), false;
  sha->update(buf, n);
  off += n;
  received += n;
  lastChunk = now;
  if (off < it.size) return true;

  // Fim do arquivo: o sha256 foi calculado no caminho, sem reler
  out.close();
  bool match = sha->hex() == it.sha;
  sha.reset();
  if (!match) return fail("sha256 não bate: " + it.path), false;
  off = 0;
  if (++cur < items.size()) return true;
  stage = Stage::Checking;
  checkingSince = now;
  checkingShown = false;
  free(buf);
  buf = nullptr;
  Serial.printf("[install] %s: chegou tudo, conferindo %s\n", cmdId.c_str(), target.c_str());
  return true;
}

static void takeChunk(JsonDocument& cmd, uint32_t now) {
  String id = cmd["id"] | "";
  if (stage == Stage::Idle || id != cmdId) {
    protoReply(id.c_str(), "oba.chunk", false, "nenhuma instalação com esse id");
    return;
  }
  // Só o pedaço esperado; repetido ou fora de ordem só ganha o ack com o next
  JsonVariantConst o = cmd["off"];
  bool expected = stage == Stage::Receiving && items[cur].path == (cmd["path"] | "") && o.is<uint32_t>() &&
                  o.as<uint32_t>() == off;
  if (expected && !writeChunk(cmd["data"], now)) return;
  JsonDocument x;
  putNext(x);
  protoReply(id.c_str(), "oba.chunk", true, "", &x);
}

// ---------------------------------------------------------------- conferência e troca

// files do oba.json = a lista do cabeçalho sem o oba.json, com os mesmos sha256
static bool sameFiles(const ObaSpec& s, String& err) {
  size_t n = 0;
  for (const Item& it : items) {
    if (it.path == "oba.json") continue;
    ++n;
    const String& want = s.fileSha(it.path);
    if (!want.length()) return err = "files do oba.json não bate com a lista: " + it.path, false;
    if (want != it.sha) return err = "sha256 não bate: " + it.path, false;
  }
  if (s.files.size() == n) return true;
  for (const auto& f : s.files) {
    bool listed = false;
    for (const Item& it : items) listed |= it.path == f.first;
    if (!listed) return err = "files do oba.json não bate com a lista: " + f.first, false;
  }
  return err = "files do oba.json não bate com a lista", false;
}

static String needs(const std::vector<String>& req) {
  static const char* const LABELS[][2] = {
      {"display", "tela"},      {"touch", "toque"},     {"buttons", "botões"},
      {"imu", "movimento"},     {"mic.level", "nível do som"}, {"mic.transcribe", "transcrição da fala"},
      {"leds", "LEDs"},         {"vibration", "vibração"},     {"battery", "bateria"},
      {"speaker", "alto-falante"}, {"sd", "cartão"},
  };
  String out;
  for (const String& r : req) {
    String name = r;
    for (const auto& l : LABELS) {
      if (r == l[0]) name = l[1];
    }
    out += (out.length() ? ", " : "") + name;
  }
  return "Pede: " + (out.length() ? out : String("nada"));
}

// /obas/<id> vira .old-<id>, a temporária vira /obas/<id> e a .old sai.
// Se faltar luz no meio, o installRecover do boot termina ou desfaz.
static bool swapDirs() {
  String tmp = tmpDir(target), old = oldDir(target), dst = liveDir(target);
  if (!removeIfThere(old)) return false;
  bool had = SD.exists(dst);
  if (had && !SD.rename(dst, old)) return false;
  if (!SD.rename(tmp, dst)) {
    if (had && !SD.rename(old, dst)) Serial.printf("[install] %s ficou em %s (volta no boot)\n", dst.c_str(), old.c_str());
    return false;
  }
  if (had && !obaRemoveTree(old)) Serial.printf("[install] não deu para apagar %s (sai no boot)\n", old.c_str());
  return true;
}

static bool finish(M5Canvas& c) {
  vibrationStop();  // a conferência e a pergunta seguram o loop, que é quem anda com a vibração
  String err;
  ObaSpec* spec = obaLoadDir(tmpDir(target), target, err);
  if (spec && !sameFiles(*spec, err)) {
    delete spec;
    spec = nullptr;
  }
  if (!spec) return fail(err), false;

  bool update = false;
  String before;
  for (const ObaInfo& o : obaList()) {
    if (o.id == target && o.onCard) update = true, before = o.version;
  }
  String title = String(update ? "Atualizar " : "Instalar ") + spec->name + "?";
  String version = "versão " + spec->version;
  if (update && before == spec->version) version += " (a mesma)";
  else if (update) version = "versão " + before + " » " + spec->version;
  String body = version + "\n" + needs(spec->requires) + "\n" + String((unsigned)((total + 1023) / 1024)) + " KB";

  replyInstall(cmdId, true, "confirm");
  Serial.printf("[install] %s: %s %s conferido, perguntando na tela\n", cmdId.c_str(), target.c_str(),
                spec->version.c_str());
  if (!uiConfirm(c, title.c_str(), body.c_str(), "Instalar", "Recusar", INSTALL_ASK_MS)) {
    delete spec;
    fail("recusado na placa");
    return false;
  }
  if (!swapDirs()) {
    delete spec;
    fail("não deu para trocar a pasta");
    return false;
  }
  // O Oba de teste já está carregado: se for o ativo (ou pediram), ele entra direto
  bool use = oba().id == target || (activateAfter && !cloudRecording());
  String id = cmdId;
  Serial.printf("[install] %s: %s instalado%s\n", id.c_str(), target.c_str(), use ? " e ativo" : "");
  reset(false);
  if (use) {
    obaUse(spec, true);
  } else {
    delete spec;
    protoObasChanged();
  }
  JsonDocument x;
  x["stage"] = "done";
  x["active"] = use;
  protoReply(id.c_str(), "oba.install", true, "", &x);
  return use;
}

// ---------------------------------------------------------------- API

void installRecover() {
  if (stage != Stage::Idle || sdState() != SdState::Ready || !SD.exists(OBAS_DIR)) return;
  File root = SD.open(OBAS_DIR);
  if (!root) return;
  std::vector<String> names;
  for (File f = root.openNextFile(); f; f = root.openNextFile()) {
    String n = f.name();
    f.close();
    if (n.startsWith(".tmp-") || n.startsWith(".old-")) names.push_back(n);
  }
  root.close();
  for (const String& n : names) {
    String full = String(OBAS_DIR) + "/" + n, id = n.substring(5);
    if (n.startsWith(".tmp-")) {
      Serial.printf("[install] sobra de instalação: %s %s\n", full.c_str(), obaRemoveTree(full) ? "apagada" : "não saiu");
    } else if (!obaValidId(id)) {
      Serial.printf("[install] %s: nome estranho, fica como está\n", full.c_str());
    } else if (SD.exists(liveDir(id))) {
      Serial.printf("[install] troca terminada: %s %s\n", full.c_str(), obaRemoveTree(full) ? "apagada" : "não saiu");
    } else {
      bool ok = SD.rename(full, liveDir(id));
      Serial.printf("[install] troca pela metade: %s %s\n", id.c_str(), ok ? "voltou" : "não voltou");
    }
  }
}

void installCommand(JsonDocument& cmd, uint32_t now) {
  const char* type = cmd["type"] | "";
  if (!strcmp(type, "oba.install")) startInstall(cmd, now);
  else if (!strcmp(type, "oba.chunk")) takeChunk(cmd, now);
}

bool installUpdate(M5Canvas& c, uint32_t now) {
  blocked = false;
  if (stage == Stage::Idle) return false;
  if (cloudRecording()) return fail("o REC ligou"), false;
  if (stage == Stage::Receiving) {
    if (now - lastChunk > INSTALL_IDLE_MS) fail("tempo esgotado");
    return false;
  }
  // Conferindo: antes de segurar o loop, um quadro com "Conferindo…" na tela
  // e o balão fechado (ou já esperou demais por ele). Com um pedido de aprovação
  // na tela, espera sempre: a pergunta abriria por cima e o Instalar cairia
  // onde estava o Aprovar
  if (!checkingShown || extShowing() || (!bubbleIdle() && now - checkingSince < BUBBLE_WAIT_MS)) return false;
  blocked = true;
  return finish(c);
}

void installDrawStatus(M5Canvas& c) {
  if (stage == Stage::Idle) return;
  String what = "Conferindo ", pct;
  if (stage == Stage::Receiving) {
    what = "Recebendo ";
    pct = " " + String((unsigned)((uint64_t)received * 100 / total)) + "%";
  }
  const auto& color = oba().look.color;
  c.loadFont(BUBBLE_FONT_SMALL);
  static constexpr int H = 22, PAD = 12;
  String text = what + label + "…" + pct;
  if (c.textWidth(text) + 2 * PAD > c.width() - 16) text = what.substring(0, what.length() - 1) + "…" + pct;
  int w = c.textWidth(text) + 2 * PAD, y = c.height() - H - 6;
  c.fillRoundRect((c.width() - w) / 2, y, w, H, H / 2, color.shadow);
  c.setTextColor(color.text);
  c.setTextDatum(middle_center);
  c.drawString(text, c.width() / 2, y + H / 2);
  c.unloadFont();
  if (stage == Stage::Checking) checkingShown = true;
}

bool installBlocked() { return blocked; }
