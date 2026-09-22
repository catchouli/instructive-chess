// Pure helpers for the catalog UI. No DOM access here so this module can be
// unit-tested under Node (see lib.test.mjs).
import { Chess } from './vendor/chess.js';

/** Parse a PGN / move-list string into a normalised SAN array.
 *  Returns { sans, ok, bad } - on an illegal move, `sans` holds the legal
 *  prefix and `bad` names the offending token. */
export function parseQuery(text) {
  const cleaned = String(text)
    .replace(/\{[^}]*\}/g, ' ')
    .replace(/\$\d+/g, ' ')
    .replace(/\b\d+\.(\.\.)?/g, ' ')
    .replace(/(?:1-0|0-1|1\/2-1\/2|\*)/g, ' ')
    .replace(/[!?]/g, '')
    .trim();
  const tokens = cleaned ? cleaned.split(/\s+/) : [];
  const chess = new Chess();
  for (const tok of tokens) {
    let mv = null;
    try { mv = chess.move(tok); } catch { mv = null; }
    if (!mv) return { sans: chess.history(), ok: false, bad: tok };
  }
  return { sans: chess.history(), ok: true, bad: null };
}

/** A game's rating for filtering/display purposes: the higher of the two
 *  sides' ratings (a speedrun is usually classified by its host's climbing
 *  rating, not the often much-lower opponent's), or null when neither side
 *  has one - reading player ratings from video is best-effort and often
 *  unavailable (see scanner/rating_read.py). */
export function gameRating(game) {
  const vals = [game.rating_white, game.rating_black].filter((v) => v != null);
  return vals.length ? Math.max(...vals) : null;
}

/** Does this game meet a minimum-rating filter? A game with no known
 *  rating can't be confirmed to meet an active filter, so it's excluded
 *  rather than shown as a false "match" - `minRating` of null/NaN means
 *  no filter is active and every game passes. */
export function matchesMinRating(game, minRating) {
  if (minRating == null || Number.isNaN(minRating)) return true;
  const r = gameRating(game);
  return r != null && r >= minRating;
}

/** Strict prefix: does `game.moves_san` begin with exactly `qsans`? */
export function matchesPrefix(game, qsans) {
  if (qsans.length === 0) return true;
  if (qsans.length > game.moves_san.length) return false;
  for (let i = 0; i < qsans.length; i++) {
    if (game.moves_san[i] !== qsans[i]) return false;
  }
  return true;
}

/** Board snapshots (chess.js `board()` grids) after 0..N plies. */
export function replay(sans) {
  const c = new Chess();
  const out = [c.board()];
  for (const san of sans) {
    try { c.move(san); } catch { break; }
    out.push(c.board());
  }
  while (out.length < sans.length + 1) out.push(out[out.length - 1]);
  return out;
}

export function moveLabel(sans, ply) {
  const n = Math.ceil(ply / 2);
  const dots = ply % 2 === 1 ? '.' : '...';
  return `${n}${dots} ${sans[ply - 1]}`;
}

export function toPgn(sans) {
  let out = '';
  for (let i = 0; i < sans.length; i++) {
    if (i % 2 === 0) out += `${i / 2 + 1}. `;
    out += sans[i] + ' ';
  }
  return out.trim();
}

const GLYPH = { p: '♟', n: '♞', b: '♝', r: '♜', q: '♛', k: '♚' };

/** Inline SVG for a chess.js board grid (rank 8 first). */
export function boardSvg(rows) {
  const S = 36, size = S * 8;
  let sq = '', pc = '';
  for (let r = 0; r < 8; r++) {
    for (let f = 0; f < 8; f++) {
      const light = (r + f) % 2 === 0;
      sq += `<rect x="${f * S}" y="${r * S}" width="${S}" height="${S}" fill="${light ? 'var(--light-sq)' : 'var(--dark-sq)'}"/>`;
      const cell = rows[r] && rows[r][f];
      if (cell) {
        const fill = cell.color === 'w' ? '#fafafa' : '#141414';
        const stroke = cell.color === 'w' ? '#141414' : '#e8e8e8';
        pc += `<text x="${f * S + S / 2}" y="${r * S + S / 2}" font-size="${S * 0.82}" ` +
              `text-anchor="middle" dominant-baseline="central" fill="${fill}" ` +
              `stroke="${stroke}" stroke-width="0.6" paint-order="stroke">${GLYPH[cell.type]}</text>`;
      }
    }
  }
  return `<svg viewBox="0 0 ${size} ${size}" xmlns="http://www.w3.org/2000/svg" role="img">${sq}${pc}</svg>`;
}

export function withTimestamp(url, seconds) {
  if (!url) return '#';
  if (!seconds) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}t=${Math.max(0, Math.floor(seconds))}s`;
}

export function fmtTime(s) {
  s = Math.floor(s);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
