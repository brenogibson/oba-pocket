// Desenho de um Oba no navegador, a partir do que o roteador publica em ui/oba (e a API
// devolve em /api/obas): rig vetorial (palette, outline, eyes) ou a folha idle dos sprites.

// Cor da paleta escurecida para o fundo e os painéis
export function shade(color, k) {
  const n = parseInt(color.slice(1), 16);
  return '#' + [16, 8, 0].map(s => Math.round(((n >> s) & 255) * k).toString(16).padStart(2, '0')).join('');
}

export function obaSvg(o) {
  const p = o.palette, e = o.eyes, pts = o.outline;
  const xs = pts.map(q => q[0]), ys = pts.map(q => q[1]);
  const x0 = Math.min(...xs), y0 = Math.min(...ys), w = Math.max(...xs) - x0, h = Math.max(...ys) - y0;
  const m = Math.max(w, h) * 0.06, side = Math.max(w, h) + 2 * m;
  const eye = x => `<ellipse cx="${x}" cy="${e.y}" rx="${e.rx || 8}" ry="${e.ry || 12}" fill="${p.eye}"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${x0 + w / 2 - side / 2} ${y0 + h / 2 - side / 2} ${side} ${side}">` +
    `<polygon points="${pts.map(q => q.join(',')).join(' ')}" fill="${p.body}"/>${eye(e.left)}${eye(e.right)}</svg>`;
}

// Anima a folha idle (tira horizontal de quadros, docs/oba.md) num canvas do tamanho
// pedido. Devolve uma função que para a animação.
export function spriteCanvas(o, box, onFirstFrame) {
  const sp = o.sprites, [w, h] = sp.size || [];
  const fps = Math.min(30, Math.max(1, Number(sp.fps) || 6));
  let stop = false, timer = null;
  if (!(w > 0 && h > 0) || !sp.sheet) return () => {};
  const img = new Image();
  img.onload = () => {
    if (stop) return;
    const n = Math.max(1, Math.floor(img.width / w)), fit = Math.max(w, h);
    const k = fit <= box ? Math.floor(box / fit) : box / fit;   // escala inteira quando cabe
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    c.style.width = `${w * k}px`; c.style.height = `${h * k}px`;
    c.className = 'pixel';
    const g = c.getContext('2d'), t0 = performance.now();
    let last = -1;
    const draw = () => {
      const f = Math.floor((performance.now() - t0) * fps / 1000) % n;
      if (f === last) return;
      last = f;
      g.clearRect(0, 0, w, h);
      g.drawImage(img, f * w, 0, w, h, 0, 0, w, h);
    };
    draw();
    if (n > 1) timer = setInterval(draw, 500 / fps);
    if (onFirstFrame) onFirstFrame(c, img);
  };
  img.src = 'data:image/png;base64,' + sp.sheet;
  return () => { stop = true; clearInterval(timer); };
}

// Coloca o Oba em el (rig ou sprites). Devolve a função que para a animação.
export function drawOba(el, o, box) {
  if (o.sprites && o.sprites.sheet) {
    el.replaceChildren();
    return spriteCanvas(o, box, c => el.replaceChildren(c));
  }
  if (o.outline && o.eyes && o.palette) el.innerHTML = obaSvg(o);
  else el.replaceChildren();                // folha grande demais ou Oba sem desenho: só a cor
  return () => {};
}

// Favicon: o Oba sobre o fundo da paleta
export function faviconFor(o, link) {
  const [w, h] = (o.sprites && o.sprites.size) || [];
  if (o.sprites && o.sprites.sheet && w > 0 && h > 0) {
    const img = new Image();
    img.onload = () => {
      const fit = Math.max(w, h), q = fit <= 56 ? Math.floor(56 / fit) : 56 / fit;
      const ic = document.createElement('canvas');
      ic.width = ic.height = 64;
      const x = ic.getContext('2d');
      x.fillStyle = (o.palette || {}).bg || '#000000';
      x.beginPath();
      if (x.roundRect) x.roundRect(0, 0, 64, 64, 14); else x.rect(0, 0, 64, 64);
      x.fill();
      x.imageSmoothingEnabled = false;
      x.drawImage(img, 0, 0, w, h, (64 - w * q) / 2, (64 - h * q) / 2, w * q, h * q);
      link.href = ic.toDataURL('image/png');
    };
    img.src = 'data:image/png;base64,' + o.sprites.sheet;
  } else if (o.outline && o.eyes && o.palette) {
    const icon = obaSvg(o).replace('<svg ', `<svg style="background:${o.palette.bg};border-radius:22%" `);
    link.href = 'data:image/svg+xml,' + encodeURIComponent(icon);
  } else {
    link.href = 'favicon.png';
  }
}
