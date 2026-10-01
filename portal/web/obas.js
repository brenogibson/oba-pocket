// Aba Obas: o registro (S3) pela API, com a prévia de cada um e o que a placa já tem.
// Instalar passa pelo instalador (portal/api/installer.py), que conta o andamento em
// ui/install; a placa pergunta na tela antes de trocar de Oba.

import { api } from './auth.js';
import { drawOba } from './oba-render.js';
import { $, esc, toast, confirmDialog } from './ui.js';
import * as device from './device.js';

let list = null;            // o que a API devolveu em /api/obas
let loading = null;
let visible = false;
let stops = [];             // animações das prévias
let job = null;             // instalação em andamento: {cid, id, name, stage, sent, total, error}
let jobTimer = null;
let early = [];             // ui/install que chegou antes de o POST devolver o cid
const ID_RE = /^[a-z0-9_-]{1,31}$/;   // o que a rota DELETE aceita
const QUIET_MS = 110e3;     // sem notícia do instalador por isso: desiste (fila de 90 s + toque de 60 s)

export async function load() {
  if (loading) return loading;
  loading = api('GET', '/api/obas').then(r => { list = r; }).catch(e => {
    if (!list) list = { error: e.message };
    toast(`Obas: ${e.message}`, true);
  }).finally(() => { loading = null; render(); });
  return loading;
}

export function shown(on) {
  visible = on;
  if (!on) { stops.forEach(f => f()); stops = []; return; }
  if (!list || list.error) load();
  render();
}

function kb(n) {
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

// Como o Oba do registro está na placa: fora, igual, diferente (atualizar) e se é o ativo
function onBoard(o, st) {
  const have = st && (st.obas || []).find(x => x.id === o.id);
  const active = st && st.active && st.active.id === o.id ? st.active : null;
  if (!have && !active) return { here: false };
  const same = active && active.sha256 ? o.shas.includes(active.sha256)
    : (have || active).version === o.version;
  return { here: true, same, active: !!active, builtin: !!(have && have.builtin), version: (have || active).version };
}

function blocked(st) {
  if (!st) return 'esperando o estado da placa';
  if (st.online === false) return 'a placa está desconectada';
  if (st.rec) return 'com o REC ligado a placa não instala';
  if (job && job.stage !== 'done' && job.stage !== 'error') return 'outra instalação em andamento';
  return '';
}

function bar() {
  const pct = job.total ? Math.min(100, Math.round(job.sent * 100 / job.total)) : 0;
  const text = {
    queued: 'na fila…', start: 'preparando…', send: `enviando ${pct}%`,
    confirm: 'confirme na tela da placa (toque em “Instalar”)',
    done: job.active ? 'instalado e ativo' : 'instalado', error: job.error || 'falhou',
  }[job.stage] || '…';
  const width = job.stage === 'send' ? pct : ['confirm', 'done'].includes(job.stage) ? 100 : 0;
  return { text, width };
}

function progress(o) {
  if (!job || job.id !== o.id) return '';
  const { text, width } = bar();
  // A largura vai pelo CSSOM depois: o CSP não deixa style="" no HTML
  return `<div class="progress ${esc(job.stage)}" data-width="${width}"><i></i><span>${esc(text)}</span></div>`;
}

// Mesmo estágio: só a barra muda (refazer a lista reinicia as prévias e tira o foco)
function updateBar() {
  const el = visible && $('obasList').querySelector(`article[data-id="${CSS.escape(job.id)}"] .progress`);
  if (!el) return false;
  const { text, width } = bar();
  el.querySelector('i').style.width = `${width}%`;
  el.querySelector('span').textContent = text;
  return true;
}

function card(o, st, why) {
  if (o.broken) {
    // Id fora do padrão a API não apaga (é sobra no bucket): só pelo console do S3
    const act = ID_RE.test(o.id) ? '<button type="button" class="danger" data-act="drop">Tirar do registro</button>'
      : '<p class="hint">O nome da pasta não é um id válido: apague direto no bucket do registro.</p>';
    return `<article class="oba broken" data-id="${esc(o.id)}" data-name="${esc(o.id)}">
      <div class="pic"></div>
      <div class="meta"><h2>${esc(o.id)}</h2><p class="muted">Com problema no registro: ${esc(o.broken)}.</p></div>
      <div class="actions">${act}</div></article>`;
  }
  const b = onBoard(o, st);
  const badges = [];
  if (b.active) badges.push('<span class="tag">ativo</span>');
  else if (b.here) badges.push('<span class="tag soft">na placa</span>');
  if (b.here && !b.same) badges.push(`<span class="tag warn">atualizar${b.version ? ` (placa: ${esc(b.version)})` : ''}</span>`);
  if (o.missing && o.missing.length) badges.push('<span class="tag warn">arquivos faltando</span>');
  const missing = o.missing && o.missing.length ? 'faltam arquivos no registro: envie o Oba de novo' : '';
  const off = why || missing ? ` disabled title="${esc(why || missing)}"` : '';
  const noActivate = !st ? 'esperando o estado da placa' : st.online === false ? 'a placa está desconectada'
    : st.rec ? 'com o REC ligado a placa não troca de Oba' : '';
  const buttons = [];
  if (!b.here) buttons.push(`<button type="button" class="primary" data-act="install"${off}>Instalar</button>`);
  else if (!b.same) buttons.push(`<button type="button" class="primary" data-act="install"${off}>Atualizar</button>`);
  else if (!b.active) {
    buttons.push(`<button type="button" data-act="activate"${noActivate ? ` disabled title="${esc(noActivate)}"` : ''}>Ativar</button>`);
  }
  buttons.push('<button type="button" class="danger" data-act="drop">Tirar do registro</button>');
  return `<article class="oba" data-id="${esc(o.id)}" data-name="${esc(o.name)}">
    <div class="pic"></div>
    <div class="meta">
      <h2>${esc(o.name)} <small>${esc(o.version)}</small></h2>
      ${o.description ? `<p>${esc(o.description)}</p>` : ''}
      <div class="chips">${badges.join('')}${(o.requires || []).map(r => `<span class="chip">${esc(r)}</span>`).join('')}</div>
      <small>${esc(o.id)} · ${kb(o.size)} · ${o.count} arquivo(s)${o.author ? ` · ${esc(o.author)}` : ''}</small>
    </div>
    <div class="actions">${buttons.join('')}</div>
    ${progress(o)}</article>`;
}

export function render() {
  if (!visible) return;
  const el = $('obasList'), st = device.state();
  stops.forEach(f => f());
  stops = [];
  if (!list) { el.innerHTML = '<p class="empty">Carregando os Obas do registro…</p>'; return; }
  if (list.error) { el.innerHTML = `<p class="empty">Não deu para listar: ${esc(list.error)}</p>`; return; }
  if (!list.obas.length) { el.innerHTML = '<p class="empty">O registro está vazio. Mande um Oba na aba Enviar.</p>'; return; }
  const why = blocked(st);
  el.innerHTML = list.obas.map(o => card(o, st, why)).join('') +
    (list.truncated ? `<p class="hint">Mais ${list.truncated} Oba(s) no registro que não cabem aqui.</p>` : '');
  $('obasNote').textContent = why && !job ? `Instalar: ${why}.` : '';
  for (const o of list.obas) {
    const art = el.querySelector(`article[data-id="${CSS.escape(o.id)}"]`);
    if (!art || o.broken) continue;
    const pic = art.querySelector('.pic'), bar = art.querySelector('.progress');
    if (o.palette && o.palette.bg) pic.style.background = o.palette.bg;
    stops.push(drawOba(pic, o, 76));
    if (bar) bar.querySelector('i').style.width = `${bar.dataset.width}%`;
  }
}

function setJob(j) {
  const prev = job;
  job = j;
  clearTimeout(jobTimer);
  if (job && !['done', 'error'].includes(job.stage)) {
    jobTimer = setTimeout(() => setJob({ ...job, stage: 'error', error: 'o instalador não deu notícia' }), QUIET_MS);
  } else if (job) {
    const cid = job.cid;
    jobTimer = setTimeout(() => { if (job && job.cid === cid) { job = null; render(); } }, 8000);
  }
  if (prev && job && prev.id === job.id && prev.stage === job.stage && updateBar()) return;
  render();
}

export async function install(id, name) {
  const why = blocked(device.state());
  if (why) { toast(`Instalar ${name}: ${why}`, true); return false; }
  if (!await confirmDialog(`Instalar ${name}?`, 'O portal manda o Oba para a placa. Quando terminar, confirme ' +
                           'na tela dela (toque em “Instalar” em até 60 s); depois ele vira o Oba ativo.', 'Instalar'))
    return false;
  const again = blocked(device.state());      // a placa pode ter mudado enquanto o diálogo estava aberto
  if (again) { toast(`Instalar ${name}: ${again}`, true); return false; }
  early = [];
  setJob({ cid: null, id, name, stage: 'queued', sent: 0, total: 0 });
  try {
    const { cid } = await api('POST', '/api/device/install', { id, activate: true });
    if (job && job.id === id && !job.cid) {
      const got = early.filter(m => m.cid === cid);   // de outra instalação do mesmo Oba não serve
      early = [];
      setJob({ ...job, cid });
      got.forEach(m => onMessage('ui/install', m));
    }
  } catch (e) {
    setJob({ ...job, stage: 'error', error: e.message });
    toast(`Instalar ${name}: ${e.message}`, true);
  }
  return true;
}

async function drop(id, name, active) {
  const extra = active ? ' Como é o Oba ativo, a placa fica sem agente até ele voltar para o registro.' : '';
  if (!await confirmDialog(`Tirar ${name} do registro?`, 'Apaga do registro S3 (o bucket guarda a versão ' +
                           `antiga por 30 dias). A placa continua com ele.${extra}`, 'Tirar'))
    return;
  try {
    await api('DELETE', `/api/obas/${encodeURIComponent(id)}`);
    toast(`${name} saiu do registro`);
  } catch (e) {
    toast(`Tirar ${name}: ${e.message}`, true);
  }
  load();
}

async function onClick(ev) {
  if (ev.target.closest('#obasReload')) { load(); return; }
  const b = ev.target.closest('button[data-act]');
  if (!b || b.disabled) return;
  const art = b.closest('article[data-id]');
  const id = art.dataset.id, nm = art.dataset.name;
  const st = device.state();
  switch (b.dataset.act) {
    case 'install': install(id, nm); break;
    case 'activate': device.command({ type: 'oba.activate', target: id }, `Ativar ${nm}`, true); break;
    case 'drop': drop(id, nm, !!(st && st.active && st.active.id === id)); break;
  }
}

export function onMessage(ch, m) {
  if (ch !== 'ui/install' || !m || !job || job.stage === 'done') return;
  if (!job.cid) {                             // antes do cid chegar: guarda para conferir depois
    if (m.id === job.id && early.length < 50) early.push(m);
    return;
  }
  if (m.cid !== job.cid) return;
  if (m.stage === 'send' && job.stage === 'confirm') return;
  const next = { ...job, stage: m.stage, sent: m.sent ?? job.sent, total: m.total ?? job.total,
                 error: m.error, active: m.active };
  if (m.stage === 'done') toast(`${job.name}: ${m.active ? 'instalado e ativo' : 'instalado'}`);
  if (m.stage === 'error') toast(`${job.name}: ${m.error || 'a instalação falhou'}`, true);
  setJob(next);
}

// Sem o MQTT na hora do toque, o "done" se perde; o state retido da volta mostra o Oba novo ativo
function doneByState(st) {
  if (!job || !['send', 'confirm'].includes(job.stage) || !st || !st.active || st.active.id !== job.id) return false;
  const o = list && list.obas && list.obas.find(x => x.id === job.id);
  if (!o || !o.shas || !o.shas.includes(st.active.sha256)) return false;
  toast(`${job.name}: instalado e ativo`);
  setJob({ ...job, stage: 'done', active: true });
  return true;
}

$('tab-obas').addEventListener('click', onClick);
device.onChange(st => { if (!doneByState(st)) render(); });
