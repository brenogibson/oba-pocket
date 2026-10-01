// Tópicos da placa ao vivo: o ID token vira credenciais do identity pool, que só
// assinam e recebem <prefixo>/<placa>/* (docs/protocol.md), e abrem o MQTT por WSS com SigV4.

import { api, fresh, hex } from './auth.js';

const C = window.OBA_PORTAL || {};
export const BASE = `${C.prefix}/${C.device}/`;
// Tópicos exatos: o IoT Core não entrega as mensagens retidas (state, ui/oba, ui/summary)
// numa assinatura com curinga
const CHANNELS = ['state', 'transcript', 'evt', 'cmd', 'reply', 'ui/oba', 'ui/think', 'ui/summary', 'ui/install'];
const te = new TextEncoder();
const sha256 = async s => hex(await crypto.subtle.digest('SHA-256', te.encode(s)));

async function hmac(key, msg) {
  const k = await crypto.subtle.importKey('raw', typeof key === 'string' ? te.encode(key) : key,
                                          { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return crypto.subtle.sign('HMAC', k, te.encode(msg));
}

async function cognito(target, body) {
  const r = await fetch(`https://cognito-identity.${C.region}.amazonaws.com/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-amz-json-1.1', 'X-Amz-Target': `AWSCognitoIdentityService.${target}` },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.message || `Cognito: erro ${r.status}`);
  return j;
}

let identity = null;        // a API anexa a policy de leitura do IoT a esta identidade
let creds = null;

async function credentials() {
  if (creds && creds.exp - Date.now() > 5 * 60e3) return creds;
  const t = await fresh();
  if (!identity) identity = (await api('POST', '/api/iot-access', { id_token: t.id })).identity;
  const logins = { [`cognito-idp.${C.region}.amazonaws.com/${C.userPool}`]: t.id };
  const c = (await cognito('GetCredentialsForIdentity', { IdentityId: identity, Logins: logins })).Credentials;
  creds = { ...c, exp: c.Expiration * 1000 };
  return creds;
}

async function signedUrl() {
  const c = await credentials();
  const now = new Date().toISOString().replace(/[-:]|\.\d{3}/g, ''), date = now.slice(0, 8);
  const scope = `${date}/${C.region}/iotdevicegateway/aws4_request`;
  const qs = `X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=${encodeURIComponent(c.AccessKeyId + '/' + scope)}` +
             `&X-Amz-Date=${now}&X-Amz-SignedHeaders=host`;
  const canonical = `GET\n/mqtt\n${qs}\nhost:${C.iotEndpoint}\n\nhost\n${await sha256('')}`;
  let k = await hmac('AWS4' + c.SecretKey, date);
  for (const part of [C.region, 'iotdevicegateway', 'aws4_request']) k = await hmac(k, part);
  const sig = hex(await hmac(k, `AWS4-HMAC-SHA256\n${now}\n${scope}\n${await sha256(canonical)}`));
  // No IoT o token entra depois da assinatura
  return `wss://${C.iotEndpoint}/mqtt?${qs}&X-Amz-Signature=${sig}&X-Amz-Security-Token=${encodeURIComponent(c.SessionToken)}`;
}

// onMessage(canal, objeto | null, retido): null = retido apagado. onStatus(conectado, texto).
export function connect(onMessage, onStatus) {
  let client = null, wait = 1000, timer = null;
  const again = () => {
    clearTimeout(timer);
    timer = setTimeout(open, wait);
    wait = Math.min(wait * 2, 30e3);
  };
  async function open() {
    if (client) return;
    onStatus(false, 'conectando…');
    try {
      const rand = hex(crypto.getRandomValues(new Uint8Array(4)));
      const url = await signedUrl();
      client = mqtt.connect(url, { clientId: `${identity}-${rand}`, protocolVersion: 4, reconnectPeriod: 0, keepalive: 30 });
    } catch (e) {
      console.error(e);
      onStatus(false, e.status === 401 ? e.message : 'sem conexão, tentando de novo…', e);
      if (e.status !== 401) again();
      return;
    }
    const me = client;
    me.on('connect', () => {
      wait = 1000;
      onStatus(true, 'conectado');
      // O IoT Core derruba a conexão com mais de 8 tópicos num SUBSCRIBE
      for (let i = 0; i < CHANNELS.length; i += 8) me.subscribe(CHANNELS.slice(i, i + 8).map(c => BASE + c));
    });
    me.on('message', (topic, payload, packet) => {
      let m = null;
      if (payload.length) {
        try { m = JSON.parse(payload.toString()); } catch { return; }
        if (m.v !== 1) return;
      }
      onMessage(topic.slice(BASE.length), m, packet.retain);
    });
    // As credenciais valem 1 h: ao cair, reconecta com uma URL nova
    me.on('close', () => {
      me.end(true);
      if (client !== me) return;
      client = null;
      onStatus(false, 'reconectando…');
      again();
    });
    me.on('error', e => console.warn('mqtt', e.message));
  }
  // No celular a conexão morre com a tela apagada: volta assim que a aba aparece
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !client) { wait = 1000; again(); }
  });
  open();
}
