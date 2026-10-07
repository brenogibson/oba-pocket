// Portal do Oba Pocket: login, conexão ao vivo com a placa e as abas.

import { start, login, logout, user } from './auth.js';
import { connect } from './live.js';
import { COLOR, shade, drawOba, faviconFor } from './oba-render.js';
import { $, setCurrent, toast } from './ui.js';
import * as meeting from './meeting.js';
import * as hist from './history.js';
import * as device from './device.js';
import * as obas from './obas.js';
import './upload.js';

const C = window.OBA_PORTAL;
const TABS = { reuniao: 'meeting', historico: 'history', placa: 'device', obas: 'obas', enviar: 'upload' };
let stopAvatar = () => {};
const THEME = ['--accent', '--accent2', '--soft', '--muted', '--bg', '--panel', '--on-accent', '--accent-text'];

// Contraste do WCAG entre duas cores #rrggbb (1 a 21)
function luminance(c) {
  const n = parseInt(c.slice(1), 16);
  const [r, g, b] = [16, 8, 0].map(s => {
    const v = ((n >> s) & 255) / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

function showTab() {
  const name = TABS[location.hash.slice(1)] ? location.hash.slice(1) : 'reuniao';
  for (const [hash, id] of Object.entries(TABS)) {
    $(`tab-${id}`).hidden = hash !== name;
    document.querySelector(`nav a[href="#${hash}"]`).classList.toggle('on', hash === name);
  }
  obas.shown(name === 'obas');
  hist.shown(name === 'historico');
}

function setOba(o) {
  setCurrent(o);
  document.title = `${o.name} · Oba Pocket`;
  $('title').textContent = o.name;
  const s = document.documentElement.style;
  const p = o.palette || {};
  for (const k of THEME) s.removeProperty(k);        // sem paleta (ou no que não der para ler): as cores do CSS
  if (COLOR.test(p.bg || '')) {
    const accent2 = COLOR.test(p.shadow || '') ? p.shadow : p.bg;
    const bg = shade(p.bg, 0.14), panel = shade(p.bg, 0.24);
    s.setProperty('--accent', p.bg);
    s.setProperty('--accent2', accent2);
    s.setProperty('--bg', bg);
    s.setProperty('--panel', panel);
    // As cores de texto da paleta são para a tela da placa: aqui só se lerem bem no painel
    const muted = p.off || p.text;
    if (COLOR.test(p.text || '') && contrast(p.text, panel) >= 4.5) s.setProperty('--soft', p.text);
    if (COLOR.test(muted || '') && contrast(muted, panel) >= 4.5) s.setProperty('--muted', muted);
    // Em cima do accent (botões, selos, cartas): branco ou quase preto, o que ler melhor nas duas pontas
    const on = c => Math.min(contrast(c, p.bg), contrast(c, accent2));
    s.setProperty('--on-accent', on('#ffffff') >= on('#0d1a22') ? '#ffffff' : '#0d1a22');
    if (contrast(p.bg, panel) < 3) s.setProperty('--accent-text', 'var(--soft)');
    document.querySelector('meta[name=theme-color]').content = bg;
  }
  stopAvatar();
  stopAvatar = drawOba($('avatar'), o, 40);
  faviconFor(o, $('favicon'));
  meeting.setOba(o);
}

function setStatus(ok, text) {
  const live = ok && meeting.live();
  const st = device.state();
  $('status').classList.toggle('live', !!live);
  $('status').classList.toggle('off', !ok || (st && st.online === false));
  $('statusText').textContent = !ok ? text : st && st.online === false ? 'placa desconectada'
    : live ? 'ao vivo' : st && st.rec === false ? 'REC desligado' : 'conectado';
}

let connected = false;
function onMessage(ch, m, retained) {
  if (ch === 'ui/oba' && m) setOba(m);
  if (ch === 'state' && m) meeting.setState(m);
  device.onMessage(ch, m, retained);
  meeting.onMessage(ch, m, retained);
  obas.onMessage(ch, m);
  hist.onMessage(ch, m, retained);
  setStatus(connected);
}

async function boot() {
  window.addEventListener('hashchange', showTab);
  $('loginBtn').addEventListener('click', () => login());
  $('logoutBtn').addEventListener('click', () => logout());
  if (!C) { $('loginMsg').textContent = 'Falta o config.js: rode o setup.py.'; $('login').hidden = false; return; }
  let ok = false;
  try {
    ok = await start();
  } catch (e) {
    console.error(e);
    $('loginMsg').textContent = e.message;
  }
  if (!ok) { $('login').hidden = false; return; }
  $('app').hidden = false;
  $('who').textContent = user();
  showTab();
  connect(onMessage, (ok, text, err) => {
    connected = ok;
    setStatus(ok, text);
    if (err && err.status === 401) { toast(err.message, true); setTimeout(() => location.reload(), 2500); }
  });
}

boot();
