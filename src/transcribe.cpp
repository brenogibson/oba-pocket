#include "transcribe.h"
#if !__has_include("aws_config.h")
#error "falta src/aws_config.h: rode python3 setup.py (ele gera o arquivo a partir do config.json)"
#endif
#include "aws_config.h"
#include <mbedtls/md.h>
#include <rom/crc.h>
#include <time.h>

// ---------------------------------------------------------------- SigV4

static void sha256Hex(const uint8_t* data, size_t len, char out[65]) {
  uint8_t h[32];
  mbedtls_md(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), data, len, h);
  for (int i = 0; i < 32; ++i) sprintf(out + i * 2, "%02x", h[i]);
}

static void hmac(const uint8_t* key, size_t keyLen, const String& msg, uint8_t out[32]) {
  mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), key, keyLen,
                  (const uint8_t*)msg.c_str(), msg.length(), out);
}

// URI encode do SigV4: só A-Z a-z 0-9 - _ . ~ passam sem escape
static String uriEncode(const String& s) {
  String out;
  out.reserve(s.length() * 3 / 2);
  for (size_t i = 0; i < s.length(); ++i) {
    char c = s[i];
    if (isalnum((unsigned char)c) || c == '-' || c == '_' || c == '.' || c == '~') {
      out += c;
    } else {
      char buf[4];
      sprintf(buf, "%%%02X", (uint8_t)c);
      out += buf;
    }
  }
  return out;
}

String transcribePresignPath(const AwsCreds& creds) {
  time_t now = time(nullptr);
  struct tm t;
  gmtime_r(&now, &t);
  char amzDate[17], date[9];
  strftime(amzDate, sizeof amzDate, "%Y%m%dT%H%M%SZ", &t);
  strftime(date, sizeof date, "%Y%m%d", &t);

  String scope = String(date) + "/" TRANSCRIBE_REGION "/transcribe/aws4_request";
  String host = String(TRANSCRIBE_HOST ":") + TRANSCRIBE_PORT;

  // Parâmetros já em ordem alfabética, como a assinatura exige
  String q;
  q.reserve(1600);
  q += "X-Amz-Algorithm=AWS4-HMAC-SHA256";
  q += "&X-Amz-Credential=" + uriEncode(creds.accessKeyId + "/" + scope);
  q += "&X-Amz-Date=" + String(amzDate);
  q += "&X-Amz-Expires=300";
  q += "&X-Amz-Security-Token=" + uriEncode(creds.sessionToken);
  q += "&X-Amz-SignedHeaders=host";
  q += "&language-code=" TRANSCRIBE_LANG;
  q += "&media-encoding=pcm";
  q += "&sample-rate=16000";
  if (*TRANSCRIBE_VOCAB) q += "&vocabulary-name=" TRANSCRIBE_VOCAB;  // vazio: sem vocabulário

  char emptyHash[65];
  sha256Hex(nullptr, 0, emptyHash);
  String canonical = "GET\n/stream-transcription-websocket\n" + q + "\nhost:" + host +
                     "\n\nhost\n" + emptyHash;
  char canonicalHash[65];
  sha256Hex((const uint8_t*)canonical.c_str(), canonical.length(), canonicalHash);
  String toSign = String("AWS4-HMAC-SHA256\n") + amzDate + "\n" + scope + "\n" + canonicalHash;

  uint8_t k[32];
  String secret = "AWS4" + creds.secretAccessKey;
  hmac((const uint8_t*)secret.c_str(), secret.length(), date, k);
  hmac(k, 32, TRANSCRIBE_REGION, k);
  hmac(k, 32, "transcribe", k);
  hmac(k, 32, "aws4_request", k);
  uint8_t sig[32];
  hmac(k, 32, toSign, sig);
  char sigHex[65];
  for (int i = 0; i < 32; ++i) sprintf(sigHex + i * 2, "%02x", sig[i]);

  return "/stream-transcription-websocket?" + q + "&X-Amz-Signature=" + sigHex;
}

// ---------------------------------------------------------------- event-stream
//
// [tamanho total 4][tamanho headers 4][crc do prelude 4][headers][payload][crc 4]
// header: [tam. nome 1][nome][tipo 1 = 7 string][tam. valor 2][valor]

static uint32_t crc32(const uint8_t* data, size_t len, uint32_t crc = 0) {
  return crc32_le(crc, data, len);
}

static void putU32(uint8_t* p, uint32_t v) {
  p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v;
}

static uint32_t getU32(const uint8_t* p) {
  return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static size_t putHeader(uint8_t* p, const char* name, const char* value) {
  size_t n = strlen(name), v = strlen(value);
  p[0] = n;
  memcpy(p + 1, name, n);
  p[1 + n] = 7;
  p[2 + n] = v >> 8;
  p[3 + n] = v;
  memcpy(p + 4 + n, value, v);
  return 4 + n + v;
}

static uint8_t audioHeaders[128];
static size_t audioHeadersLen = 0;

static void buildAudioHeaders() {
  if (audioHeadersLen) return;
  uint8_t* p = audioHeaders;
  p += putHeader(p, ":content-type", "application/octet-stream");
  p += putHeader(p, ":event-type", "AudioEvent");
  p += putHeader(p, ":message-type", "event");
  audioHeadersLen = p - audioHeaders;
}

size_t transcribeAudioEventSize(size_t pcmBytes) {
  buildAudioHeaders();
  return 16 + audioHeadersLen + pcmBytes;
}

size_t transcribeEncodeAudioEvent(const uint8_t* pcm, size_t pcmBytes, uint8_t* out) {
  size_t total = transcribeAudioEventSize(pcmBytes);
  putU32(out, total);
  putU32(out + 4, audioHeadersLen);
  putU32(out + 8, crc32(out, 8));
  memcpy(out + 12, audioHeaders, audioHeadersLen);
  if (pcmBytes) memcpy(out + 12 + audioHeadersLen, pcm, pcmBytes);
  putU32(out + total - 4, crc32(out, total - 4));
  return total;
}

bool transcribeDecode(const uint8_t* msg, size_t len, String& messageType, String& eventType,
                      const char** payload, size_t* payloadLen) {
  if (len < 16) return false;
  uint32_t total = getU32(msg), hlen = getU32(msg + 4);
  if (total != len || 16 + hlen > total) return false;
  messageType = "";
  eventType = "";
  const uint8_t* p = msg + 12;
  const uint8_t* end = p + hlen;
  while (p < end) {
    uint8_t n = p[0];
    String name((const char*)p + 1, n);
    p += 1 + n;
    if (*p++ != 7) return false;  // o Transcribe só manda headers string
    uint16_t v = p[0] << 8 | p[1];
    String value((const char*)p + 2, v);
    p += 2 + v;
    if (name == ":message-type") messageType = value;
    else if (name == ":event-type" || name == ":exception-type") eventType = value;
  }
  *payload = (const char*)end;
  *payloadLen = total - 16 - hlen;
  return true;
}
