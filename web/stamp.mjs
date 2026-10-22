// Version stamps for the page's files, so a browser never mixes a new file
// with an old cached one (GitHub Pages lets browsers cache for 10 minutes; a
// new app.js importing an old lib.js broke the page outright).
//
//     node web/stamp.mjs           rewrite index.html with the current stamps
//     node web/stamp.mjs --check   exit 1 if index.html's stamps are stale
//
// index.html loads the stylesheets and app.js with ?v=<hash of the file>, and
// an import map sends every module import to its stamped URL, so the modules
// themselves never change. Run it after editing any file listed in FILES.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB = dirname(fileURLToPath(import.meta.url));
const MODULES = ['lib.js', 'engine.js', 'vendor/chess.js', 'vendor/chessground/chessground.min.js'];
const STYLES = ['vendor/chessground/chessground.base.css', 'vendor/chessground/chessground.brown.css',
  'vendor/chessground/chessground.cburnett.css', 'style.css'];
const ENTRY = 'app.js';

const stamp = (f) => createHash('sha1').update(readFileSync(join(WEB, f))).digest('hex').slice(0, 10);

function render(html) {
  for (const f of STYLES) {
    html = html.replace(new RegExp(`href="${f.replace(/[.]/g, '\\.')}(\\?v=[0-9a-f]*)?"`), `href="${f}?v=${stamp(f)}"`);
  }
  const map = { imports: Object.fromEntries(MODULES.map((f) => [`./${f}`, `./${f}?v=${stamp(f)}`])) };
  const block = `<script type="importmap">\n${JSON.stringify(map, null, 2)}\n</script>`;
  html = html.replace(/<script type="importmap">[\s\S]*?<\/script>\n?/, '');
  html = html.replace(/<script type="module" src="app\.js(\?v=[0-9a-f]*)?"><\/script>/,
    `${block}\n<script type="module" src="${ENTRY}?v=${stamp(ENTRY)}"></script>`);
  return html;
}

const path = join(WEB, 'index.html');
const before = readFileSync(path, 'utf8');
const after = render(before);
if (process.argv.includes('--check')) {
  if (after !== before) {
    console.error('web/index.html has stale version stamps: run node web/stamp.mjs');
    process.exit(1);
  }
  console.log('stamps up to date');
} else {
  writeFileSync(path, after);
  console.log(after === before ? 'stamps already up to date' : 'index.html stamped');
}
