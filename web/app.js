import {
  parseQuery, matchesPrefix, replay, moveLabel, toPgn, boardSvg,
  withTimestamp, fmtTime, topRating, matchesMinRating, matchesPov, matchesSpeedrun,
  narratorSide, trustedView,
} from './lib.js';

const DEFAULT_LIMIT = 20;   // games shown when the search box is empty
const RESULT_LIMIT = 50;    // cap on a filtered result set

const els = {
  query: document.getElementById('query'),
  clear: document.getElementById('clear'),
  minRating: document.getElementById('min-rating'),
  pov: document.getElementById('pov'),
  noSpeedrun: document.getElementById('no-speedrun'),
  status: document.getElementById('status'),
  results: document.getElementById('results'),
};

let GAMES = [];

init();

async function init() {
  try {
    const res = await fetch('data/games.json', { cache: 'no-cache' });
    // only the part of each game the scanner is sure of (see trustedView)
    GAMES = (await res.json()).map(trustedView).filter(Boolean);
  } catch {
    GAMES = [];
  }
  GAMES.sort(byNewest);

  els.query.addEventListener('input', debounce(render, 130));
  els.clear.addEventListener('click', () => { els.query.value = ''; render(); els.query.focus(); });
  els.minRating.addEventListener('input', debounce(render, 130));
  els.pov.addEventListener('change', render);
  els.noSpeedrun.addEventListener('change', render);

  readHash();
  render();
}

// The search and filters live in the URL hash (#q=...&pov=white&min=2000&nospeedrun=1)
// so a search can be bookmarked or shared.
function readHash() {
  const raw = location.hash.replace(/^#/, '');
  const params = new URLSearchParams(raw.startsWith('q=') || raw.includes('&') ? raw : 'q=' + raw);
  els.query.value = params.get('q') || '';
  els.pov.value = ['white', 'black'].includes(params.get('pov')) ? params.get('pov') : '';
  els.minRating.value = /^\d+$/.test(params.get('min') || '') ? params.get('min') : '';
  els.noSpeedrun.checked = params.get('nospeedrun') === '1';
}

function writeHash() {
  const params = new URLSearchParams();
  if (els.query.value) params.set('q', els.query.value);
  if (els.pov.value) params.set('pov', els.pov.value);
  if (els.minRating.value !== '') params.set('min', els.minRating.value);
  if (els.noSpeedrun.checked) params.set('nospeedrun', '1');
  const h = params.toString();
  history.replaceState(null, '', h ? '#' + h : location.pathname + location.search);
}

function byNewest(a, b) {
  const d = String(b.published || '').localeCompare(String(a.published || ''));
  return d || (a.start_seconds || 0) - (b.start_seconds || 0);
}

function render() {
  const raw = els.query.value;
  writeHash();
  const { sans, ok, bad } = parseQuery(raw);
  const minRating = els.minRating.value === '' ? null : Number(els.minRating.value);
  const pov = els.pov.value || null;
  const noSpeedrun = els.noSpeedrun.checked;

  const matched = GAMES.filter((g) => matchesPrefix(g, sans) && matchesMinRating(g, minRating)
    && matchesPov(g, pov) && matchesSpeedrun(g, noSpeedrun));
  const total = matched.length;
  const limit = sans.length === 0 ? DEFAULT_LIMIT : RESULT_LIMIT;
  const shown = matched.slice(0, limit);
  const ratingNote = (minRating != null ? ` · both rated ${minRating}+` : '')
    + (pov ? ` · narrator plays ${pov}` : '')
    + (noSpeedrun ? ' · no speedruns' : '');

  if (!GAMES.length) {
    els.status.textContent = 'No games in the catalog yet — run the video scanner to add some.';
  } else if (!ok) {
    els.status.innerHTML = `Illegal move <span class="bad">${escapeHtml(bad)}</span> — filtering on the ${sans.length} legal move${sans.length === 1 ? '' : 's'} before it.${ratingNote}`;
  } else if (sans.length === 0) {
    els.status.textContent = `${total} game${total === 1 ? '' : 's'} in the catalog${ratingNote} · showing the ${Math.min(DEFAULT_LIMIT, total)} most recent`;
  } else {
    els.status.textContent = `${total} game${total === 1 ? '' : 's'} start with ${sans.join(' ')}${ratingNote}` +
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
  const rating = topRating(g);
  const ratingPill = rating != null
    ? `<span class="pill" title="higher-rated side (approximate - read from video)">~${rating}</span>` : '';
  const speedrunPill = g.speedrun ? '<span class="pill speedrun" title="the narrator climbing a fresh account against weaker opponents">speedrun</span>' : '';
  const side = narratorSide(g);
  const sideNote = side ? ` plays <span class="pov">${side === 'white' ? '♔ White' : '♚ Black'}</span>` : '';
  const summary = document.createElement('summary');
  summary.innerHTML = `
    <span class="title">${escapeHtml(g.video_title || g.video_id)}</span>
    <span class="badges">
      <span class="pill">${g.ply} ply</span>
      <span class="pill ${cls}" title="reconstruction confidence">${Math.round(conf * 100)}%</span>
      ${ratingPill}
      ${speedrunPill}
    </span>
    <span class="meta">${escapeHtml(g.channel || 'Unknown')}${sideNote}${g.published ? ' · ' + g.published : ''}</span>
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
  const first = button('«');
  first.title = 'start of the game';
  const prev = button('‹ prev');
  const next = button('next ›');
  const last = button('»');
  last.title = 'end of the game';
  const label = document.createElement('span');
  nav.append(first, prev, next, last, label);
  const moveWatch = document.createElement('a');
  moveWatch.className = 'move-watch';
  moveWatch.target = '_blank';
  moveWatch.rel = 'noopener';
  const clockLabel = document.createElement('div');
  clockLabel.className = 'clock-label';
  const ratingLabel = document.createElement('div');
  ratingLabel.className = 'clock-label';
  const narrator = narratorSide(g);
  const who = (side) => (side === narrator ? `${escapeHtml(g.channel || 'narrator')} (${side})` : side);
  if (g.rating_white != null || g.rating_black != null) {
    const parts = [];
    if (g.rating_white != null) parts.push(`${who('White')} ${g.rating_white}`);
    if (g.rating_black != null) parts.push(`${who('Black')} ${g.rating_black}`);
    ratingLabel.textContent = `♟ ${parts.join(' · ')} (approximate)`;
  } else if (narrator) {
    ratingLabel.textContent = `♟ ${g.channel || 'Narrator'} plays ${narrator}`;
  } else {
    ratingLabel.hidden = true;
  }
  boardCol.append(board, nav, clockLabel, ratingLabel, moveWatch);

  const positions = replay(g.moves_san);
  const hasTimes = Array.isArray(g.move_seconds) && g.move_seconds.length === g.moves_san.length;
  const hasClocks = (Array.isArray(g.clock_white_seconds) && g.clock_white_seconds.length === g.moves_san.length) ||
    (Array.isArray(g.clock_black_seconds) && g.clock_black_seconds.length === g.moves_san.length);
  let ply = g.moves_san.length;
  const draw = () => {
    board.innerHTML = boardSvg(positions[ply]);
    label.textContent = ply === 0 ? 'start position' : `after ${moveLabel(g.moves_san, ply)}`;
    first.disabled = prev.disabled = ply === 0;
    next.disabled = last.disabled = ply === g.moves_san.length;
    for (const el of pgn.querySelectorAll('.ply')) el.classList.toggle('current', Number(el.dataset.ply) === ply);
    if (hasTimes) {
      const t = ply === 0 ? g.start_seconds : g.move_seconds[ply - 1];
      moveWatch.href = withTimestamp(g.video_url, t);
      moveWatch.textContent = `▶ watch this position (${fmtTime(t)})`;
      moveWatch.hidden = false;
    } else {
      moveWatch.hidden = true;
    }
    if (hasClocks && ply > 0) {
      const w = g.clock_white_seconds?.[ply - 1];
      const b = g.clock_black_seconds?.[ply - 1];
      const parts = [];
      if (w != null) parts.push(`White ${fmtTime(w)}`);
      if (b != null) parts.push(`Black ${fmtTime(b)}`);
      clockLabel.textContent = parts.length ? `⏱ ${parts.join(' · ')}` : '';
      clockLabel.hidden = parts.length === 0;
    } else {
      clockLabel.hidden = true;
    }
  };
  const go = (p) => { ply = Math.max(0, Math.min(g.moves_san.length, p)); draw(); };
  first.onclick = () => go(0);
  prev.onclick = () => go(ply - 1);
  next.onclick = () => go(ply + 1);
  last.onclick = () => go(g.moves_san.length);

  // pgn (each move jumps the board to the position after it) + actions
  const info = document.createElement('div');
  const pgn = document.createElement('div');
  pgn.className = 'pgn';
  pgn.innerHTML = g.moves_san.map((san, i) =>
    `${i % 2 === 0 ? `${i / 2 + 1}. ` : ''}<button type="button" class="ply" data-ply="${i + 1}">${escapeHtml(san)}</button>`).join(' ');
  pgn.addEventListener('click', (e) => {
    const el = e.target.closest('.ply');
    if (el) go(Number(el.dataset.ply));
  });
  draw();

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
