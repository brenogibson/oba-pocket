// Utilidades de tela compartilhadas pelas abas.

export const $ = id => document.getElementById(id);
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// O Oba ativo, como o roteador publica em ui/oba
let oba = { name: 'Oba', wake_words: [] };
export const current = () => oba;
export function setCurrent(o) { oba = o; }

let toastTimer = null;
export function toast(text, bad) {
  const t = $('toast');
  t.textContent = text;
  t.classList.toggle('bad', !!bad);
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('on'), bad ? 6000 : 3000);
}

// Confirmação num <dialog>; resolve true ou false. Com outra aberta, recusa: as duas
// esperariam o mesmo close, e um toque confirmaria as duas
export function confirmDialog(title, text, ok = 'Confirmar') {
  const d = $('confirm');
  if (d.open) return Promise.resolve(false);
  d.querySelector('h3').textContent = title;
  d.querySelector('p').textContent = text;
  d.querySelector('[value=ok]').textContent = ok;
  d.returnValue = '';
  d.showModal();
  return new Promise(res => d.addEventListener('close', () => res(d.returnValue === 'ok'), { once: true }));
}

export function ago(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return 'agora';
  if (s < 3600) return `há ${Math.round(s / 60)} min`;
  if (s < 86400) return `há ${Math.round(s / 3600)} h`;
  return new Date(ts).toLocaleDateString('pt-BR');
}
