// Aba Reunião: legendas ao vivo, as regras que o agente deixou armadas na placa, as
// sugestões que o Oba mostrou e o resumo quando o REC desliga.

import { $, esc, current } from './ui.js';

const KIND_LABEL = { service: 'Serviço AWS', blog: 'Blog AWS', doc: 'Documentação', demo: 'Demo', tip: 'Dica' };
const lines = [];           // frases finais
let partial = null;         // {id, text}
let hits = 0;
let wakeRe = null;
const state = { rec: null, online: null };

const highlight = s => wakeRe ? esc(s).replace(wakeRe, m => `<mark>${m}</mark>`) : esc(s);

export function render() {
  const html = lines.slice(-12).map(t => `<p>${highlight(t)}</p>`);
  if (partial) html.push(`<p class="partial">${highlight(partial.text)}</p>`);
  if (html.length) $('transcript').innerHTML = html.join('');
  else $('transcript').innerHTML = `<p class="empty">${state.rec ? 'Ouvindo…'
    : state.online === false ? 'A placa está desconectada' : `Ligue o REC no ${esc(current().name)} para começar`}</p>`;
  $('transcript').scrollTop = $('transcript').scrollHeight;
}

export function setOba(o) {
  const words = (o.wake_words || []).map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  wakeRe = words.length ? new RegExp(`\\b(${words.join('|')})\\b`, 'gi') : null;
  renderCounter();
  render();
}

export function setState(m) {
  state.online = m.online !== false;
  if (state.online) {
    state.rec = !!m.rec;
    if (!state.rec) partial = null;
  }
  render();
}

function onTranscript(m) {
  if (m.partial) {
    partial = { id: m.id, text: m.text || '' };
  } else {
    if (m.text) lines.push(m.text);
    if (lines.length > 200) lines.splice(0, lines.length - 200);
    if (partial && partial.id === m.id) partial = null;
  }
  render();
}

function renderCounter() {
  const w = (current().wake_words || [])[0];
  $('counter').innerHTML = w ? `“${esc(current().name)}” citado <b>${hits}</b>×` : '';
}

// ------------------------------------------------------------ comandos do agente (cmd)
// Os mesmos que a placa recebe; "card" é só para as telas
const hand = new Map();     // id da regra -> {card, bubble, expires, el}

const speakIn = c => (c.do || []).find(d => d.type === 'speak');
const iconImg = b => b && b.png ? `<img class="icon" src="data:image/png;base64,${esc(b.png)}" alt="">` : '';
function qrSvg(url) {
  const q = qrcode(0, 'M');
  q.addData(url);
  q.make();
  return q.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
}

function showCard(card, b, heard) {
  card = card || {}; b = b || {};
  if (card.kind === 'summary' || (!card.title && !b.text)) return;  // o resumo tem a tela própria
  const url = card.url || b.url;
  const side = b.kind === 'qr' && url ? `<div class="qr">${qrSvg(url)}</div>` : iconImg(b);
  const el = document.createElement('div');
  el.className = 'card';
  el.innerHTML = `<small>${esc(KIND_LABEL[card.kind] || current().name)}</small>` +
    `<div class="row"><div><h2>${esc(card.title || '')}</h2><p>${esc(card.body || b.text || '')}</p></div>${side}</div>` +
    `<footer>${heard ? `lembrei quando ouvi “${esc(heard)}”` : 'direto da conversa'}` +
    `${card.source ? ` · fonte: ${esc(card.source)}` : ''}</footer>`;
  document.querySelectorAll('.card').forEach(x => x.classList.add('old'));
  $('hand').after(el);
  const all = document.querySelectorAll('.card');
  for (let i = 3; i < all.length; i++) all[i].remove();
}

function renderHandEmpty() {
  const empty = $('handList').querySelector('.empty');
  if (hand.size && empty) empty.remove();
  if (!hand.size && !empty) $('handList').innerHTML = '<p class="empty">Nada armado ainda</p>';
}

function arm(c) {
  disarm(c.id);
  const b = (speakIn(c) || {}).bubble || {};
  const chips = c.words && c.words.length ? c.words : [c.on || 'speech'];
  const el = document.createElement('div');
  el.className = 'armed';
  el.innerHTML = `${b.png ? `<img src="data:image/png;base64,${esc(b.png)}" alt="">` : ''}` +
    `<div><b>${esc((c.card && c.card.title) || b.text || c.on)}</b>` +
    `<div class="chips">${chips.map(t => `<span class="chip">${esc(t)}</span>`).join('')}</div></div>`;
  $('handList').append(el);
  hand.set(c.id, { card: c.card, bubble: b, expires: Date.now() + (c.ttl_s || 900) * 1000, el });
  renderHandEmpty();
}

function disarm(id, fired) {
  const h = hand.get(id);
  if (!h) return;
  hand.delete(id);
  if (fired) {
    h.el.querySelectorAll('.chip').forEach(x => x.classList.toggle('hit', x.textContent === fired));
    h.el.classList.add('fired');
    setTimeout(() => { h.el.remove(); renderHandEmpty(); }, 900);
  } else {
    h.el.remove();
    renderHandEmpty();
  }
}

function clearAll() {
  [...hand.keys()].forEach(id => disarm(id));
  document.querySelectorAll('.card').forEach(x => x.remove());
  thinking(false);
}

function onCommand(m) {
  switch (m.type) {
    case 'speak': showCard(m.card, m.bubble); break;
    case 'arm': arm(m); break;
    case 'disarm': disarm(m.id); break;
    case 'reset':               // session nova = começou uma reunião; null = acabou
      clearAll();
      if (m.session) { lines.length = 0; partial = null; hits = 0; renderCounter(); render(); hideSummary(); }
      break;
  }
}
setInterval(() => { for (const [id, h] of hand) if (Date.now() > h.expires) disarm(id); }, 5000);

function onEvent(m) {
  if (m.type === 'wake') { hits++; renderCounter(); }
  else if (m.type === 'rule.fired') {
    const h = hand.get(m.id);
    if (!h) return;
    disarm(m.id, m.trigger);
    showCard(h.card, h.bubble, m.text && m.text.length < 90 ? m.text : m.trigger);
  }
}

function thinking(on, run) {
  $('thinking').classList.toggle('on', !!on);
  $('thinkingText').textContent = run === 'summary' ? `${current().name} escrevendo o resumo…` : `${current().name} pensando…`;
}

// ------------------------------------------------------------ resumo (ui/summary, retido)
const list = (title, items) => items && items.length
  ? `<div><h4>${esc(title)}</h4><ul>${items.map(x => `<li>${x}</li>`).join('')}</ul></div>` : '';
const host = u => { try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.hostname : ''; } catch { return ''; } };

function showSummary(s, open) {
  const sugg = (s.suggestions || []).map(x => `${esc(x.title)}` +
    (x.url && host(x.url) ? ` — <a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(host(x.url))}</a>` : ''));
  $('summary').querySelector('article').innerHTML =
    `<button class="close" type="button">Fechar</button>` +
    `<small>Resumo da conversa · ${Number(s.lines) || 0} frases · ${new Date(s.ts || Date.now()).toLocaleTimeString('pt-BR')}</small>` +
    `<h2>${esc(s.title || 'Resumo')}</h2><p class="text">${esc(s.summary || '')}</p>` +
    `<div class="cols">${list('Assuntos', (s.topics || []).map(esc))}${list('Decisões', (s.decisions || []).map(esc))}` +
    `${list('Próximos passos', (s.next_steps || []).map(esc))}${list(`Sugestões do ${current().name}`, sugg)}</div>`;
  $('summary').classList.toggle('on', open);
  $('lastSummary').hidden = false;
}
function hideSummary() { $('summary').classList.remove('on'); }
$('summary').addEventListener('click', e => { if (e.target === $('summary') || e.target.closest('.close')) hideSummary(); });
$('lastSummary').addEventListener('click', () => $('summary').classList.add('on'));

export function onMessage(ch, m, retained) {
  if (!m) {                   // retido apagado
    if (ch === 'ui/summary') { hideSummary(); $('lastSummary').hidden = true; }
    return;
  }
  switch (ch) {
    case 'transcript': onTranscript(m); break;
    case 'evt': onEvent(m); break;
    case 'cmd': onCommand(m); break;
    case 'ui/think': thinking(m.state === 'start', m.run); break;
    case 'ui/summary': showSummary(m, !retained); break;   // o retido fica no botão, sem abrir sozinho
  }
}

export const live = () => state.rec && state.online;
thinking(false);
render();
