"""12.8 — minimal web UI for `gp serve`.

Renders a single self-contained HTML page that loads Cytoscape.js from
a CDN, calls the REST endpoints with the bearer token from
``localStorage["gp_token"]``, and offers: search, click-to-skeleton,
confidence filter slider.

This module is imported only by ``rest_server`` when the ``[web]``
extra is installed (which pulls in Jinja2). Without Jinja2 the import
fails and ``rest_server`` returns a 503 with an install hint.
"""

from __future__ import annotations

try:
    import jinja2  # noqa: F401  (presence check)
except ImportError as exc:  # pragma: no cover
    raise ImportError("Web UI requires: pip install graphify-plus[web]") from exc


_INDEX = """<!doctype html>
<html><head>
<meta charset="utf-8"/>
<title>graphify-plus</title>
<style>
  body { font: 14px system-ui, sans-serif; margin: 0; display: grid;
         grid-template-columns: 320px 1fr; grid-template-rows: 48px 1fr;
         height: 100vh; }
  header { grid-column: 1 / -1; background: #111; color: #eee;
           display: flex; align-items: center; padding: 0 16px; gap: 12px; }
  aside  { padding: 12px; border-right: 1px solid #ddd; overflow: auto; }
  main   { position: relative; }
  #cy    { position: absolute; inset: 0; }
  input, button { font: inherit; padding: 4px 6px; }
  pre    { background: #f7f7f7; padding: 8px; overflow: auto;
           white-space: pre-wrap; }
</style>
</head><body>
<header>
  <strong>graphify-plus</strong>
  <input id="q" placeholder="search symbol…" style="flex:1"/>
  <label>min conf <input id="conf" type="range" min="0" max="1" step="0.1" value="0"/></label>
  <input id="token" placeholder="bearer token" style="width: 280px"/>
</header>
<aside><div id="info">Click a node to see its skeleton.</div></aside>
<main><div id="cy"></div></main>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<script>
const tokenInput = document.getElementById('token');
tokenInput.value = localStorage.gp_token || '';
tokenInput.oninput = () => { localStorage.gp_token = tokenInput.value; };
function hdr(){ return tokenInput.value ? {'Authorization':'Bearer '+tokenInput.value} : {}; }

const cy = cytoscape({container: document.getElementById('cy'),
  style: [
    {selector:'node', style:{label:'data(label)', 'font-size':10, 'background-color':'#69c'}},
    {selector:'edge', style:{'line-color':'#aaa', 'curve-style':'bezier',
                             'target-arrow-shape':'triangle', width:'data(w)'}},
    {selector:'.low', style:{'line-color':'#e22'}}
  ]});

document.getElementById('q').onchange = async (e) => {
  const r = await fetch('/v1/find', {method:'POST',
    headers:{'Content-Type':'application/json', ...hdr()},
    body: JSON.stringify({query: e.target.value, k: 30})});
  if (!r.ok) { document.getElementById('info').textContent = 'auth/error: '+r.status; return; }
  const data = await r.json();
  cy.elements().remove();
  for (const m of (data.matches||[])) {
    cy.add({data:{id:m.id, label:m.qualified_name||m.id}});
  }
  cy.layout({name:'concentric'}).run();
};

document.getElementById('conf').oninput = (e) => {
  const t = parseFloat(e.target.value);
  cy.edges().forEach(ed => {
    ed.style('display', (ed.data('conf')||0) >= t ? 'element' : 'none');
  });
};

cy.on('tap', 'node', async (evt) => {
  const id = evt.target.id();
  const r = await fetch('/v1/explain/'+encodeURIComponent(id), {headers: hdr()});
  if (!r.ok) return;
  const data = await r.json();
  document.getElementById('info').innerHTML =
    '<h3>'+(data.symbol?.qualified_name||id)+'</h3>'+
    '<pre>'+(data.skeleton||'(no skeleton)')+'</pre>'+
    '<p><b>out:</b> '+(data.out_edges||[]).length+
    ' &middot; <b>in:</b> '+(data.in_edges||[]).length+'</p>';
  for (const e of (data.out_edges||[])) {
    if (!cy.getElementById(e.to).length) cy.add({data:{id:e.to, label:e.to}});
    const cls = (e.confidence!=null && e.confidence < 0.7) ? 'low' : '';
    cy.add({data:{source:id, target:e.to, w: 1+(e.confidence||0)*2, conf:e.confidence}, classes: cls});
  }
});
</script>
</body></html>
"""


def render_index() -> str:
    return _INDEX


__all__ = ["render_index"]
