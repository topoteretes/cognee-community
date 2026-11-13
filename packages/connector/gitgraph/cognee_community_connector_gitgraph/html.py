"""The blast radius as one self-contained 3D page.

``render_html`` is pure: it takes the same table-name -> rows mapping and result as
``blast_radius`` and returns a page with the vendored 3d-force-graph bundle (which carries
three.js) inlined, so opening it makes no network request.
"""

import json
from collections.abc import Iterable, Mapping
from html import escape
from pathlib import Path
from typing import Any

VENDOR = Path(__file__).parent / "vendor"

_COLORS = {"changed": "#e5484d", "at-risk": "#f59e0b", "safe": "#a8a29e"}

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Blast radius of {title}</title>
<style>
html, body {{ margin: 0; height: 100%; background: #f4f2ee; color: #1c1917;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; overflow: hidden; }}
#stage {{ position: absolute; top: 0; right: 0; bottom: 0; left: 340px; }}
#graph, #labels {{ position: absolute; inset: 0; }}
#labels {{ pointer-events: none; overflow: hidden; }}
#labels div {{ position: absolute; white-space: nowrap; padding: 3px 10px 3px 8px;
  font-size: 12px; line-height: 16px; color: #57534e; background: rgba(255, 255, 255, 0.94);
  border: 1px solid color-mix(in srgb, var(--c) 45%, #e7e3dc); border-radius: 999px;
  box-shadow: 0 1px 2px rgba(41, 37, 36, 0.08); }}
#labels div::before {{ content: ""; display: inline-block; width: 7px; height: 7px;
  margin-right: 6px; border-radius: 50%; background: var(--c); vertical-align: 1px; }}
#labels b {{ color: #1c1917; font-weight: 600; }}
#panel {{ position: absolute; top: 16px; left: 16px; z-index: 1; width: 268px;
  padding: 16px 20px; background: #fff; border: 1px solid #e7e3dc; border-radius: 12px;
  box-shadow: 0 1px 2px rgba(41, 37, 36, 0.04), 0 8px 24px rgba(41, 37, 36, 0.06);
  font-size: 14px; line-height: 1.5; }}
#panel h1 {{ margin: 0 0 8px; font-size: 16px; line-height: 1.35; overflow-wrap: anywhere; }}
#panel h2 {{ margin: 14px 0 4px; font-size: 11px; letter-spacing: 0.06em; color: #8a847a;
  text-transform: uppercase; }}
#panel ul {{ margin: 0; padding-left: 18px; color: #44403c; }}
#panel .legend {{ list-style: none; padding: 0; }}
#panel .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  margin-right: 8px; }}
/* the library's mouse hint, quiet and in the page's type (its own style sheet comes later) */
#graph .scene-nav-info {{ bottom: 14px; color: #a8a29e; opacity: 1; font: 11px system-ui,
  -apple-system, "Segoe UI", sans-serif; letter-spacing: 0.01em; }}
/* A phone: the summary is a compact card on top and the graph gets the screen below it, so the
   card never covers a node. Labels show the name only (service and hops are in the card and the
   tooltip): full ones are too wide to all sit beside their nodes on a narrow stage. */
@media (max-width: 760px) {{
  body {{ display: flex; flex-direction: column; }}
  #panel {{ position: relative; top: auto; left: auto; width: auto; margin: 12px 12px 0;
    padding: 14px 16px; font-size: 13px; }}
  #panel h1 {{ margin-bottom: 10px; font-size: 15px; }}
  #panel h2 {{ margin-top: 0; }}
  #panel h2:nth-of-type(2) {{ display: none; }}
  #panel #services {{ display: flex; flex-wrap: wrap; gap: 6px; padding: 0; list-style: none; }}
  #services li {{ padding: 2px 10px; border-radius: 999px; background: #fdf3e2; color: #8a4b0f; }}
  #panel .legend {{ display: flex; flex-wrap: wrap; gap: 2px 14px; margin-top: 12px;
    font-size: 12px; color: #78716c; }}
  #panel .dot {{ width: 8px; height: 8px; margin-right: 6px; }}
  #stage {{ position: relative; inset: auto; flex: 1; min-height: 0; }}
  #labels div {{ padding: 2px 8px 2px 6px; font-size: 11px; line-height: 14px; }}
  #labels div::before {{ width: 6px; height: 6px; margin-right: 5px; }}
  #labels span, #graph .scene-nav-info {{ display: none; }}
}}
</style>
</head>
<body>
<aside id="panel">
<h1>Blast radius of {title}</h1>
<h2>At-risk services</h2>
<ul id="services">
{services}
</ul>
<h2>Legend</h2>
<ul class="legend">
{legend}
</ul>
</aside>
<div id="stage"><div id="graph"></div><div id="labels"></div></div>
<script>{library}</script>
<script id="graph-data" type="application/json">{data}</script>
<script>
const data = JSON.parse(document.getElementById("graph-data").textContent);
const colors = {colors};
// The tooltip is HTML and ids come from file paths: escape every part of it.
const entities = {{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }};
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => entities[c]);
const stage = document.getElementById("stage");
const nodeVal = (n) => (n.status === "safe" ? 3 : 8);
let fitted = false;
window.graph = ForceGraph3D()(document.getElementById("graph"))
  .width(stage.clientWidth)
  .height(stage.clientHeight)
  .backgroundColor("#f4f2ee")
  .nodeColor((n) => colors[n.status])
  .numDimensions(2) // a flat layout, seen at an angle, keeps the labels from stacking up
  .nodeRelSize(7)
  .nodeResolution(24)
  .nodeVal(nodeVal)
  .nodeLabel((n) => esc(n.label) + "<br>service: " + esc(n.service))
  .linkColor(() => "#a8a29e")
  .linkOpacity(0.8)
  .linkWidth(1.5)
  .linkDirectionalArrowLength(7)
  .linkDirectionalArrowRelPos(0.9)
  // lay the graph out before the first frame (d3 is seeded, so the same every time), then
  // fit it to the stage once; a drag later reheats the layout as usual
  .warmupTicks(300)
  .cooldownTicks(0)
  .onEngineStop(() => {{
    // the engine also stops on the empty graph it starts with: fit only once there are nodes
    if (fitted || !data.nodes.some((n) => n.x !== undefined)) return;
    fitted = true;
    window.graph.cooldownTicks(Infinity);
    fitView(600);
  }});
window.graph.d3Force("link").distance(110);
window.graph.d3Force("charge").strength(-160);
window.graph.cameraPosition({{ x: 60, y: 90, z: 320 }});
window.graph.graphData(data);
window.addEventListener("resize", () => {{
  tags.forEach((t) => (t.w = 0)); // measure again: crossing the phone width changes the text
  window.graph.width(stage.clientWidth).height(stage.clientHeight);
  if (fitted) fitView(0);
}});
// Fit the flat layout to the stage from its 2D box and the real camera angle (the library's fit
// wraps a sphere round it and leaves the stage half empty), keeping room at the edges for labels.
const view = {{ x: 0.15, y: 0.3, z: 1 }}; // looking slightly down on the flat layout
function fitView(ms) {{
  const g = window.graph, cam = g.camera(), V = cam.position.constructor;
  const laid = data.nodes.filter((n) => n.x !== undefined);
  const xs = laid.map((n) => n.x), ys = laid.map((n) => n.y);
  const mid = (a) => (Math.min(...a) + Math.max(...a)) / 2;
  const span = (a) => Math.max(...a) - Math.min(...a);
  const w = stage.clientWidth, h = stage.clientHeight;
  // a wide layout on a tall stage (a phone) or the other way round: turn the view a quarter
  // turn so the layout's long side runs along the stage's long side
  const turn = span(xs) > span(ys) !== w > h;
  cam.up.set(turn ? 1 : 0, turn ? 0 : 1, 0);
  const tilt = turn ? [view.y, -view.x] : [view.x, view.y];
  const at = new V(mid(xs), mid(ys), 0), dir = new V(...tilt, view.z).normalize();
  const sized = tags.map(size);
  const mx = Math.max(...sized.map((t) => t.w)) / 2 + 12;
  const my = Math.max(...sized.map((t) => t.h)) + 30;
  const probe = cam.clone();
  const fits = (d) => {{
    probe.position.copy(at).addScaledVector(dir, d);
    probe.lookAt(at);
    probe.updateMatrixWorld();
    return laid.every((n) => {{
      const p = new V(n.x, n.y, n.z || 0).project(probe);
      const x = ((p.x + 1) * w) / 2, y = ((1 - p.y) * h) / 2;
      return x >= mx && x <= w - mx && y >= my && y <= h - my;
    }});
  }};
  let near = 1, far = 1e6; // the nearest camera distance that still fits, by bisection
  for (let i = 0; i < 60; i++) {{
    const d = (near + far) / 2;
    if (fits(d)) far = d;
    else near = d;
  }}
  // one node, or a few close together, fit from any distance: keep them a sensible size
  const to = at.clone().addScaledVector(dir, Math.max(far, 300));
  g.cameraPosition({{ x: to.x, y: to.y, z: to.z }}, {{ x: at.x, y: at.y, z: at.z }}, ms);
}}
// Always-visible labels: "name · service · hops", the name in bold, built as text, not HTML.
const labels = document.getElementById("labels");
const tags = data.nodes.map((n) => {{
  const tag = document.createElement("div");
  const cut = n.short.indexOf(" · ");
  const name = document.createElement("b");
  const rest = document.createElement("span"); // hidden on a phone
  name.textContent = cut < 0 ? n.short : n.short.slice(0, cut);
  rest.textContent = cut < 0 ? "" : n.short.slice(cut);
  tag.append(name, rest);
  tag.title = n.id;
  tag.style.setProperty("--c", colors[n.status]);
  tag.style.display = "none";
  labels.appendChild(tag);
  return tag;
}});
// Labels never overlap: each frame the camera or layout moved, place them in priority order
// (changed, then at-risk by hops, then safe) at the first free spot around their node, out of
// the way of other nodes and inside the stage. A safe label with no free spot is hidden; a
// changed or at-risk one gives up on avoiding nodes before it gives up on its place.
const rank = (n) => (n.status === "changed" ? 0 : n.status === "at-risk" ? n.hops : 1e9);
const order = data.nodes.map((n, i) => i).sort((a, b) => rank(data.nodes[a]) - rank(data.nodes[b]));
const hit = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
// how far a point is from a box (0 inside it)
const gap = (b, q) =>
  Math.hypot(Math.max(b[0] - q.x, 0, q.x - b[2]), Math.max(b[1] - q.y, 0, q.y - b[3]));
// a label's size, measured once (it has to be laid out, unseen, to be measured)
function size(tag) {{
  if (!tag.w) {{
    if (tag.style.display === "none") tag.style.visibility = "hidden";
    tag.style.display = "block";
    tag.w = tag.offsetWidth;
    tag.h = tag.offsetHeight;
  }}
  return tag;
}}
function spots(p, w, h) {{
  const out = [];
  for (let ring = 0; ring < 5; ring++) {{
    const d = p.r + 6 + ring * (h + 6), c = d * 0.75;
    out.push(
      [p.x - w / 2, p.y + d], [p.x - w / 2, p.y - d - h], // below, above
      [p.x + d, p.y - h / 2], [p.x - d - w, p.y - h / 2], // right, left
      [p.x + c, p.y + c], [p.x - c - w, p.y + c], [p.x + c, p.y - c - h], [p.x - c - w, p.y - c - h]
    );
  }}
  // last resort, in a view so crowded a node is boxed in (a phone, zoomed out and tilted): on
  // top of its own node, so it reads as the node itself; centred first
  const steps = (n) => [...Array(n + 1).keys()].map((k) => k / n)
    .sort((a, b) => Math.abs(a - 0.5) - Math.abs(b - 0.5));
  for (const fy of steps(4)) for (const fx of steps(10)) out.push([p.x - fx * w, p.y - fy * h]);
  return out.map(([x, y]) => [x, y, x + w, y + h]);
}}
let lastKey = "";
function placeLabels() {{
  requestAnimationFrame(placeLabels);
  const g = window.graph;
  const width = stage.clientWidth, height = stage.clientHeight;
  const pts = data.nodes.map((n) => {{
    if (n.x === undefined) return null;
    const rad = 7 * Math.cbrt(nodeVal(n));
    const p = g.graph2ScreenCoords(n.x, n.y, n.z);
    const px = g.graph2ScreenCoords(n.x + rad, n.y, n.z);
    const py = g.graph2ScreenCoords(n.x, n.y + rad, n.z);
    const r = Math.max(Math.hypot(px.x - p.x, px.y - p.y), Math.hypot(py.x - p.x, py.y - p.y));
    return {{ x: p.x, y: p.y, r }};
  }});
  const key = JSON.stringify(pts) + width + "x" + height;
  if (key === lastKey) return;
  lastKey = key;
  const discs = pts.map((p) => p && [p.x - p.r, p.y - p.r, p.x + p.r, p.y + p.r]);
  const taken = [];
  const inside = (b) => b[0] >= 4 && b[1] >= 4 && b[2] <= width - 4 && b[3] <= height - 4;
  for (const i of order) {{
    const p = pts[i], tag = tags[i];
    if (!p) continue;
    size(tag);
    const boxes = spots(p, tag.w, tag.h);
    // level 0, free: on the stage and clear of other labels; 1, also nearer its own node than
    // any other, so it cannot be read as naming a neighbour; 2, tidy: also clear of other nodes
    const free = (k, level) => {{
      const b = boxes[k];
      if (!inside(b) || taken.some((t) => hit(b, t))) return false;
      const own = gap(b, p);
      return pts.every((q, j) => !q || j === i ||
        ((level < 1 || gap(b, q) > own) && (level < 2 || !hit(b, discs[j]))));
    }};
    // the spot it had last frame first, so labels do not jump about while the camera moves
    const tries = [...(tag.spot === undefined ? [] : [tag.spot]), ...boxes.keys()];
    let k = tries.find((s) => free(s, 2));
    if (k === undefined && data.nodes[i].status !== "safe") {{
      k = tries.find((s) => free(s, 1)) ?? tries.find((s) => free(s, 0)) ?? tag.spot ?? 0;
    }}
    if (k === undefined) {{
      tag.style.visibility = "hidden";
      continue;
    }}
    const b = boxes[k];
    tag.spot = k;
    tag.style.left = b[0] + "px";
    tag.style.top = b[1] + "px";
    tag.style.visibility = "visible";
    taken.push([b[0] - 3, b[1] - 3, b[2] + 3, b[3] + 3]);
  }}
}}
requestAnimationFrame(placeLabels);
</script>
</body>
</html>
"""


def _hops(count: int) -> str:
    return f"{count} hop" if count == 1 else f"{count} hops"


def _service(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "."


def graph_data(
    tables: Mapping[str, Iterable[Mapping[str, Any]]], result: Mapping[str, Any]
) -> dict[str, Any]:
    """Nodes (one per symbol, with status and hops) and caller -> callee links."""
    changed = set(result["changed_symbols"])
    hops = {s["id"]: s["hops"] for s in result["symbols"]}
    nodes = []
    for row in tables.get("symbols", []):
        sid = row["id"]
        service = _service(row["file"])
        node: dict[str, Any] = {"id": sid, "service": service, "label": sid}
        # the short, always-visible text: "charge · billing · 1 hop"
        short = f"{sid.rpartition('::')[2]} · {service} · "
        if sid in changed:
            node |= {"status": "changed", "short": short + "changed"}
        elif sid in hops:
            node |= {
                "status": "at-risk",
                "hops": hops[sid],
                "label": f"{sid} ({_hops(hops[sid])})",
                "short": short + _hops(hops[sid]),
            }
        else:
            node |= {"status": "safe", "short": short + "safe"}
        nodes.append(node)
    ids = {n["id"] for n in nodes}
    # Module-level imports (no owner symbol) are file -> symbol edges in blast.py; the page draws
    # symbols only, so they have no end to attach to here.
    links = sorted(
        {
            (ref["from_symbol"], ref["to_symbol"])
            for ref in tables.get("references", [])
            if ref.get("from_symbol") in ids
            and ref.get("to_symbol") in ids
            and ref["from_symbol"] != ref["to_symbol"]
        }
    )
    return {"nodes": nodes, "links": [{"source": s, "target": t} for s, t in links]}


def render_html(
    tables: Mapping[str, Iterable[Mapping[str, Any]]], result: Mapping[str, Any]
) -> str:
    """The page for one blast result; ``window.graph`` is the ForceGraph3D instance."""
    tables = {name: list(rows) for name, rows in tables.items()}
    services = [
        f"<li>{escape(s['name'])}: {_hops(s['hops'])}</li>" for s in result["services"]
    ] or ["<li>none</li>"]
    legend = [
        f'<li><span class="dot" style="background: {_COLORS[status]}"></span>{text}</li>'
        for status, text in (
            ("changed", "changed symbol"),
            ("at-risk", "at risk (calls the change)"),
            ("safe", "safe"),
        )
    ]
    # A "<" in the data could close the script element or open a comment; < is the same
    # character to a JSON parser.
    data = json.dumps(graph_data(tables, result)).replace("<", "\\u003c")
    return _PAGE.format(
        title=escape(f"{result['base']}...{result['head']}"),
        services="\n".join(services),
        legend="\n".join(legend),
        library=(VENDOR / "3d-force-graph.min.js").read_text(encoding="utf-8"),
        data=data,
        colors=json.dumps(_COLORS),
    )
