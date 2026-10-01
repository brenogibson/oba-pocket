// Aba Enviar: um Oba novo (ou uma versão nova) em zip. A API confere como o
// tools/oba.py validate e põe no registro; daqui dá para instalar na placa em seguida.

import { api } from './auth.js';
import { $, esc, confirmDialog } from './ui.js';
import * as obas from './obas.js';

const ZIP_MAX = 4 * 1024 * 1024;     // o mesmo da API (portal/api/api.py)
let last = null;                     // o último Oba que subiu: {id, name}

function show(html) { $('uploadOut').innerHTML = html; }

async function send(file, replace) {
  show('<p class="muted">Enviando e conferindo…</p>');
  try {
    return await api('POST', `/api/obas${replace ? '?replace=1' : ''}`, file);
  } catch (e) {
    const d = e.detail || {};
    if (e.status === 409 && !replace) {
      const ok = await confirmDialog(`Substituir ${d.id}?`, `O registro já tem ${d.id}` +
        `${d.version ? ` na versão ${d.version}` : ''}. Os arquivos que saíram do Oba também saem do registro ` +
        '(o bucket guarda os antigos por 30 dias).', 'Substituir');
      if (ok) return send(file, true);
      show('<p class="muted">Nada mudou no registro.</p>');
      return null;
    }
    const problems = (d.problems || []).map(p => `<li>${esc(p)}</li>`).join('');
    show(`<p class="bad">${esc(e.message)}</p>${problems ? `<ul class="problems">${problems}</ul>` : ''}`);
    return null;
  }
}

async function onFile() {
  const input = $('zipInput'), file = input.files[0];
  input.value = '';                  // o mesmo arquivo de novo depois de corrigir
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) { show('<p class="bad">Escolha um arquivo .zip.</p>'); return; }
  if (file.size > ZIP_MAX) {
    show(`<p class="bad">O zip tem ${(file.size / 1024 / 1024).toFixed(1)} MB; pelo portal vai até 4 MB. ` +
         'Para Obas maiores, use no computador: <code>python3 tools/oba.py install &lt;pasta&gt;</code>.</p>');
    return;
  }
  $('zipBtn').disabled = true;
  const r = await send(file, false).finally(() => { $('zipBtn').disabled = false; });
  if (!r) return;
  last = { id: r.id, name: r.name || r.id };
  const what = r.new ? 'entrou no registro' : 'foi atualizado no registro';
  const parts = [`${r.sent} arquivo(s) enviado(s)`, `${r.kept} sem mudança`];
  if (r.deleted) parts.push(`${r.deleted} apagado(s)`);
  const ignored = (r.ignored || []).length
    ? `<p class="hint">Ficaram de fora (não estão em "files"): ${r.ignored.map(esc).join(', ')}</p>` : '';
  show(`<p><b>${esc(last.name)} ${esc(r.version || '')}</b> ${what}: ${parts.join(', ')}.</p>${ignored}
    <div class="actions"><button type="button" class="primary" data-act="install">Instalar na placa</button>
    <a class="button" href="#obas">Ver nos Obas</a></div>`);
  obas.load();
}

async function onClick(ev) {
  if (ev.target.closest('#zipBtn')) { $('zipInput').click(); return; }
  const b = ev.target.closest('button[data-act=install]');
  if (b && last && await obas.install(last.id, last.name)) location.hash = '#obas';
}

$('zipInput').addEventListener('change', onFile);
$('tab-upload').addEventListener('click', onClick);
