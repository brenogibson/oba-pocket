// Login do portal: managed login do Cognito com code + PKCE, sem segredo no navegador.
// O access e o ID token ficam só na memória; o refresh token fica no localStorage para
// o celular continuar logado (o CSP não deixa rodar script de fora), e gira a cada uso.

const C = window.OBA_PORTAL || {};
const REFRESH = 'oba.refresh';
const PKCE = 'oba.pkce';
const te = new TextEncoder();

let tokens = null;          // {access, id, exp, user}
let timer = null;

export const hex = b => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('');
const b64url = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const random = n => b64url(crypto.getRandomValues(new Uint8Array(n)));

function claims(jwt) {
  const s = atob(jwt.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'));
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(s, c => c.charCodeAt(0))));
}

async function oauth(path, params) {
  const r = await fetch(`${C.login}/oauth2/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: C.clientId, ...params }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error_description || j.error || `login: erro ${r.status}`), { status: r.status });
  return j;
}

function keep(j) {
  if (j.refresh_token) localStorage.setItem(REFRESH, j.refresh_token);
  const c = claims(j.id_token);
  tokens = { access: j.access_token, id: j.id_token, exp: c.exp * 1000, user: c['cognito:username'] };
  schedule(Math.max(10e3, tokens.exp - Date.now() - 5 * 60e3));
}

// Renova antes de vencer; sem rede, tenta de novo em 30 s
function schedule(ms) {
  clearTimeout(timer);
  timer = setTimeout(() => refresh().catch(() => schedule(30e3)), ms);
}

export async function login() {
  const verifier = random(48), state = random(16);
  sessionStorage.setItem(PKCE, JSON.stringify({ verifier, state }));
  const challenge = b64url(await crypto.subtle.digest('SHA-256', te.encode(verifier)));
  const q = new URLSearchParams({
    response_type: 'code', client_id: C.clientId, redirect_uri: C.url, scope: 'openid email', state,
    code_challenge: challenge, code_challenge_method: 'S256', lang: 'pt-BR',
  });
  location.assign(`${C.login}/oauth2/authorize?${q}`);
}

// Volta do login (?code=) ou sessão guardada. true = logado.
export async function start() {
  const u = new URL(location.href);
  const code = u.searchParams.get('code'), error = u.searchParams.get('error');
  if (code || error) {
    history.replaceState(null, '', u.pathname + u.hash);
    const saved = JSON.parse(sessionStorage.getItem(PKCE) || 'null');
    sessionStorage.removeItem(PKCE);
    if (error) throw new Error(u.searchParams.get('error_description') || error);
    if (!saved || saved.state !== u.searchParams.get('state')) throw new Error('o login expirou: entre de novo');
    keep(await oauth('token', { grant_type: 'authorization_code', code, redirect_uri: C.url, code_verifier: saved.verifier }));
    return true;
  }
  return refresh();
}

// Um refresh por vez, também entre abas: com a rotação, o refresh token usado deixa de valer
let pending = null;
export function refresh() {
  const run = async () => {
    const rt = localStorage.getItem(REFRESH);
    if (!rt) { tokens = null; return false; }
    try {
      keep(await oauth('token', { grant_type: 'refresh_token', refresh_token: rt }));
      return true;
    } catch (e) {
      if (e.status !== 400) throw e;          // rede: tenta de novo depois
      if (localStorage.getItem(REFRESH) === rt) localStorage.removeItem(REFRESH);
      tokens = null;
      return false;
    }
  };
  pending ??= (navigator.locks ? navigator.locks.request(REFRESH, run) : run()).finally(() => { pending = null; });
  return pending;
}

// Tokens com pelo menos min ms de vida (1 minuto)
export async function fresh(min = 60e3) {
  if (tokens && tokens.exp - Date.now() > min) return tokens;
  if (!(await refresh())) throw Object.assign(new Error('sessão encerrada: entre de novo'), { status: 401 });
  return tokens;
}

export const user = () => tokens && tokens.user;

export async function logout() {
  const rt = localStorage.getItem(REFRESH);
  localStorage.removeItem(REFRESH);
  tokens = null;
  if (rt) await oauth('revoke', { token: rt }).catch(() => {});
  location.assign(`${C.login}/logout?${new URLSearchParams({ client_id: C.clientId, logout_uri: C.url })}`);
}

// API do portal (/api/*, atrás do CloudFront). O OAC assina o pedido à Lambda e ocupa o
// Authorization, então o token vai no x-oba-token; com corpo, o OAC exige o sha256 dele.
export async function api(method, path, body) {
  const blob = body instanceof Blob;
  const t = await fresh(blob ? 5 * 60e3 : 60e3);   // um zip de 4 MB no 4G pode levar minutos
  const headers = { 'x-oba-token': t.access };
  let data;
  if (body !== undefined) {
    data = blob ? new Uint8Array(await body.arrayBuffer()) : te.encode(JSON.stringify(body));
    headers['content-type'] = blob ? 'application/zip' : 'application/json';
    headers['x-amz-content-sha256'] = hex(await crypto.subtle.digest('SHA-256', data));
  }
  let r;
  try {
    r = await fetch(path, { method, headers, body: data });
  } catch {
    throw Object.assign(new Error('sem conexão com o portal: confira a internet'), { status: 0 });
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || `erro ${r.status}`), { status: r.status, detail: j });
  return j;
}
