// Stockfish in the browser (vendor/stockfish: the lite single-threaded WASM
// build, which needs no special server headers). One worker for the page,
// created the first time it's asked for; each analyse() stops the search before.
import { Chess } from './vendor/chess.js';

const ENGINE = 'vendor/stockfish/stockfish-19-lite-single.js';
export const DEPTH = 22;

let worker = null;
let ready = null;          // resolves once the engine answered "readyok"
let listener = null;       // the current analysis's callback
let current = 0;           // id of the current analysis

function start() {
  if (worker) return ready;
  worker = new Worker(ENGINE);
  ready = new Promise((resolve) => {
    worker.onmessage = (e) => {
      const line = String(e.data);
      if (line === 'readyok') resolve();
      if (listener) listener(line);
    };
  });
  worker.postMessage('uci');
  worker.postMessage('isready');
  return ready;
}

/** Analyse a position to DEPTH. `onInfo` gets {depth, cp, mate, pv: [san...],
 *  done} each time the engine reports, the score from White's side. Returns a
 *  function that stops this analysis. */
export function analyse(fen, onInfo) {
  const id = ++current;
  const whiteToMove = fen.split(' ')[1] === 'w';
  listener = null;
  start().then(() => {
    if (id !== current) return;
    worker.postMessage('stop');
    // the "bestmove" a stop produces belongs to the old search: only
    // listen once the engine has confirmed it's ready again
    listener = (line) => {
      if (line === 'readyok') { listener = (l) => handle(l); }
    };
    worker.postMessage('isready');
    worker.postMessage('ucinewgame');
    worker.postMessage(`position fen ${fen}`);
    worker.postMessage(`go depth ${DEPTH}`);
  });
  let last = null;
  function handle(line) {
    if (id !== current) return;
    if (line.startsWith('bestmove')) {
      if (last) onInfo({ ...last, done: true });
      return;
    }
    if (!line.startsWith('info') || !line.includes(' pv ') || line.includes(' upperbound') || line.includes(' lowerbound')) return;
    const depth = Number(/ depth (\d+)/.exec(line)?.[1]);
    const cpm = / score cp (-?\d+)/.exec(line);
    const matem = / score mate (-?\d+)/.exec(line);
    const sign = whiteToMove ? 1 : -1;
    last = {
      depth,
      cp: cpm ? sign * Number(cpm[1]) : null,
      mate: matem ? sign * Number(matem[1]) : null,
      pv: toSan(fen, line.split(' pv ')[1].trim().split(' ')),
      done: false,
    };
    onInfo(last);
  }
  return () => { if (id === current) { current++; listener = null; if (worker) worker.postMessage('stop'); } };
}

function toSan(fen, ucis) {
  const chess = new Chess(fen);
  const out = [];
  for (const u of ucis.slice(0, 8)) {
    try {
      const mv = chess.move({ from: u.slice(0, 2), to: u.slice(2, 4), promotion: u[4] });
      out.push(mv.san);
    } catch { break; }
  }
  return out;
}

/** "+0.35", "-1.20", "#3", "#-2" from White's side. */
export function fmtScore(info) {
  if (info.mate != null) return `#${info.mate}`;
  if (info.cp == null) return '';
  const v = info.cp / 100;
  return (v > 0 ? '+' : '') + v.toFixed(2);
}
