import {
  parseQuery, matchesPrefix, replay, moveLabel, toPgn, boardSvg,
  withTimestamp, fmtTime, topRating, matchesMinRating, matchesPov, matchesSpeedrun,
  narratorSide, trustedView,
} from './lib.js';
import { Chess } from './vendor/chess.js';
import { Chessground } from './vendor/chessground/chessground.min.js';
import { analyse, fmtScore, DEPTH } from './engine.js';

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
  sboard: document.getElementById('sboard'),
  sbBack: document.getElementById('sb-back'),
  sbReset: document.getElementById('sb-reset'),
  sbFlip: document.getElementById('sb-flip'),
  sbArrows: document.getElementById('sb-arrows'),
  sbNext: document.getElementById('sb-next'),
};

const NEXT_SHOWN = 8;   // database moves shown as arrows / in the list
let board = null;       // the search board (chessground)

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

  board = Chessground(els.sboard, {
    movable: { free: false, color: 'white', showDests: true, events: { after: onBoardMove } },
    premovable: { enabled: false },
    drawable: { enabled: true, visible: true },
    highlight: { lastMove: true, check: true },
  });
  els.sbBack.addEventListener('click', () => setQuerySans(parseQuery(els.query.value).sans.slice(0, -1)));
  els.sbReset.addEventListener('click', () => setQuerySans([]));
  els.sbFlip.addEventListener('click', () => board.toggleOrientation());
  els.sbArrows.addEventListener('change', render);
  els.sbNext.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-san]');
    if (b) setQuerySans([...parseQuery(els.query.value).sans, b.dataset.san]);
  });

  readHash();
  render();
}

// --- the search board: the moves played on it are the search ---

function setQuerySans(sans) {
  els.query.value = toPgn(sans);
  render();
}

function onBoardMove(orig, dest) {
  const { sans } = parseQuery(els.query.value);
  const chess = new Chess();
  for (const san of sans) chess.move(san);
  let mv = null;
  try { mv = chess.move({ from: orig, to: dest, promotion: 'q' }); } catch { mv = null; }
  if (mv) setQuerySans([...sans, mv.san]);
  else render();      // not legal after all: put the board back
}

function legalDests(chess) {
  const dests = new Map();
  for (const m of chess.moves({ verbose: true })) {
    if (!dests.has(m.from)) dests.set(m.from, []);
    dests.get(m.from).push(m.to);
  }
  return dests;
}

/** The search board at the position after `sans`, with arrows for the
 *  moves the matching games played next (thicker for more games). */
function drawSearchBoard(sans, matched) {
  const chess = new Chess();
  let last = null;
  for (const san of sans) last = chess.move(san);
  const turn = chess.turn() === 'w' ? 'white' : 'black';
  const counts = new Map();
  for (const g of matched) {
    const nxt = g.moves_san[sans.length];
    if (nxt) counts.set(nxt, (counts.get(nxt) || 0) + 1);
  }
  const next = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, NEXT_SHOWN);
  const top = next.length ? next[0][1] : 1;
  const shapes = [];
  if (els.sbArrows.checked) {
    for (const [san, n] of next) {
      const probe = new Chess(chess.fen());
      let mv = null;
      try { mv = probe.move(san); } catch { mv = null; }
      if (mv) shapes.push({ orig: mv.from, dest: mv.to, brush: 'blue',
                            modifiers: { lineWidth: Math.round(4 + 11 * n / top) } });
    }
  }
  board.set({
    fen: chess.fen(),
    turnColor: turn,
    lastMove: last ? [last.from, last.to] : undefined,
    check: chess.inCheck(),
    movable: { color: turn, dests: legalDests(chess) },
  });
  board.setAutoShapes(shapes);
  els.sbNext.innerHTML = next.map(([san, n]) =>
    `<li><button type="button" data-san="${escapeHtml(san)}">${escapeHtml(san)}</button> <span>${n} game${n === 1 ? '' : 's'}</span></li>`).join('');
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
  drawSearchBoard(sans, matched);
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
    if (!d.open) d.querySelector('.detail')?.dispatchEvent(new Event('engine-close'));
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
  const fens = [new Chess().fen()];
  { const c = new Chess(); for (const san of g.moves_san) { c.move(san); fens.push(c.fen()); } }
  let onPosition = () => {};   // the engine panel's hook, set below
  const draw = () => {
    onPosition();
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
  info.append(enginePanel(d => (onPosition = d), () => fens[ply], wrap));
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

// --- the engine panel: off until ticked; one analysis at a time on the page ---

let engineOwner = null;   // the panel whose checkbox is on

function enginePanel(setHook, fen, root) {
  const panel = document.createElement('div');
  panel.className = 'engine';
  panel.innerHTML = `<label class="check"><input type="checkbox"> Engine <span class="dim">(Stockfish, depth ${DEPTH})</span></label>
    <div class="engine-out" hidden><span class="score"></span> <span class="depth dim"></span><div class="pv"></div></div>`;
  const box = panel.querySelector('input');
  const out = panel.querySelector('.engine-out');
  let stop = () => {};
  const run = () => {
    stop();
    if (!box.checked) { out.hidden = true; return; }
    out.hidden = false;
    out.querySelector('.score').textContent = '…';
    out.querySelector('.depth').textContent = 'starting the engine';
    out.querySelector('.pv').textContent = '';
    const f = fen();
    const c = new Chess(f);
    if (c.isGameOver()) {
      out.querySelector('.score').textContent = c.isCheckmate() ? 'checkmate' : 'draw';
      out.querySelector('.depth').textContent = '';
      return;
    }
    stop = analyse(f, (info) => {
      out.querySelector('.score').textContent = fmtScore(info);
      out.querySelector('.depth').textContent = `depth ${info.depth}${info.done ? '' : '…'}`;
      out.querySelector('.pv').textContent = pvText(f, info.pv);
    });
  };
  box.addEventListener('change', () => {
    if (box.checked) {
      if (engineOwner && engineOwner !== box) { engineOwner.checked = false; engineOwner.dispatchEvent(new Event('change')); }
      engineOwner = box;
    } else if (engineOwner === box) {
      engineOwner = null;
    }
    run();
  });
  setHook(() => { if (box.checked) run(); });
  // stop analysing when the game's card is closed
  root.addEventListener('engine-close', () => { if (box.checked) { box.checked = false; box.dispatchEvent(new Event('change')); } });
  return panel;
}

function pvText(fen, sans) {
  const [, side, , , , full] = fen.split(' ');
  let n = Number(full);
  let out = side === 'b' ? `${n}... ` : '';
  sans.forEach((san, i) => {
    const white = (side === 'w') === (i % 2 === 0);
    if (white) out += `${n}. `;
    out += san + ' ';
    if (!white) n++;
  });
  return out.trim();
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
