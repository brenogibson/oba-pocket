// O resumo de uma conversa (ui/summary), na aba Reunião e no Histórico. Tudo que vem do
// servidor passa por esc(), e link só http ou https.

import { $, esc, current } from './ui.js';

const arr = v => Array.isArray(v) ? v : [];
const list = (title, items) => (items = items.filter(Boolean)).length
  ? `<div><h4>${esc(title)}</h4><ul>${items.map(x => `<li>${x}</li>`).join('')}</ul></div>` : '';
const host = u => { try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.hostname : ''; } catch { return ''; } };

// "3 frases · "; sem contagem (resumo recuperado), nada
export const frases = v => { const n = Number(v) || 0; return n ? `${n} ${n === 1 ? 'frase' : 'frases'} · ` : ''; };

// Hoje, só a hora; antes, a data também
export function when(ts) {
  const d = new Date(Number(ts) || Date.now());
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' });
}

export function summaryHtml(s) {
  const o = current();
  const by = !s.oba || s.oba === o.id ? `Sugestões do ${o.name}` : 'Sugestões';
  const sugg = arr(s.suggestions).map(x => `${esc(x && x.title)}` +
    (x && x.url && host(x.url) ? ` — <a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(host(x.url))}</a>` : ''));
  return `<div class="tools"><button class="download" type="button">Baixar .md</button>` +
    `<button class="close" type="button">Fechar</button></div>` +
    `<small>Resumo da conversa · ${frases(s.lines)}${esc(when(s.ts))}</small>` +
    `<h2>${esc(s.title || 'Resumo')}</h2><p class="text">${esc(s.summary || '')}</p>` +
    `<div class="cols">${list('Assuntos', arr(s.topics).map(esc))}${list('Decisões', arr(s.decisions).map(esc))}` +
    `${list('Próximos passos', arr(s.next_steps).map(esc))}${list(by, sugg)}</div>`;
}

// ------------------------------------------------------------ baixar em Markdown

const line = v => String(v ?? '').replace(/\s+/g, ' ').trim();
const mdList = (title, items) => (items = items.filter(Boolean)).length
  ? `## ${title}\n\n${items.map(x => `- ${x}`).join('\n')}\n\n` : '';

export function summaryMd(s, o = current()) {
  const d = new Date(Number(s.ts) || Date.now());
  const date = d.toLocaleString('pt-BR', { dateStyle: 'long', timeStyle: 'short' });
  const n = Number(s.lines) || 0;
  const sugg = arr(s.suggestions).filter(x => x && x.title).map(x => {
    const t = line(x.title);
    return x.url && host(x.url) ? `[${t.replace(/([[\]])/g, '\\$1')}](${x.url.replace(/[()<>\s]/g, c => '%' + c.charCodeAt(0).toString(16).padStart(2, '0').toUpperCase())})` : t;
  });
  return `# ${line(s.title) || 'Resumo'}\n\n${date}${n ? ` · ${n} ${n === 1 ? 'frase' : 'frases'}` : ''}\n\n` +
    `${String(s.summary || '').trim()}\n\n` +
    mdList('Assuntos', arr(s.topics).map(line)) + mdList('Decisões', arr(s.decisions).map(line)) +
    mdList('Próximos passos', arr(s.next_steps).map(line)) +
    mdList(!s.oba || s.oba === o.id ? `Sugestões do ${o.name}` : 'Sugestões', sugg);
}

// resumo-2026-10-06-1503-titulo-curto.md
export function summaryFile(s) {
  const d = new Date(Number(s.ts) || Date.now()), p = n => String(n).padStart(2, '0');
  const slug = line(s.title).normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 50).replace(/-+$/, '');
  return `resumo-${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}` +
    `${slug ? `-${slug}` : ''}.md`;
}

function download(s) {
  const url = URL.createObjectURL(new Blob([summaryMd(s)], { type: 'text/markdown;charset=utf-8' }));
  const a = Object.assign(document.createElement('a'), { href: url, download: summaryFile(s) });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

let open = null;            // o resumo aberto na tela
export const showing = () => open;

export function openSummary(s) {
  open = s;
  $('summary').querySelector('article').innerHTML = summaryHtml(s);
  $('summary').classList.add('on');
}

export function closeSummary() {
  open = null;
  $('summary').classList.remove('on');
}

$('summary').addEventListener('click', e => { if (open && e.target.closest('.download')) download(open); });
