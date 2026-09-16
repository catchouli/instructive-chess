import {
  parseQuery, matchesPrefix, replay, moveLabel, toPgn, boardSvg,
  withTimestamp, fmtTime,
} from './lib.js';

const DEFAULT_LIMIT = 20;   // games shown when the search box is empty
const RESULT_LIMIT = 50;    // cap on a filtered result set

const els = {
  query: document.getElementById('query'),
  clear: document.getElementById('clear'),
  status: document.getElementById('status'),
  results: document.getElementById('results'),
};

let GAMES = [];

init();

async function init() {
  try {
    const res = await fetch('data/games.json', { cache: 'no-cache' });
    GAMES = await res.json();
  } catch {
    GAMES = [];
  }
  GAMES.sort(byNewest);

  els.query.addEventListener('input', debounce(render, 130));
  els.clear.addEventListener('click', () => { els.query.value = ''; render(); els.query.focus(); });

  const q = decodeURIComponent(location.hash.replace(/^#q=/, ''));
  if (q) els.query.value = q;
  render();
}

function byNewest(a, b) {
  const d = String(b.published || '').localeCompare(String(a.published || ''));
  return d || (a.start_seconds || 0) - (b.start_seconds || 0);
}

function render() {
  const raw = els.query.value;
  history.replaceState(null, '', raw ? '#q=' + encodeURIComponent(raw) : location.pathname + location.search);
  const { sans, ok, bad } = parseQuery(raw);

  const matched = GAMES.filter((g) => matchesPrefix(g, sans));
  const total = matched.length;
  const limit = sans.length === 0 ? DEFAULT_LIMIT : RESULT_LIMIT;
  const shown = matched.slice(0, limit);

  if (!GAMES.length) {
    els.status.textContent = 'No games in the catalog yet — run the video scanner to add some.';
  } else if (!ok) {
    els.status.innerHTML = `Illegal move <span class="bad">${escapeHtml(bad)}</span> — filtering on the ${sans.length} legal move${sans.length === 1 ? '' : 's'} before it.`;
  } else if (sans.length === 0) {
    els.status.textContent = `${GAMES.length} game${GAMES.length === 1 ? '' : 's'} in the catalog · showing the ${Math.min(DEFAULT_LIMIT, total)} most recent`;
  } else {
    els.status.textContent = `${total} game${total === 1 ? '' : 's'} start with ${sans.join(' ')}` +
      (total > shown.length ? ` · showing the first ${shown.length}` : '');
  }

  els.results.innerHTML = '';
  if (!shown.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = GAMES.length ? 'No games match those moves.' : '';
    els.results.append(li);
    return;
  }
  for (const g of shown) els.results.append(card(g, sans.length));
}

function card(g, hl) {
  const li = document.createElement('li');
  const d = document.createElement('details');
  d.className = 'card';

  const conf = g.confidence ?? 0;
  const cls = conf >= 0.9 ? 'good' : conf >= 0.75 ? 'ok' : 'weak';
  const summary = document.createElement('summary');
  summary.innerHTML = `
    <span class="title">${escapeHtml(g.video_title || g.video_id)}</span>
    <span class="badges">
      <span class="pill">${g.ply} ply</span>
      <span class="pill ${cls}" title="reconstruction confidence">${Math.round(conf * 100)}%</span>
    </span>
    <span class="meta">${escapeHtml(g.channel || 'Unknown')}${g.published ? ' · ' + g.published : ''}</span>
    <span class="moves-inline">${inlineMoves(g.moves_san, hl)}</span>`;
  d.append(summary);

  d.addEventListener('toggle', () => {
    if (d.open && !d.dataset.built) { d.append(detail(g)); d.dataset.built = '1'; }
  });

  li.append(d);
  return li;
}

function inlineMoves(sans, hl, max = 16) {
  let out = '';
  for (let i = 0; i < Math.min(sans.length, max); i++) {
    if (i % 2 === 0) out += `${i / 2 + 1}. `;
    const mv = escapeHtml(sans[i]);
    out += (i < hl ? `<mark>${mv}</mark>` : mv) + ' ';
  }
  if (sans.length > max) out += '…';
  return out.trim();
}

function detail(g) {
  const wrap = document.createElement('div');
  wrap.className = 'detail';
  const grid = document.createElement('div');
  grid.className = 'detail-grid';

  // board + stepper
  const boardCol = document.createElement('div');
  const board = document.createElement('div');
  board.className = 'board';
  const nav = document.createElement('div');
  nav.className = 'nav';
  const prev = button('‹ prev');
  const next = button('next ›');
  const label = document.createElement('span');
  nav.append(prev, next, label);
  const moveWatch = document.createElement('a');
  moveWatch.className = 'move-watch';
  moveWatch.target = '_blank';
  moveWatch.rel = 'noopener';
  boardCol.append(board, nav, moveWatch);

  const positions = replay(g.moves_san);
  const hasTimes = Array.isArray(g.move_seconds) && g.move_seconds.length === g.moves_san.length;
  let ply = g.moves_san.length;
  const draw = () => {
    board.innerHTML = boardSvg(positions[ply]);
    label.textContent = ply === 0 ? 'start position' : `after ${moveLabel(g.moves_san, ply)}`;
    prev.disabled = ply === 0;
    next.disabled = ply === g.moves_san.length;
    if (hasTimes) {
      const t = ply === 0 ? g.start_seconds : g.move_seconds[ply - 1];
      moveWatch.href = withTimestamp(g.video_url, t);
      moveWatch.textContent = `▶ watch this position (${fmtTime(t)})`;
      moveWatch.hidden = false;
    } else {
      moveWatch.hidden = true;
    }
  };
  prev.onclick = () => { ply = Math.max(0, ply - 1); draw(); };
  next.onclick = () => { ply = Math.min(g.moves_san.length, ply + 1); draw(); };
  draw();

  // pgn + actions
  const info = document.createElement('div');
  const pgn = document.createElement('div');
  pgn.className = 'pgn';
  pgn.textContent = toPgn(g.moves_san);

  const actions = document.createElement('div');
  actions.className = 'detail-actions';
  const watch = document.createElement('a');
  watch.className = 'primary';
  watch.href = withTimestamp(g.video_url, g.start_seconds);
  watch.target = '_blank';
  watch.rel = 'noopener';
  watch.textContent = g.start_seconds ? `Watch from ${fmtTime(g.start_seconds)}` : 'Watch on YouTube';
  const copy = button('Copy PGN');
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(toPgn(g.moves_san)); copy.textContent = 'Copied ✓'; }
    catch { copy.textContent = 'Copy failed'; }
    setTimeout(() => (copy.textContent = 'Copy PGN'), 1500);
  };
  actions.append(watch, copy);
  info.append(pgn, actions);

  grid.append(boardCol, info);
  wrap.append(grid);
  return wrap;
}

function button(text) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = text;
  return b;
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
