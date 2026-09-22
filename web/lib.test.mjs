// Node test for the pure UI logic:  node web/lib.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  parseQuery, matchesPrefix, replay, toPgn, boardSvg, withTimestamp, fmtTime,
  gameRating, matchesMinRating,
} from './lib.js';

let pass = 0;
const t = (name, fn) => { fn(); pass++; console.log('ok  ' + name); };

t('parseQuery: numbered pgn -> san list', () => {
  const { sans, ok } = parseQuery('1. e4 c5 2. Nc3 e6');
  assert.deepEqual(sans, ['e4', 'c5', 'Nc3', 'e6']);
  assert.equal(ok, true);
});

t('parseQuery: bare moves, mixed spacing', () => {
  assert.deepEqual(parseQuery('  e4   e5  Nf3 ').sans, ['e4', 'e5', 'Nf3']);
});

t('parseQuery: black-to-move ellipsis and annotations', () => {
  const { sans, ok } = parseQuery('1. e4 e5 2. Nf3!! Nc6?! 3. Bb5 a6');
  assert.deepEqual(sans, ['e4', 'e5', 'Nf3', 'Nc6', 'Bb5', 'a6']);
  assert.equal(ok, true);
});

t('parseQuery: illegal move -> legal prefix + bad token', () => {
  const { sans, ok, bad } = parseQuery('1. e4 e5 2. Bxf7');   // bishop can't move on a file
  assert.deepEqual(sans, ['e4', 'e5']);
  assert.equal(ok, false);
  assert.equal(bad, 'Bxf7');
});

t('parseQuery: empty', () => {
  assert.deepEqual(parseQuery('').sans, []);
  assert.equal(parseQuery('   ').ok, true);
});

t('matchesPrefix: strict prefix semantics', () => {
  const g = { moves_san: ['e4', 'c5', 'Nc3', 'e6', 'f4'] };
  assert.equal(matchesPrefix(g, []), true);
  assert.equal(matchesPrefix(g, ['e4', 'c5']), true);
  assert.equal(matchesPrefix(g, ['e4', 'e5']), false);       // wrong 2nd move
  assert.equal(matchesPrefix(g, ['c5']), false);             // not a prefix
  assert.equal(matchesPrefix(g, ['e4', 'c5', 'Nc3', 'e6', 'f4', 'd5']), false); // longer than game
});

t('replay: snapshot count and a known square', () => {
  const snaps = replay(['e4', 'e5', 'Nf3']);
  assert.equal(snaps.length, 4);
  // after 1.e4 the e4 square (rank 4 -> row 4, file e -> col 4) holds a white pawn
  const p = snaps[1][4][4];
  assert.equal(p.type, 'p');
  assert.equal(p.color, 'w');
});

t('replay: tolerates a bad tail without throwing', () => {
  const snaps = replay(['e4', 'e5', 'Zz9', 'Nf3']);
  assert.equal(snaps.length, 5);
});

t('toPgn: numbering', () => {
  assert.equal(toPgn(['e4', 'c5', 'Nc3']), '1. e4 c5 2. Nc3');
});

t('boardSvg: 64 squares + right piece count for the start position', () => {
  const svg = boardSvg(replay([])[0]);
  assert.equal((svg.match(/<rect /g) || []).length, 64);
  assert.equal((svg.match(/<text /g) || []).length, 32);
});

t('withTimestamp / fmtTime', () => {
  assert.equal(withTimestamp('https://youtu.be/x', 70), 'https://youtu.be/x?t=70s');
  assert.equal(withTimestamp('https://www.youtube.com/watch?v=x', 70), 'https://www.youtube.com/watch?v=x&t=70s');
  assert.equal(withTimestamp('https://youtu.be/x', 0), 'https://youtu.be/x');
  assert.equal(fmtTime(70), '1:10');
  assert.equal(fmtTime(605), '10:05');
});

t('gameRating: higher of the two sides, or null', () => {
  assert.equal(gameRating({ rating_white: 1200, rating_black: 2400 }), 2400);
  assert.equal(gameRating({ rating_white: 2400, rating_black: 1200 }), 2400);
  assert.equal(gameRating({ rating_white: 1800 }), 1800);
  assert.equal(gameRating({}), null);
  assert.equal(gameRating({ rating_white: null, rating_black: null }), null);
});

t('matchesMinRating: no filter passes everything, unknown rating excluded once active', () => {
  const rated = { rating_white: 1500, rating_black: 1900 };
  const unrated = {};
  assert.equal(matchesMinRating(rated, null), true);
  assert.equal(matchesMinRating(unrated, null), true);
  assert.equal(matchesMinRating(rated, 1900), true);
  assert.equal(matchesMinRating(rated, 1901), false);
  assert.equal(matchesMinRating(unrated, 0), false);
});

t('end-to-end against data/games.json (if present)', () => {
  let games;
  try { games = JSON.parse(readFileSync(new URL('./data/games.json', import.meta.url))); }
  catch { console.log('    (skipped - no games.json)'); return; }
  assert.ok(Array.isArray(games));
  for (const g of games) {
    // every stored game must be legal from the start position
    const snaps = replay(g.moves_san);
    assert.equal(snaps.length, g.moves_san.length + 1, `${g.id} has an illegal move`);
    assert.equal(g.ply, g.moves_san.length, `${g.id} ply mismatch`);
    // the query for a game's own opening must match that game
    const { sans } = parseQuery(toPgn(g.moves_san.slice(0, 6)));
    assert.equal(matchesPrefix(g, sans), true, `${g.id} not matched by its own opening`);
  }
  console.log(`    (${games.length} catalogued game(s) validated)`);
});

console.log(`\n${pass} tests passed`);
