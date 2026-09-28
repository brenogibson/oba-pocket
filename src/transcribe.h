// Protocolo do Amazon Transcribe streaming via WebSocket, sem SDK:
// URL pré-assinada (SigV4) e mensagens no formato event-stream.
#pragma once
#include <Arduino.h>

struct AwsCreds {
  String accessKeyId;
  String secretAccessKey;
  String sessionToken;
  time_t expiration = 0;  // epoch UTC
};

// Caminho + query assinados para /stream-transcription-websocket.
// O relógio precisa estar certo (NTP): a assinatura vale 5 minutos.
String transcribePresignPath(const AwsCreds& creds);

// Monta um AudioEvent com o PCM dado em out (que precisa de
// transcribeAudioEventSize(bytes) bytes). Retorna o tamanho total.
size_t transcribeAudioEventSize(size_t pcmBytes);
size_t transcribeEncodeAudioEvent(const uint8_t* pcm, size_t pcmBytes, uint8_t* out);

// Decodifica uma mensagem recebida. messageType/eventType vêm dos headers
// (":message-type" = "event" ou "exception"); payload aponta para o JSON.
bool transcribeDecode(const uint8_t* msg, size_t len, String& messageType, String& eventType,
                      const char** payload, size_t* payloadLen);
