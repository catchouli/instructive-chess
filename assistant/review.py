"""A local page for checking the model's labels (classify.py) by hand.

    scanner/.venv/bin/python assistant/review.py [--n 150] [--seed 1] [--kind instructive]
    scanner/.venv/bin/python assistant/review.py --run TAG     # a dev-set run (iterate.py)

Writes assistant/cache/review.html (local only: it quotes the transcripts) with a
random sample of labelled units, each with the units around it, its moves, the
model's label and idea, a link to the moment in the video and one to the
position on lichess's analysis board (from the narrator's side). Clicking a
verdict saves it in the browser; "Export" downloads them as JSON, for
`validate.py`-style scoring. An idea also gets a verdict: faithful to what was
said, or going beyond it. With --run the page is review_TAG.html.
"""
from __future__ import annotations

import argparse
import json
import random

import chess

import positions

CACHE = positions.ROOT / "assistant" / "cache"
LABELS = CACHE / "labels"
OUT = CACHE / "review.html"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Label review</title>
<style>
:root { --bg:#fbfaf7; --card:#fff; --ink:#1d1d1b; --mute:#6b6a65; --line:#e4e1d8;
  --ins:#1f7a4d; --mov:#8a6d1d; --cha:#7a4a8a; --sel:#1d1d1b; }
@media (prefers-color-scheme: dark) { :root { --bg:#161614; --card:#1f1f1c; --ink:#ecebe6; --mute:#9d9b93;
  --line:#33322d; --ins:#5cc28f; --mov:#d8b55a; --cha:#c597d6; --sel:#ecebe6; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 -apple-system, system-ui, sans-serif }
header { position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line);
  padding:12px 16px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; z-index:1 }
h1 { font-size:17px; margin:0 } .stat { color:var(--mute) } main { max-width:860px; margin:0 auto; padding:16px }
button { font:inherit; border:1px solid var(--line); background:var(--card); color:var(--ink);
  border-radius:6px; padding:4px 10px; cursor:pointer }
button.on { background:var(--sel); color:var(--bg); border-color:var(--sel) }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px 16px; margin:0 0 12px }
.meta { color:var(--mute); font-size:13px; display:flex; gap:12px; flex-wrap:wrap }
.ctx { color:var(--mute); font-size:14px } .said { font-size:16px; margin:6px 0 }
.label { font-weight:600 } .instructive { color:var(--ins) } .move { color:var(--mov) } .chatter { color:var(--cha) }
.idea { margin:4px 0 2px; font-style:italic } .evidence { color:var(--mute); font-size:13px; margin:0 0 6px }
.warn { color:#c0392b } .verdict { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px }
a { color:inherit }
</style></head><body>
<header><h1>Label review</h1><span class="stat" id="stat"></span>
<button id="export">Export verdicts</button></header>
<main id="list"></main>
<script>
const UNITS = __DATA__;
const KEY = "label-review-" + __KEY__;
let verdicts = {}, ideas = {};
try { verdicts = JSON.parse(localStorage.getItem(KEY) || "{}"); ideas = JSON.parse(localStorage.getItem(KEY + "-idea") || "{}"); } catch (e) {}
function save() { try { localStorage.setItem(KEY, JSON.stringify(verdicts)); localStorage.setItem(KEY + "-idea", JSON.stringify(ideas)); } catch (e) {} stat(); }
function stat() {
  const v = Object.values(verdicts), ok = v.filter(x => x === "right").length;
  document.getElementById("stat").textContent =
    `${v.length} of ${UNITS.length} checked` + (v.length ? `, model right on ${ok} (${Math.round(100 * ok / v.length)}%)` : "")
    + (Object.keys(ideas).length ? `; ideas faithful ${Object.values(ideas).filter(x => x === "faithful").length} of ${Object.keys(ideas).length}` : "");
}
const esc = s => s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const list = document.getElementById("list");
for (const u of UNITS) {
  const card = document.createElement("div"); card.className = "card";
  const t = Math.max(0, Math.floor(u.start));
  card.innerHTML = `<div class="meta"><span>${esc(u.channel)}</span><span>ply ${u.ply}${u.trusted ? "" : " (untrusted)"}</span>
    <span>${esc(u.recent)}</span><a href="https://www.youtube.com/watch?v=${u.video_id}&t=${t}s" target="_blank">video at ${Math.floor(t/60)}:${String(t%60).padStart(2,"0")}</a>
    <a href="${u.board}" target="_blank">board</a></div>
    <div class="ctx">${esc(u.before)}</div><div class="said">${esc(u.text)}</div><div class="ctx">${esc(u.after)}</div>
    <div>Model: <span class="label ${u.kind}">${u.kind}</span>${u.topics ? ` <span class="stat">${esc(u.topics)}</span>` : ""}</div>
    ${u.idea ? `<div class="idea">${esc(u.idea)}</div>` : ""}
    ${u.evidence ? `<div class="evidence">Evidence: "${esc(u.evidence)}"${u.evidence_ok === false ? ' <span class="warn">(not found in what was said)</span>' : ""}</div>` : ""}
    <div class="verdict">${["right", "instructive", "move", "chatter"].map(v =>
      `<button data-v="${v}">${v === "right" ? "Right" : "Should be " + v}</button>`).join("")}</div>
    ${u.idea ? `<div class="verdict">${["faithful", "beyond"].map(v =>
      `<button data-i="${v}">${v === "faithful" ? "Idea faithful" : "Idea goes beyond what was said"}</button>`).join("")}</div>` : ""}`;
  const ibtns = card.querySelectorAll("button[data-i]");
  const ipaint = () => ibtns.forEach(b => b.classList.toggle("on", ideas[u.id] === b.dataset.i));
  ibtns.forEach(b => b.onclick = () => { if (ideas[u.id] === b.dataset.i) delete ideas[u.id]; else ideas[u.id] = b.dataset.i;
    ipaint(); save(); });
  ipaint();
  const btns = card.querySelectorAll("button[data-v]");
  const paint = () => btns.forEach(b => b.classList.toggle("on", verdicts[u.id] === b.dataset.v));
  btns.forEach(b => {
    if (b.dataset.v === u.kind) b.style.display = "none";
    b.onclick = () => { verdicts[u.id] = verdicts[u.id] === b.dataset.v ? undefined : b.dataset.v;
      if (!verdicts[u.id]) delete verdicts[u.id]; paint(); save(); };
  });
  paint(); list.appendChild(card);
}
document.getElementById("export").onclick = () => {
  // every verdict saved here, also for units an earlier build of this page showed
  const kind = Object.fromEntries(UNITS.map(u => [u.id, u.kind]));
  const ids = [...new Set([...Object.keys(verdicts), ...Object.keys(ideas)])];
  const out = ids.map(id => ({id, model: kind[id] ?? null,
    hand: verdicts[id] === "right" ? (kind[id] ?? "right") : (verdicts[id] ?? null), idea: ideas[id] ?? null}));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], {type: "application/json"}));
  a.download = "review_verdicts_" + __KEY__ + ".json"; a.click();
};
stat();
</script></body></html>
"""


def analysis_url(moves: list[str], ply: int, pov: str | None) -> str:
    board = chess.Board()
    for san in moves[:ply]:
        board.push_san(san)
    return "https://lichess.org/analysis/" + board.fen().replace(" ", "_") + ("?color=black" if pov == "black" else "")


def entry(u: dict, lab: dict, before: str, after: str, games: dict) -> dict:
    g = games[u["game_id"]]
    return {"id": u["id"], "video_id": u["video_id"], "channel": u["channel"], "ply": u["ply"],
            "recent": u["recent"], "trusted": u["trusted"], "start": u["start"], "text": u["text"],
            "before": before, "after": after, "kind": lab.get("kind"), "topics": ", ".join(lab.get("topics") or []),
            "idea": lab.get("idea") or "", "evidence": lab.get("evidence") or "", "evidence_ok": lab.get("evidence_ok"),
            "board": analysis_url(g["moves_san"], u["ply"], g.get("pov"))}


def write(sample: list[dict], key: str, out) -> None:
    data = json.dumps(sample, ensure_ascii=False).replace("</", "<\\/")
    out.write_text(PAGE.replace("__DATA__", data).replace("__KEY__", json.dumps(key)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--kind", choices=["instructive", "move", "chatter"], help="only units the model gave this label")
    ap.add_argument("--run", help="show a dev-set run (iterate.py label TAG)")
    args = ap.parse_args(argv)
    games = {g["id"]: g for g in json.loads(positions.CATALOG.read_text())}
    if args.run:
        dev = CACHE / "dev"
        labels = json.loads((dev / f"run_{args.run}.json").read_text())["labels"]
        sample = []
        for u in json.loads((dev / "units.json").read_text()):
            batch = u["batch"]
            i = next(j for j, c in enumerate(batch) if c["id"] == u["id"])
            sample.append(entry(u, labels.get(u["id"]) or {}, batch[i - 1]["text"] if i > 0 else "",
                                batch[i + 1]["text"] if i + 1 < len(batch) else "", games))
        out = CACHE / f"review_{args.run}.html"
        write(sample, f"run-{args.run}", out)
        print(f"{len(sample)} units in {out}")
        return 0
    pool = []
    for f in sorted(LABELS.glob("*.json")):
        us = json.loads(f.read_text())["units"]
        for i, u in enumerate(us):
            lab = u.get("label")
            if not lab or (args.kind and lab.get("kind") != args.kind):
                continue
            same = lambda j: 0 <= j < len(us) and us[j]["game_id"] == u["game_id"]
            pool.append(entry(u, lab, us[i - 1]["text"] if same(i - 1) else "",
                              us[i + 1]["text"] if same(i + 1) else "", games))
    # the same units on every rebuild (labels keep arriving while classify.py runs)
    kept = CACHE / f"review_sample_{args.seed}{'_' + args.kind if args.kind else ''}.json"
    by_id = {u["id"]: u for u in pool}
    if kept.exists():
        sample = [by_id[i] for i in json.loads(kept.read_text()) if i in by_id]
    else:
        sample = random.Random(args.seed).sample(pool, min(args.n, len(pool)))
        kept.write_text(json.dumps([u["id"] for u in sample]))
    write(sample, str(args.seed), OUT)
    print(f"{len(sample)} of {len(pool)} units in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
