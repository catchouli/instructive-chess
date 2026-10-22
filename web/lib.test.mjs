// Node test for the pure UI logic:  node web/lib.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  parseQuery, matchesPrefix, replay, toPgn, boardSvg, withTimestamp, fmtTime,
  gameRating, topRating, matchesMinRating, matchesPov, matchesSpeedrun, narratorSide, trustedView, MIN_SHOWN_PLY,
  matchesTags,
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

t('gameRating: lower of the two sides (both players rated X+), or null', () => {
  assert.equal(gameRating({ rating_white: 1200, rating_black: 2400 }), 1200);
  assert.equal(gameRating({ rating_white: 2400, rating_black: 1200 }), 1200);
  assert.equal(gameRating({ rating_white: 1800 }), 1800);
  assert.equal(gameRating({}), null);
  assert.equal(gameRating({ rating_white: null, rating_black: null }), null);
});

t('topRating: higher of the two sides, for the card pill', () => {
  assert.equal(topRating({ rating_white: 1200, rating_black: 2400 }), 2400);
  assert.equal(topRating({ rating_black: 1500 }), 1500);
  assert.equal(topRating({}), null);
});

t('narratorSide / matchesPov: from the pov field; unknown side never matches an active filter', () => {
  assert.equal(narratorSide({ pov: 'white' }), 'white');
  assert.equal(narratorSide({ pov: 'black' }), 'black');
  assert.equal(narratorSide({}), null);
  assert.equal(narratorSide({ pov: 'red' }), null);
  assert.equal(matchesPov({ pov: 'black' }, null), true);
  assert.equal(matchesPov({ pov: 'black' }, ''), true);
  assert.equal(matchesPov({ pov: 'black' }, 'black'), true);
  assert.equal(matchesPov({ pov: 'black' }, 'white'), false);
  assert.equal(matchesPov({}, 'white'), false);
});

t('matchesTags: any of the ticked tags within a group, every group across them', () => {
  const g = { tags: ['speedrun', 'blitz'] };
  assert.equal(matchesTags(g, new Set()), true);
  assert.equal(matchesTags(g, new Set(['speedrun'])), true);
  assert.equal(matchesTags(g, new Set(['stream'])), false);
  assert.equal(matchesTags(g, new Set(['speedrun', 'stream'])), true);          // OR within Video
  assert.equal(matchesTags(g, new Set(['speedrun', 'rapid'])), false);          // AND across groups
  assert.equal(matchesTags(g, new Set(['speedrun', 'blitz', 'rapid'])), true);
  assert.equal(matchesTags({ tags: ['stream'] }, new Set(['blitz'])), false);   // no clock: no time control
  assert.equal(matchesTags({}, new Set(['stream'])), false);
});

t('matchesSpeedrun: only drops tagged speedruns, and only when asked', () => {
  assert.equal(matchesSpeedrun({ speedrun: true }, false), true);
  assert.equal(matchesSpeedrun({ speedrun: true }, true), false);
  assert.equal(matchesSpeedrun({ speedrun: false }, true), true);
  assert.equal(matchesSpeedrun({}, true), true);
});

t('matchesMinRating: no filter passes everything, unknown rating excluded once active', () => {
  const rated = { rating_white: 1500, rating_black: 1900 };
  const unrated = {};
  assert.equal(matchesMinRating(rated, null), true);
  assert.equal(matchesMinRating(unrated, null), true);
  assert.equal(matchesMinRating(rated, 1500), true);   // both players at least 1500
  assert.equal(matchesMinRating(rated, 1501), false);  // the 1500 side falls short
  assert.equal(matchesMinRating(unrated, 0), false);
});

t('trustedView: games with fewer than 6 trusted moves per side are not shown', () => {
  const moves = ['e4', 'e5', 'Nf3', 'Nc6', 'Bc4', 'Bc5', 'c3', 'Nf6', 'd4', 'exd4', 'cxd4', 'Bb4+', 'Bd2'];
  assert.equal(MIN_SHOWN_PLY, 12);
  assert.equal(trustedView({ moves_san: moves, trusted_ply: 2 }), null);
  assert.equal(trustedView({ moves_san: moves, trusted_ply: 11 }), null);
  assert.equal(trustedView({ moves_san: moves, trusted_ply: 12 }).moves_san.length, 12);
  assert.equal(trustedView({ moves_san: moves.slice(0, 4) }), null);   // short game, no trust data
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

t('trustedView: cuts moves, times and clocks to trusted_ply', () => {
  const moves = ['e4', 'e5', 'Nf3', 'Nc6', 'Bc4', 'Bc5', 'c3', 'Nf6', 'd4', 'exd4', 'cxd4', 'Bb4+', 'Bd2', 'Bxd2+'];
  const secs = moves.map((_, i) => i + 1);
  const clk = moves.map((_, i) => 600 - i);
  const g = { moves_san: moves, move_seconds: secs, clock_white_seconds: clk, trusted_ply: 12, ply: 14 };
  const v = trustedView(g);
  assert.deepEqual(v.moves_san, moves.slice(0, 12));
  assert.deepEqual(v.move_seconds, secs.slice(0, 12));
  assert.deepEqual(v.clock_white_seconds, clk.slice(0, 12));
  assert.equal(v.clock_black_seconds, undefined);
  assert.equal(v.ply, 12);
  assert.equal(g.moves_san.length, 14);   // the data itself is untouched
});

t('trustedView: nothing trusted hides the game; no field shows it all', () => {
  assert.equal(trustedView({ moves_san: ['e4'], trusted_ply: 0 }), null);
  const old = { moves_san: ['e4', 'e5', 'Nf3', 'Nc6', 'Bc4', 'Bc5', 'c3', 'Nf6', 'd4', 'exd4', 'cxd4', 'Bb4+'] };
  assert.equal(trustedView(old), old);
});

console.log(`\n${pass} tests passed`);

// index.html's version stamps must match the files (node web/stamp.mjs)
{
  const { spawnSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const r = spawnSync(process.execPath, [fileURLToPath(new URL('./stamp.mjs', import.meta.url)), '--check'], { encoding: 'utf8' });
  if (r.status !== 0) { console.error('FAIL index.html version stamps are stale: run node web/stamp.mjs'); process.exit(1); }
  console.log('ok  index.html version stamps match the files');
}
