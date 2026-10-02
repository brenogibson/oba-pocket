#include "cloud.h"
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include <vector>
#include <mutex>
#include <mbedtls/base64.h>
#include "audio.h"
#include "transcribe.h"
// Os dois ficam fora do git: sem eles, um erro que diz o que fazer
#if !__has_include("secrets.h")
#error "falta src/secrets.h: copie src/secrets.h.example para src/secrets.h, preencha o WiFi e rode python3 setup.py (ele gera o certificado e o src/aws_config.h)"
#endif
#if !__has_include("aws_config.h")
#error "falta src/aws_config.h: rode python3 setup.py (ele gera o arquivo a partir do config.json)"
#endif
#include "aws_config.h"
#include "amazon_root_ca.h"
#include "secrets.h"

// Tópicos do Oba Protocol v1: <prefixo>/<placa>/<canal>
#define TOPIC(ch) OBA_PREFIX "/" IOT_THING_NAME "/" ch
static const char* const TOPIC_STATE = TOPIC("state");
static const char* const TOPIC_EVT = TOPIC("evt");
static const char* const TOPIC_TRANSCRIPT = TOPIC("transcript");
static const char* const TOPIC_REPLY = TOPIC("reply");
static const char* const TOPIC_CMD = TOPIC("cmd");
// Fontes externas: cada uma publica em ext/<fonte> e ouve a resposta em ext/<fonte>/re
static const char* const TOPIC_EXT = TOPIC("ext/");
static const char* const TOPIC_EXT_ALL = TOPIC("ext/+");
static constexpr size_t EXT_SRC_MAX = 64, EXT_JSON_MAX = 4096;
// Última vontade: o broker publica quando a placa cai
static const char* const STATE_OFFLINE = "{\"v\":1,\"type\":\"state\",\"online\":false}";

// Oba ativo: o id vai nas mensagens; as palavras de ativação são o nome e as
// grafias que o Transcribe em pt-BR costuma devolver para ele. Compara palavra
// inteira: o nome no meio de outra palavra não conta. O loop troca junto com o
// Oba; a task de rede lê.
static std::mutex obaLock;
static std::vector<String> wakeWords;
static String obaId;

static constexpr int CHUNKS_PER_EVENT = 3;              // 3 x 32 ms ~ 100 ms por AudioEvent
static constexpr uint32_t PARTIAL_PUBLISH_MS = 250;     // parciais para a tela, no máximo 4/s
static constexpr uint32_t CREDS_MARGIN_S = 300;         // renova credenciais 5 min antes
static constexpr uint32_t WS_CONNECT_TIMEOUT_MS = 15000;
static constexpr int AUDIO_GAIN = 3;                    // o mic do Core2 é baixo para fala a 1 m
// Balão com ícone em base64 chega a ~8 KB; um pedaço de oba.chunk (8 KB em
// base64 + JSON + tópico) a ~11 KB. O pacote inteiro precisa caber.
static constexpr size_t MQTT_BUFFER = 16384;
static constexpr size_t MAX_RULES = 8;                  // regras armadas ao mesmo tempo
static constexpr size_t MAX_WORDS = 8;                  // palavras de uma regra de fala
static constexpr size_t MAX_DO = 4;                     // comandos de uma regra
static constexpr uint32_t TTL_DEFAULT_S = 900, TTL_MAX_S = 86400;
static constexpr uint32_t FIRE_GAP_MS = 6000;           // intervalo mínimo entre duas regras de fala
static constexpr uint32_t DUE_MAX_MS = 20000;           // regra que casou espera a tela livre até isso

static volatile CloudState state = CloudState::Offline;
static volatile bool recording = false;  // pedido pelo loop (toque na tela) ou desligado pelo harness
static volatile bool online = false;     // MQTT conectado
static volatile bool stateRequested = false;
static bool streamOn = false;            // o que a task de rede aplicou
static uint32_t keywordHits = 0;  // escrito pela task de rede, lido pelo loop

// Regras armadas pelo agente: "quando X, faça Y". X é fala (uma das palavras
// aparece na transcrição, parcial ou final) ou um evento da placa. Y já chega
// pronto: os balões decodificados e os outros comandos em JSON, para o loop.
// Assim a regra dispara na hora, sem esperar a nuvem. Só a task de rede mexe
// nelas; os balões vão para a animação pela fila.
struct Action {
  Bubble* bubble = nullptr;  // speak
  String cmd;                // os outros: o comando inteiro, para o loop
};
struct Rule {
  String id, on;                 // on: "speech" ou o type do evento
  std::vector<String> words;     // speech: minúsculas, sem acento (como foldText)
  std::vector<Action> actions;
  uint32_t expiresAt;
  uint32_t dueAt = 0;            // casou e está esperando a tela ficar livre
  String dueWord, dueText;
};
static std::vector<Rule> rules;
// Eventos da placa que podem armar regras
static const char* const RULE_EVENTS[] = {"touch.tap", "button.a", "button.c", "sound.loud", "imu.shake", "imu.tap"};
// Comandos que o loop executa; os cinco primeiros também valem dentro de uma regra
static const char* const LOOP_CMDS[] = {"react",        "look",       "vibrate",     "leds",     "play", "read",
                                        "oba.activate", "oba.remove", "oba.install", "oba.chunk"};
static constexpr int RULE_CMDS = 5;

// O que o loop manda publicar
struct Outgoing {
  Channel ch;
  bool publish;
  String evt, json;
  String to;  // Ext: a fonte que recebe
};

static QueueHandle_t bubbleQueue, cmdQueue, extQueue, outbox;
static volatile bool bubbleBusy = false;
static uint32_t lastFire = 0;
static String* volatile simulatedLine = nullptr;  // frase de teste vinda da serial

static AwsCreds creds;
static WiFiClientSecure mqttNet;
static PubSubClient mqtt(mqttNet);
static WebSocketsClient ws;

static bool wsOpen = false, wsStarting = false;
static uint32_t wsStartedAt = 0, nextWsTry = 0, nextMqttTry = 0;

static int16_t pcm[AUDIO_CHUNK * CHUNKS_PER_EVENT];
static int pcmChunks = 0;
static uint8_t* eventBuf;

// Resultado em andamento (o Transcribe reenvia o mesmo ResultId até fechar a frase)
static String currentId;
static int currentHits = 0;
static uint32_t lastPartialPublish = 0;

// Diagnóstico: o que entra e sai do stream, impresso a cada 5 s
static uint32_t statTx = 0, statRx = 0, statBad = 0, statPeak = 0, statDrop = 0, nextStat = 0;

// ---------------------------------------------------------------- helpers

uint64_t cloudEpochMs() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  if (tv.tv_sec < 1700000000) return 0;  // o NTP ainda não respondeu
  return (uint64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

// Quantas palavras de ativação o texto tem; a primeira vai em *first
static int countKeyword(const String& text, String* first) {
  std::lock_guard<std::mutex> lock(obaLock);
  int n = 0;
  String word;
  for (size_t i = 0; i <= text.length(); ++i) {
    char c = i < text.length() ? text[i] : ' ';
    if (isalnum((unsigned char)c)) {
      word += (char)tolower((unsigned char)c);
      continue;
    }
    for (const String& k : wakeWords) {
      if (word != k) continue;
      if (!n++) *first = k;
    }
    word = "";
  }
  return n;
}

// Começo de toda mensagem: {"v": 1, "type", "ts", "oba"}
static void envelope(JsonDocument& doc, const char* type) {
  doc["v"] = 1;
  doc["type"] = type;
  doc["ts"] = cloudEpochMs();
  std::lock_guard<std::mutex> lock(obaLock);
  doc["oba"] = obaId;
}

static void publish(const char* topic, JsonDocument& doc) {
  if (!mqtt.connected()) return;
  String out;
  serializeJson(doc, out);
  mqtt.publish(topic, out.c_str());
}

// Comando recusado: só responde se ele tinha id
static void replyError(const char* id, const char* re, const char* error) {
  Serial.printf("[cloud] %s recusado: %s\n", re, error);
  if (!*id) return;
  JsonDocument r;
  envelope(r, "reply");
  r["id"] = id;
  r["re"] = re;
  r["ok"] = false;
  r["error"] = error;
  publish(TOPIC_REPLY, r);
}

static bool isOneOf(const char* s, const char* const* list, int n) {
  for (int i = 0; i < n; ++i) if (!strcmp(s, list[i])) return true;
  return false;
}

// Texto em minúsculas, sem acento e só com letras, números e espaços, entre
// espaços (" voce viu o sqs "), para comparar palavras inteiras com os gatilhos.
// Igual ao norm() do roteador (harness/router/icons.py).
static String foldText(const String& text) {
  // Latin-1 de U+00C0 a U+00FF (maiúsculas e minúsculas caem no mesmo índice)
  static const char FOLD[] = "aaaaaaaceeeeiiiidnooooo ouuuuyty";
  String out = " ";
  bool space = true;
  for (size_t i = 0; i < text.length(); ++i) {
    uint8_t c = text[i];
    char f = ' ';
    if (isalnum(c)) {
      f = tolower(c);
    } else if (c == '-') {
      continue;  // "e-mail" = "email"
    } else if (c == 0xC3 && i + 1 < text.length()) {
      f = FOLD[((uint8_t)text[++i] - 0x80) & 0x1F];
    } else if (c >= 0x80) {
      while (i + 1 < text.length() && ((uint8_t)text[i + 1] & 0xC0) == 0x80) ++i;  // pula o resto do caractere
    }
    if (f == ' ' && space) continue;
    out += f;
    space = f == ' ';
  }
  if (!space) out += ' ';
  return out;
}

static void showBubble(Bubble* b) {
  if (xQueueSend(bubbleQueue, &b, 0) != pdTRUE) delete b;  // fila cheia: esse fica de fora
}

static Bubble* parseBubble(JsonObjectConst j, const char* id) {
  auto* b = new Bubble();
  const char* kind = j["kind"] | "text";
  b->kind = !strcmp(kind, "image") ? BubbleKind::Image : !strcmp(kind, "qr") ? BubbleKind::Qr : BubbleKind::Text;
  b->id = id;
  b->text = j["text"] | "";
  b->url = j["url"] | "";
  const char* png = j["png"] | "";
  size_t n = strlen(png);
  if (b->kind == BubbleKind::Image && n) {
    size_t cap = n / 4 * 3 + 3;
    b->png = (uint8_t*)ps_malloc(cap);
    if (!b->png || mbedtls_base64_decode(b->png, cap, &b->pngLen, (const uint8_t*)png, n) != 0) {
      free(b->png);
      b->png = nullptr;
      b->pngLen = 0;
    }
  }
  if ((b->kind == BubbleKind::Image && !b->png) || (b->kind == BubbleKind::Qr && !b->url.length())) {
    b->kind = BubbleKind::Text;
  }
  return b;
}

static void forward(const String& json) {
  String* cmd = new String(json);
  if (xQueueSend(cmdQueue, &cmd, 0) != pdTRUE) delete cmd;  // o loop está atrasado: esse fica de fora
}

static void dropRule(size_t i) {
  for (Action& a : rules[i].actions) delete a.bubble;
  rules.erase(rules.begin() + i);
}

// all = false: só as de fala (a transcrição que elas esperavam acabou)
static void clearRules(bool all) {
  for (size_t i = rules.size(); i-- > 0;) {
    if (all || rules[i].on == "speech") dropRule(i);
  }
}

static int findRule(const char* id) {
  for (size_t i = 0; i < rules.size(); ++i) if (rules[i].id == id) return i;
  return -1;
}

static void armRule(JsonObjectConst doc, const char* id) {
  String on = doc["on"] | "speech";
  bool speech = on == "speech";
  if (!*id) return replyError(id, "arm", "falta o id");
  if (speech && !streamOn) return replyError(id, "arm", "o REC está desligado");  // sobra de uma reunião que já acabou
  if (!speech && !isOneOf(on.c_str(), RULE_EVENTS, sizeof RULE_EVENTS / sizeof *RULE_EVENTS)) {
    return replyError(id, "arm", "evento desconhecido");
  }
  Rule r;
  r.id = id;
  r.on = on;
  for (JsonVariantConst w : doc["words"].as<JsonArrayConst>()) {
    String f = foldText(w.as<String>());
    if (f.length() > 2 && r.words.size() < MAX_WORDS) r.words.push_back(f);
  }
  if (speech && r.words.empty()) return replyError(id, "arm", "sem palavras");
  for (JsonObjectConst a : doc["do"].as<JsonArrayConst>()) {
    if (r.actions.size() >= MAX_DO) break;
    const char* type = a["type"] | "";
    Action act;
    if (!strcmp(type, "speak")) {
      act.bubble = parseBubble(a["bubble"], id);
    } else if (isOneOf(type, LOOP_CMDS, RULE_CMDS)) {
      serializeJson(a, act.cmd);
    } else {
      continue;
    }
    r.actions.push_back(std::move(act));
  }
  if (r.actions.empty()) return replyError(id, "arm", "sem comandos em do");
  uint32_t ttl = doc["ttl_s"] | TTL_DEFAULT_S;
  r.expiresAt = millis() + min(max(ttl, 1u), TTL_MAX_S) * 1000UL;

  int i = findRule(id);
  if (i >= 0) dropRule(i);
  if (rules.size() >= MAX_RULES) dropRule(0);  // sai a mais antiga
  Serial.printf("[cloud] regra %s: %s (%u palavras, %u comandos)\n", id, on.c_str(), (unsigned)r.words.size(),
                (unsigned)r.actions.size());
  rules.push_back(std::move(r));
}

// Chaves que os comandos usam; "card" fica de fora (é só para as telas).
// "files" (a lista do oba.install) passa inteira.
static const char* const CMD_KEYS[] = {"v",     "type",  "id",    "bubble", "on",      "words", "ttl_s",
                                       "do",    "session", "x",   "y",      "ms",      "level", "fx",
                                       "color", "trail", "min",   "max",    "speed",   "hold_ms", "what",
                                       "target", "files", "activate", "path", "off",   "data",  "sound",
                                       "volume"};

// Mensagem de uma fonte externa (ext/<fonte>): só confere o tamanho e o nome e
// entrega ao loop (ext.cpp), que lê o resto
static void onExt(const char* src, uint8_t* payload, unsigned int len) {
  size_t n = strlen(src);
  bool ok = n > 0 && n <= EXT_SRC_MAX && len <= EXT_JSON_MAX;
  for (size_t i = 0; ok && i < n; i++) ok = isalnum((unsigned char)src[i]) || strchr(":_-", src[i]);
  if (!ok) {
    Serial.printf("[cloud] ext: fonte ou mensagem inválida (%u bytes)\n", len);
    return;
  }
  ExtMessage* m = new ExtMessage{src, String((const char*)payload, len)};
  if (xQueueSend(extQueue, &m, 0) != pdTRUE) delete m;  // o loop está atrasado: essa fica de fora
}

// Comandos do harness (TOPIC_CMD). Roda dentro de mqtt.loop(), na task de rede.
static void onCommand(char* topic, uint8_t* payload, unsigned int len) {
  size_t extLen = strlen(TOPIC_EXT);
  if (!strncmp(topic, TOPIC_EXT, extLen)) return onExt(topic + extLen, payload, len);
  JsonDocument filter;
  for (const char* k : CMD_KEYS) filter[k] = true;
  JsonDocument doc;
  if (deserializeJson(doc, payload, len, DeserializationOption::Filter(filter))) {
    Serial.printf("[cloud] comando inválido (%u bytes)\n", len);
    return;
  }
  const char* type = doc["type"] | "";
  const char* id = doc["id"] | "";
  if ((doc["v"] | 1) != 1) return replyError(id, type, "versão do protocolo desconhecida");

  if (!strcmp(type, "speak")) {
    Serial.printf("[cloud] agente fala: %s\n", doc["bubble"]["text"] | "");
    showBubble(parseBubble(doc["bubble"], id));
  } else if (!strcmp(type, "arm")) {
    armRule(doc.as<JsonObjectConst>(), id);
  } else if (!strcmp(type, "disarm")) {
    int i = findRule(id);
    if (i >= 0) dropRule(i);
  } else if (!strcmp(type, "reset")) {
    clearRules(true);
    Serial.printf("[cloud] sessão: %s\n", doc["session"] | "(nenhuma)");
  } else if (!strcmp(type, "rec")) {
    if (doc["on"].is<bool>() && !doc["on"].as<bool>()) recording = false;
    else replyError(id, type, "o REC só liga pelo botão na placa");
  } else if (!strcmp(type, "state")) {
    stateRequested = true;
  } else if (isOneOf(type, LOOP_CMDS, sizeof LOOP_CMDS / sizeof *LOOP_CMDS)) {
    String json;
    serializeJson(doc, json);
    forward(json);
  } else {
    replyError(id, type, "comando desconhecido");
  }
}

// Dispara a regra: avisa o harness e executa o que ela manda
static void fire(size_t i, const String& trigger, const String& text) {
  Rule& r = rules[i];
  JsonDocument ev;
  envelope(ev, "rule.fired");
  ev["id"] = r.id;
  ev["on"] = r.on;
  if (trigger.length()) ev["trigger"] = trigger;
  if (text.length()) ev["text"] = text;
  publish(TOPIC_EVT, ev);
  Serial.printf("[cloud] >>> regra %s (%s)\n", r.id.c_str(), trigger.length() ? trigger.c_str() : r.on.c_str());
  for (Action& a : r.actions) {
    if (a.bubble) showBubble(a.bubble);
    else forward(a.cmd);
    a.bubble = nullptr;
  }
  dropRule(i);
}

// Alguém falou de um assunto que uma regra espera: marca a regra para disparar
static void matchSpeech(const String& text) {
  if (rules.empty()) return;
  String folded = foldText(text);
  for (Rule& r : rules) {
    if (r.dueAt || r.on != "speech") continue;
    for (const String& w : r.words) {
      if (folded.indexOf(w) < 0) continue;
      r.dueAt = millis();
      r.dueWord = w;
      r.dueWord.trim();
      r.dueText = text;
      Serial.printf("[cloud] >>> \"%s\" para a regra %s\n", r.dueWord.c_str(), r.id.c_str());
      break;
    }
  }
}

// Evento da placa: as regras dele disparam na hora
static void matchEvent(const String& type) {
  for (size_t i = rules.size(); i-- > 0;) {
    if (rules[i].on == type) fire(i, "", "");
  }
}

// Regras de fala: no máximo uma por vez, com a tela livre e um respiro entre elas
static void serviceRules() {
  uint32_t now = millis();
  for (size_t i = 0; i < rules.size();) {
    if ((int32_t)(now - rules[i].expiresAt) >= 0) { dropRule(i); continue; }
    if (rules[i].dueAt && now - rules[i].dueAt > DUE_MAX_MS) rules[i].dueAt = 0;  // passou a hora
    ++i;
  }
  if (bubbleBusy || uxQueueMessagesWaiting(bubbleQueue) || now - lastFire < FIRE_GAP_MS) return;
  for (size_t i = 0; i < rules.size(); ++i) {
    if (!rules[i].dueAt) continue;
    String word = rules[i].dueWord, text = rules[i].dueText;
    fire(i, word, text);
    lastFire = now;
    return;
  }
}

// O que o loop mandou publicar. Os eventos passam antes pelas regras.
static void drainOutbox() {
  static const char* const TOPICS[] = {TOPIC_EVT, TOPIC_REPLY, TOPIC_STATE};
  Outgoing* m;
  while (xQueueReceive(outbox, &m, 0) == pdTRUE) {
    if (m->ch == Channel::Evt && m->evt.length()) matchEvent(m->evt);
    if (m->publish && mqtt.connected()) {
      if (m->ch == Channel::Ext) mqtt.publish((TOPIC_EXT + m->to + "/re").c_str(), m->json.c_str());
      else mqtt.publish(TOPICS[(int)m->ch], m->json.c_str(), m->ch == Channel::State);
    }
    delete m;
  }
}

// ---------------------------------------------------------------- AWS

static bool fetchCreds() {
  WiFiClientSecure net;
  net.setCACert(AMAZON_ROOT_CA1);
  net.setCertificate(DEVICE_CERT);
  net.setPrivateKey(DEVICE_KEY);
  HTTPClient http;
  http.begin(net, "https://" IOT_CRED_ENDPOINT "/role-aliases/" IOT_ROLE_ALIAS "/credentials");
  http.addHeader("x-amzn-iot-thingname", IOT_THING_NAME);
  int code = http.GET();
  if (code != 200) {
    Serial.printf("[cloud] credenciais: HTTP %d %s\n", code, http.getString().c_str());
    http.end();
    return false;
  }
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getString());
  http.end();
  if (err) return false;

  JsonObject c = doc["credentials"];
  creds.accessKeyId = c["accessKeyId"].as<String>();
  creds.secretAccessKey = c["secretAccessKey"].as<String>();
  creds.sessionToken = c["sessionToken"].as<String>();
  struct tm t = {};
  sscanf(c["expiration"] | "", "%d-%d-%dT%d:%d:%d", &t.tm_year, &t.tm_mon, &t.tm_mday,
         &t.tm_hour, &t.tm_min, &t.tm_sec);
  t.tm_year -= 1900;
  t.tm_mon -= 1;
  creds.expiration = mktime(&t);  // TZ é UTC (configTime com offset 0)
  Serial.printf("[cloud] credenciais ok, expiram em %ld s\n", (long)(creds.expiration - time(nullptr)));
  return true;
}

static void connectMqtt() {
  Serial.printf("[cloud] conectando MQTT (estado %d)...\n", mqtt.state());  // -4 keepalive, -3 caiu
  if (mqtt.connect(IOT_THING_NAME, TOPIC_STATE, 1, true, STATE_OFFLINE)) {
    Serial.printf("[cloud] MQTT ok (%s)\n", OBA_PREFIX "/" IOT_THING_NAME);
    stateRequested = true;  // o loop monta o state, que substitui o da última vontade
    if (!mqtt.subscribe(TOPIC_CMD)) Serial.print("[cloud] não consegui assinar os comandos\n");
    if (!mqtt.subscribe(TOPIC_EXT_ALL, 1)) Serial.print("[cloud] não consegui assinar as fontes externas\n");
  } else {
    Serial.printf("[cloud] MQTT falhou (%d)\n", mqtt.state());
    nextMqttTry = millis() + 5000;
  }
}

static void handleTranscript(const char* json, size_t len) {
  JsonDocument filter;
  JsonObject fr = filter["Transcript"]["Results"].add<JsonObject>();
  fr["ResultId"] = true;
  fr["IsPartial"] = true;
  fr["Alternatives"][0]["Transcript"] = true;

  JsonDocument doc;
  if (deserializeJson(doc, json, len, DeserializationOption::Filter(filter))) return;

  for (JsonObject r : doc["Transcript"]["Results"].as<JsonArray>()) {
    String id = r["ResultId"] | "";
    bool partial = r["IsPartial"] | false;
    String text = r["Alternatives"][0]["Transcript"] | "";
    uint32_t now = millis();

    if (id != currentId) {
      currentId = id;
      currentHits = 0;
    }
    String word;
    int hits = countKeyword(text, &word);
    if (hits > currentHits) {
      __atomic_fetch_add(&keywordHits, hits - currentHits, __ATOMIC_RELAXED);
      currentHits = hits;
      Serial.printf("[cloud] >>> %s! (%s) \"%s\"\n", word.c_str(), partial ? "parcial" : "final", text.c_str());
      JsonDocument ev;
      envelope(ev, "wake");
      ev["word"] = word;
      ev["text"] = text;
      ev["partial"] = partial;
      publish(TOPIC_EVT, ev);
    }

    matchSpeech(text);

    if (!partial) Serial.printf("[cloud] final: %s\n", text.c_str());
    else if (now - lastPartialPublish >= PARTIAL_PUBLISH_MS) Serial.printf("[cloud] ~ %s\n", text.c_str());
    if (partial && now - lastPartialPublish < PARTIAL_PUBLISH_MS) continue;
    lastPartialPublish = now;
    JsonDocument out;
    envelope(out, "transcript");
    out["id"] = id;
    out["partial"] = partial;
    out["text"] = text;
    publish(TOPIC_TRANSCRIPT, out);
  }
}

static void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      Serial.print("[cloud] Transcribe conectado, transmitindo áudio\n");
      wsOpen = true;
      wsStarting = false;
      pcmChunks = 0;
      audioFlush();
      break;
    case WStype_DISCONNECTED:
      if (wsOpen || wsStarting) Serial.print("[cloud] Transcribe desconectado\n");
      wsOpen = wsStarting = false;
      nextWsTry = millis() + 2000;
      break;
    case WStype_BIN: {
      String msgType, evType;
      const char* body;
      size_t bodyLen;
      ++statRx;
      if (!transcribeDecode(payload, length, msgType, evType, &body, &bodyLen)) {
        ++statBad;
        Serial.printf("[cloud] mensagem inválida (%u bytes)\n", (unsigned)length);
        break;
      }
      if (msgType == "event" && evType == "TranscriptEvent") {
        handleTranscript(body, bodyLen);
      } else {
        Serial.printf("[cloud] Transcribe %s %s: %.*s\n", msgType.c_str(), evType.c_str(),
                      (int)bodyLen, body);
      }
      break;
    }
    case WStype_TEXT:
      Serial.printf("[cloud] Transcribe texto: %.*s\n", (int)length, (const char*)payload);
      break;
    case WStype_ERROR:
      Serial.printf("[cloud] Transcribe erro WS: %.*s\n", (int)length, (const char*)payload);
      break;
    case WStype_FRAGMENT_BIN_START:
    case WStype_FRAGMENT:
    case WStype_FRAGMENT_FIN:
      Serial.printf("[cloud] Transcribe fragmento tipo %d (%u bytes)\n", type, (unsigned)length);
      break;
    default:
      break;
  }
}

static void startStream() {
  if (creds.expiration) {  // 0 = ainda não pediu nenhuma
    Serial.printf("[cloud] credenciais expiram em %ld s\n", (long)(creds.expiration - time(nullptr)));
  }
  if (creds.expiration - time(nullptr) < (time_t)CREDS_MARGIN_S && !fetchCreds()) {
    nextWsTry = millis() + 5000;
    return;
  }
  Serial.print("[cloud] abrindo stream do Transcribe...\n");
  ws.disconnect();
  ws.beginSslWithCA(TRANSCRIBE_HOST, TRANSCRIBE_PORT, transcribePresignPath(creds).c_str(),
                    AMAZON_ROOT_CA1, "");
  wsStarting = true;
  wsStartedAt = millis();
}

// Envia no máximo um AudioEvent por chamada, para o loop sempre voltar a
// ler o WebSocket e o MQTT. Se a rede não acompanhar, a fila do áudio enche e
// o callback do mic descarta os trechos mais novos.
static void pumpAudio() {
  const int16_t* chunk;
  for (int popped = 0; popped < CHUNKS_PER_EVENT && audioPop(&chunk, 5); ++popped) {
    if (!wsOpen) { ++statDrop; continue; }  // sem stream o áudio é descartado
    int16_t* dst = pcm + pcmChunks * AUDIO_CHUNK;
    for (int i = 0; i < AUDIO_CHUNK; ++i) {
      int32_t v = chunk[i] * AUDIO_GAIN;
      dst[i] = v > 32767 ? 32767 : v < -32768 ? -32768 : v;
      uint32_t a = abs(dst[i]);
      if (a > statPeak) statPeak = a;
    }
    if (++pcmChunks < CHUNKS_PER_EVENT) continue;
    pcmChunks = 0;
    size_t n = transcribeEncodeAudioEvent((const uint8_t*)pcm, sizeof pcm, eventBuf);
    if (ws.sendBIN(eventBuf, n)) ++statTx;
  }
}

// ---------------------------------------------------------------- task

static void waitWifiAndTime() {
  state = CloudState::Offline;
  Serial.printf("[cloud] conectando ao WiFi %s...\n", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    audioFlush();
    vTaskDelay(pdMS_TO_TICKS(250));
  }
  Serial.printf("[cloud] WiFi ok, IP %s\n", WiFi.localIP().toString().c_str());
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  while (time(nullptr) < 1700000000) {
    audioFlush();
    vTaskDelay(pdMS_TO_TICKS(200));
  }
}

static void cloudTask(void*) {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // economia de energia do WiFi atrasa o streaming
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  mqttNet.setCACert(AMAZON_ROOT_CA1);
  mqttNet.setCertificate(DEVICE_CERT);
  mqttNet.setPrivateKey(DEVICE_KEY);
  mqtt.setServer(IOT_DATA_ENDPOINT, 8883);
  mqtt.setBufferSize(MQTT_BUFFER);
  // A espera do CONNACK no PubSubClient não cede a CPU: acima de 5 s o
  // watchdog do IDLE0 reinicia a placa.
  mqtt.setSocketTimeout(4);
  mqtt.setCallback(onCommand);

  ws.onEvent(onWsEvent);
  // A reconexão é feita por startStream() com uma URL assinada nova; o
  // intervalo da lib só precisa ser maior que o nosso retry (2 s).
  ws.setReconnectInterval(5000);

  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      wsOpen = wsStarting = online = false;
      waitWifiAndTime();
    }
    uint32_t now = millis();

    if (!mqtt.connected() && (int32_t)(now - nextMqttTry) >= 0) connectMqtt();
    mqtt.loop();
    online = mqtt.connected();

    bool want = recording;
    if (want != streamOn) {
      streamOn = want;
      Serial.printf("[cloud] gravação %s\n", want ? "ligada" : "desligada");
      if (want) {
        nextWsTry = now;
      } else {
        ws.disconnect();
        wsOpen = wsStarting = false;
      }
      clearRules(false);  // as de fala são da reunião que acabou (ou de antes dela)
    }
    if (String* line = __atomic_exchange_n(&simulatedLine, nullptr, __ATOMIC_ACQ_REL)) {
      Serial.printf("[cloud] frase simulada: %s\n", line->c_str());
      matchSpeech(*line);
      delete line;
    }
    drainOutbox();
    serviceRules();

    // Desligado, ws.loop() não pode rodar: a lib reconectaria sozinha com a URL antiga
    if (streamOn) {
      if (!wsOpen && !wsStarting && (int32_t)(now - nextWsTry) >= 0) startStream();
      if (wsStarting && millis() - wsStartedAt > WS_CONNECT_TIMEOUT_MS) {
        Serial.print("[cloud] Transcribe não respondeu, tentando de novo\n");
        ws.disconnect();
        wsStarting = false;
        nextWsTry = now + 3000;
      }
      ws.loop();
    }

    state = !streamOn ? CloudState::Ready : wsOpen ? CloudState::Streaming : CloudState::Connecting;
    if (streamOn && (int32_t)(millis() - nextStat) >= 0) {
      nextStat = millis() + 5000;
      Serial.printf("[cloud] stats tx=%u rx=%u bad=%u drop=%u peak=%u ws=%d heap=%u\n", statTx, statRx,
                    statBad, statDrop, statPeak, wsOpen, ESP.getFreeHeap());
      statPeak = 0;
    }
    pumpAudio();
  }
}

void cloudBegin() {
  eventBuf = (uint8_t*)malloc(transcribeAudioEventSize(sizeof pcm));
  bubbleQueue = xQueueCreate(4, sizeof(Bubble*));
  cmdQueue = xQueueCreate(8, sizeof(String*));
  extQueue = xQueueCreate(8, sizeof(ExtMessage*));
  outbox = xQueueCreate(16, sizeof(Outgoing*));
  xTaskCreatePinnedToCore(cloudTask, "cloud", 16384, nullptr, 3, nullptr, 0);
}

CloudState cloudState() { return state; }

bool cloudOnline() { return online; }

void cloudSetRecording(bool on) { recording = on; }

bool cloudRecording() { return recording; }

void cloudSetOba(const String& id, const std::vector<String>& words) {
  std::lock_guard<std::mutex> lock(obaLock);
  obaId = id;
  wakeWords = words;
}

uint32_t cloudTakeKeywordHits() {
  return __atomic_exchange_n(&keywordHits, 0, __ATOMIC_RELAXED);
}

Bubble* cloudTakeBubble() {
  Bubble* b = nullptr;
  return xQueueReceive(bubbleQueue, &b, 0) == pdTRUE ? b : nullptr;
}

bool cloudBubblePending() { return uxQueueMessagesWaiting(bubbleQueue) > 0; }

void cloudSetBubbleBusy(bool busy) { bubbleBusy = busy; }

String* cloudTakeCommand() {
  String* cmd = nullptr;
  return xQueueReceive(cmdQueue, &cmd, 0) == pdTRUE ? cmd : nullptr;
}

ExtMessage* cloudTakeExt() {
  ExtMessage* m = nullptr;
  return xQueueReceive(extQueue, &m, 0) == pdTRUE ? m : nullptr;
}

bool cloudTakeStateRequest() { return __atomic_exchange_n(&stateRequested, false, __ATOMIC_ACQ_REL); }

void cloudSend(Channel ch, const String& json, const char* evt, bool publish) {
  Outgoing* m = new Outgoing{ch, publish, evt ? evt : "", json};
  if (xQueueSend(outbox, &m, 0) != pdTRUE) delete m;  // a rede está atrasada: essa fica de fora
}

void cloudSendExt(const String& src, const String& json) {
  Outgoing* m = new Outgoing{Channel::Ext, true, "", json, src};
  if (xQueueSend(outbox, &m, 0) != pdTRUE) delete m;
}

void cloudSimulateLine(const String& text) {
  delete __atomic_exchange_n(&simulatedLine, new String(text), __ATOMIC_ACQ_REL);
}
