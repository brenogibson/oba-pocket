// Aba Histórico: os resumos que o roteador guarda por portal.history_days dias. Tocar abre o
// resumo inteiro; apagar tira o resumo e a sessão dele (legendas e regras) pela API.

import { api } from './auth.js';
import { $, esc, toast, confirmDialog } from './ui.js';
import { frases, openSummary, when } from './summary.js';

let list = null;            // o que a API devolveu em /api/history
let loading = null;
let visible = false;
let stale = false;          // chegou resumo novo com a aba fechada
let busy = false;
const ID_RE = /^[0-9]{13}_(?:[0-9]{8}-[0-9]{6}-[0-9a-f]{6}|-)$/;   // o que as rotas aceitam
const DAY = 86400e3;
const ROUNDS = 20;          // a API apaga por até 20 s e devolve "more": pede de novo

export const daysLeft = (expires, now = Date.now()) => Math.max(1, Math.ceil((Number(expires) - now) / DAY));

export function itemHtml(it, now = Date.now()) {
  if (!it || !ID_RE.test(it.id)) return '';
  const d = daysLeft(it.expires, now);
  return `<li data-id="${esc(it.id)}" data-title="${esc(it.title || 'Resumo')}">` +
    `<button type="button" class="open" data-act="open"><b>${esc(it.title || 'Resumo')}</b>` +
    `<small>${esc(when(it.ts))} · ${frases(it.lines)}apaga em ${d} ${d === 1 ? 'dia' : 'dias'}</small></button>` +
    `<button type="button" class="danger" data-act="drop">Apagar</button></li>`;
}

export function listHtml(l, now = Date.now()) {
  if (!l) return '<p class="empty">Carregando o histórico…</p>';
  if (l.error) return `<p class="empty">Não deu para listar: ${esc(l.error)}</p>`;
  if (!l.items.length) return '<p class="empty">Nenhum resumo guardado. Quando o REC desliga, o resumo da conversa aparece aqui.</p>';
  return `<ul class="list">${l.items.map(it => itemHtml(it, now)).join('')}</ul>` +
    (l.truncated ? `<p class="hint">Só os ${l.items.length} mais novos aparecem aqui.</p>` : '');
}

export async function load() {
  if (loading) return loading;
  stale = false;
  loading = api('GET', '/api/history').then(r => { list = { ...r, items: Array.isArray(r.items) ? r.items : [] }; })
    .catch(e => {
      if (!list) list = { error: e.message };
      toast(`Histórico: ${e.message}`, true);
    }).finally(() => { loading = null; render(); });
  return loading;
}

export function render() {
  if (!visible) return;
  $('historyList').innerHTML = listHtml(list);
  const days = list && Number(list.days);
  $('historyNote').textContent = days ? `Cada resumo fica ${days} ${days === 1 ? 'dia' : 'dias'} e depois some sozinho. Apagar tira também ` +
    'as legendas da conversa.' : '';
  $('historyClear').disabled = busy || !(list && list.items && list.items.length);
}

export function shown(on) {
  visible = on;
  if (on && (!list || list.error || stale)) load();
  render();
}

async function open(id) {
  try {
    openSummary(await api('GET', `/api/history/${encodeURIComponent(id)}`));
  } catch (e) {
    toast(`Resumo: ${e.message}`, true);
    if (e.status === 404) load();
  }
}

// DELETE até a API dizer que acabou (sem "more")
async function rounds(path) {
  let r = {};
  for (let i = 0; i < ROUNDS; i++) {
    const got = await api('DELETE', path);
    r = { ...got, deleted: (r.deleted || 0) + (got.deleted || 0), kept: got.kept };
    if (!got.more) return r;
  }
  throw new Error('demorou demais: tente de novo');
}

async function drop(id, title) {
  if (!await confirmDialog(`Apagar “${title}”?`, 'Apaga o resumo e as legendas dessa conversa. Não tem volta.', 'Apagar'))
    return;
  busy = true;
  render();
  try {
    await rounds(`/api/history/${encodeURIComponent(id)}`);
    toast('Resumo apagado');
  } catch (e) {
    toast(`Apagar: ${e.message}`, true);
  }
  busy = false;
  load();
}

async function clearAll() {
  const n = list && list.items ? list.items.length : 0;
  const what = list && list.truncated ? `todos os resumos (mais de ${n})` : n === 1 ? 'o resumo' : `os ${n} resumos`;
  if (!await confirmDialog('Apagar todo o histórico?', `Apaga ${what} e as ` +
                           'legendas das conversas. Não tem volta.', 'Apagar tudo'))
    return;
  busy = true;
  render();
  try {
    const r = await rounds('/api/history');
    toast(`${r.deleted} ${r.deleted === 1 ? 'resumo apagado' : 'resumos apagados'}` +
          (r.kept ? '; o da gravação em andamento ficou' : ''));
  } catch (e) {
    toast(`Apagar tudo: ${e.message}`, true);
  }
  busy = false;
  load();
}

function onClick(ev) {
  if (ev.target.closest('#historyReload')) { load(); return; }
  if (ev.target.closest('#historyClear')) { if (!busy) clearAll(); return; }
  const b = ev.target.closest('button[data-act]');
  const li = b && b.closest('li[data-id]');
  if (!li || b.disabled || busy) return;
  if (b.dataset.act === 'open') open(li.dataset.id);
  else if (b.dataset.act === 'drop') drop(li.dataset.id, li.dataset.title);
}

// Resumo novo ao vivo: o roteador grava o histórico logo depois de publicar
export function onMessage(ch, m, retained) {
  if (ch !== 'ui/summary' || !m || retained) return;
  if (visible) setTimeout(load, 1500);
  else stale = true;
}

$('tab-history').addEventListener('click', onClick);
