// Aba Placa: o state retido (Oba ativo, Obas instalados, REC, bateria) e os comandos que a
// API aceita (portal/api/api.py). Ligar o REC não existe aqui: só pelo botão na placa.

import { api } from './auth.js';
import { $, esc, ago, current, toast, confirmDialog } from './ui.js';

let st = null;              // último state
export const state = () => st;
const listeners = [];
export const onChange = fn => listeners.push(fn);

// id do comando -> {label, timer}. Ativar e remover sempre respondem; os outros só quando
// a placa recusa (docs/protocol.md)
const pending = new Map();
const REPLY_MS = 20e3, REFUSE_MS = 5e3;

export async function command(cmd, label, answers) {
  let id;
  try {
    ({ id } = await api('POST', '/api/device/cmd', cmd));
  } catch (e) {
    toast(`${label}: ${e.message}`, true);
    return;
  }
  toast(answers ? `${label}: esperando a placa…` : `${label}: enviado`);
  pending.set(id, { label, timer: setTimeout(() => {
    pending.delete(id);
    if (answers) toast(`${label}: a placa não respondeu`, true);
  }, answers ? REPLY_MS : REFUSE_MS) });
}

function onReply(m) {
  const p = pending.get(m.id);
  if (!p) return;
  clearTimeout(p.timer);
  pending.delete(m.id);
  if (m.ok) toast(`${p.label}: pronto`);
  else toast(`${p.label}: ${m.error || 'a placa recusou'}`, true);
}

function battery(b) {
  if (!b || b.pct == null) return '';
  return `${b.pct}%${b.charging ? ' · carregando' : ''}`;
}

// Fontes externas (state.ext, docs/protocol.md): o nome sem o prefixo da ponte, o humor e os pedidos
const MOODS = { idle: 'ociosa', busy: 'trabalhando', alert: 'esperando você' };
function sources(ext) {
  return ext.map(e => {
    const name = String(e.src || '').replace(/^.*-ponte-/, '');
    const asks = e.asks ? ` · ${e.asks} ${e.asks > 1 ? 'pedidos' : 'pedido'}` : '';
    return `${esc(name)} <small>${esc(MOODS[e.mood] || e.mood || '')}${asks}</small>`;
  }).join('<br>');
}

function actions() {
  const sounds = (st.caps || []).includes('speaker') ? current().sounds || [] : [];
  const out = [];
  if (st.rec) out.push('<button type="button" class="danger" data-act="rec">Desligar REC</button>');
  if (sounds.length) {
    out.push(`<span class="sound"><select id="soundSel" aria-label="Som"${st.rec ? ' disabled' : ''}>
      ${sounds.map(s => `<option>${esc(s)}</option>`).join('')}</select>
      <button type="button" data-act="play"${st.rec ? ' disabled' : ''}>Tocar</button></span>`);
  }
  out.push('<button type="button" data-act="state">Atualizar</button>');
  const hint = st.rec ? `<p class="hint">Com o REC ligado a placa não troca de Oba nem toca sons.</p>` : '';
  return `<div class="actions">${out.join('')}</div>${hint}`;
}

export function render() {
  const el = $('deviceInfo');
  if (!st) { el.innerHTML = '<p class="empty">Esperando o estado da placa…</p>'; return; }
  if (st.online === false) {
    el.innerHTML = `<p class="empty">A placa está desconectada${st.seen ? ` (último sinal ${ago(st.seen)})` : ''}.</p>`;
    $('deviceObas').replaceChildren();
    return;
  }
  const a = st.active || {};
  el.innerHTML = `<dl>
    <dt>Oba ativo</dt><dd>${esc(a.name || st.oba || '—')} <small>${esc(a.version || '')}</small></dd>
    <dt>REC</dt><dd>${st.rec ? '<b class="rec">ligado</b>' : 'desligado'}</dd>
    <dt>Bateria</dt><dd>${esc(battery(st.battery)) || '—'}</dd>
    ${st.ext && st.ext.length ? `<dt>Fontes</dt><dd>${sources(st.ext)}</dd>` : ''}
    <dt>Firmware</dt><dd>${esc(st.fw || '—')}</dd>
    <dt>Atualizado</dt><dd>${st.ts ? ago(st.ts) : '—'}</dd></dl>${actions()}`;
  $('deviceObas').innerHTML = (st.obas || []).map(o => {
    const buttons = o.id === a.id ? '<span class="tag">ativo</span>'
      : `<button type="button" data-act="activate"${st.rec ? ' disabled' : ''}>Ativar</button>` +
        (o.builtin ? '' : '<button type="button" class="danger" data-act="remove">Remover</button>');
    return `<li data-id="${esc(o.id)}" data-name="${esc(o.name || o.id)}">
      <span><b>${esc(o.name || o.id)}</b> <small>${esc(o.version || '')}${o.builtin ? ' · embutido' : ''}</small></span>
      ${buttons}</li>`;
  }).join('');
}

async function onClick(ev) {
  const b = ev.target.closest('button[data-act]');
  if (!b || b.disabled) return;
  const li = b.closest('li[data-id]');
  const id = li && li.dataset.id, nm = li && li.dataset.name;
  switch (b.dataset.act) {
    case 'rec':
      if (await confirmDialog('Desligar o REC?', 'As legendas param e o Oba faz o resumo da reunião.', 'Desligar'))
        command({ type: 'rec', on: false }, 'Desligar REC');
      break;
    case 'play':
      command({ type: 'play', sound: $('soundSel').value }, `Som ${$('soundSel').value}`);
      break;
    case 'state':
      command({ type: 'state' }, 'Atualizar');
      break;
    case 'activate':
      command({ type: 'oba.activate', target: id }, `Ativar ${nm}`, true);
      break;
    case 'remove':
      if (await confirmDialog(`Remover ${nm}?`, 'Apaga o Oba do cartão da placa. O registro continua com ele, ' +
                              'então dá para instalar de novo.', 'Remover'))
        command({ type: 'oba.remove', target: id }, `Remover ${nm}`, true);
      break;
  }
}

export function onMessage(ch, m) {
  if (ch === 'reply' && m) return onReply(m);
  if (ch === 'ui/oba' && m) return st && render();   // os sons do Oba ativo
  if (ch !== 'state' || !m) return;
  st = m.online === false ? { ...(st || {}), online: false, seen: st && st.ts } : m;
  render();
  listeners.forEach(fn => fn(st));
}

$('tab-device').addEventListener('click', onClick);
render();
