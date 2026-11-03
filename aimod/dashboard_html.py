"""AIMOD Live: a web dashboard served by AIMOD (http://<pc>:8767/) - running order, pit log and pit board.

Read-only. Polls /monitor.json and /pitlog.json once a second. URL options (also for OBS browser sources):
  ?view=monitor|pitlog|board   open on that tab (and hide the tab bar with &bare=1)
  &cols=pos,num,name,...       monitor columns to show (default: all)
  &hideout=1                   hide cars that are out
  &scale=1.2                   zoom
"""

DASHBOARD_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIMOD Live</title>
<style>
  :root { --bg:#111214; --panel:#1b1c1f; --line:#2a2c30; --fg:#e6e6e6; --dim:#8a8a8a; --accent:#00d26a;
          --yellow:#ffd54f; --red:#ff5c5c; --blue:#4fc3f7; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:13px/1.35 "Segoe UI", Arial, sans-serif; }
  header { display:flex; align-items:center; gap:14px; padding:8px 14px; background:#0a0a0b; border-bottom:3px solid var(--accent); }
  header.caution { border-bottom-color: var(--yellow); }
  header .brand { font-weight:800; letter-spacing:.5px; color:var(--accent); }
  header .track { font-weight:700; font-size:15px; }
  header .laps, header .stage { color:var(--dim); }
  header .flag { margin-left:auto; font-weight:800; padding:2px 10px; border-radius:3px; background:var(--accent); color:#000; }
  header.caution .flag { background:var(--yellow); }
  nav { display:flex; gap:2px; padding:6px 14px 0; }
  nav button { background:var(--panel); color:var(--dim); border:0; padding:6px 14px; cursor:pointer; font:inherit; font-weight:600; }
  nav button.on { color:var(--fg); box-shadow: inset 0 -2px 0 var(--accent); }
  main { padding:8px 14px 14px; }
  table { width:100%; border-collapse:collapse; background:var(--panel); }
  th { position:sticky; top:0; background:#232428; color:var(--dim); text-align:left; font-weight:600; padding:5px 6px; white-space:nowrap; }
  td { padding:3px 6px; border-top:1px solid var(--line); white-space:nowrap; }
  td.r, th.r { text-align:right; }
  tr.group td { background:#26272b; color:var(--yellow); font-weight:700; }
  .chip { display:inline-block; min-width:28px; text-align:center; font-weight:700; border-radius:3px; padding:0 4px; }
  .empty { color:var(--dim); padding:20px; }
  iframe { border:0; width:100%; height:calc(100vh - 110px); background:transparent; }
  body.bare header, body.bare nav { display:none; }
  body.bare main { padding:0; }
  body.bare iframe { height:100vh; }
  .stale { color: var(--red); font-weight:700; }
</style></head>
<body>
<header id="hdr"><span class="brand">AIMOD LIVE</span><span class="track" id="track"></span>
  <span class="laps" id="laps"></span><span class="stage" id="stage"></span><span id="stale"></span><span class="flag" id="flag">-</span></header>
<nav id="nav"><button data-v="monitor">Running order</button><button data-v="pitlog">Pit log</button><button data-v="board">Pit board</button></nav>
<main>
  <div id="monitor"></div>
  <div id="pitlog" hidden></div>
  <div id="board" hidden><iframe id="boardframe"></iframe></div>
</main>
<script>
const q = new URLSearchParams(location.search);
const scale = parseFloat(q.get('scale') || '1');
if (scale !== 1) document.body.style.zoom = scale;
if (q.get('bare') === '1') document.body.classList.add('bare');
const hideOut = q.get('hideout') === '1';
const COLS = [
  ['pos','Pos','r'],['num','#','r'],['name','Driver',''],['make','Make',''],['lap','Lap','r'],['gap','Gap','r'],
  ['wear_l','Left','r'],['wear_r','Right','r'],['prob_l','L-Prob','r'],['prob_r','R-Prob','r'],['damage','Damage','r'],
  ['fuel','Tank laps','r'],['fuelpct','Fuel','r'],['fuelleft','Fuel laps','r'],['mileage','Laps/tank','r'],['fuelst','To finish',''],['pace','Pace','r'],['falloff','Falloff','r'],['tires','Tires',''],
  ['stops','Stops','r'],['last','Last pit',''],['next','Next pit',''],['pen','Flag',''],['status','State / service','']];
const want = (q.get('cols') || '').split(',').map(s => s.trim()).filter(Boolean);
const cols = want.length ? COLS.filter(c => want.includes(c[0])) : COLS;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let view = q.get('view') || localStorage.getItem('aimod_view') || 'monitor';
function show(v) {
  view = v; try { localStorage.setItem('aimod_view', v); } catch (e) {}
  for (const id of ['monitor','pitlog','board']) document.getElementById(id).hidden = id !== v;
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  const f = document.getElementById('boardframe');
  if (v === 'board' && !f.src) f.src = '/overlay?always=1' + (q.get('boardscale') ? '&scale=' + q.get('boardscale') : '&scale=1.4');
}
document.querySelectorAll('nav button').forEach(b => b.onclick = () => show(b.dataset.v));
show(view);

function header(d) {
  document.getElementById('track').textContent = d.track || '';
  document.getElementById('laps').textContent = d.laps || '';
  document.getElementById('stage').textContent = d.stage || '';
  document.getElementById('flag').textContent = d.flag || '-';
  document.getElementById('hdr').classList.toggle('caution', !!d.caution);
}
function monitor(d) {
  const rows = (d.rows || []).filter(r => !(hideOut && r.out));
  if (!rows.length) { document.getElementById('monitor').innerHTML = '<div class="empty">Waiting for NR2003...</div>'; return; }
  let h = '<table><tr>' + cols.map(c => `<th class="${c[2]}">${c[1]}</th>`).join('') + '</tr>';
  for (const r of rows) {
    h += `<tr style="color:${r.fg || 'inherit'}">` + cols.map(c => {
      const v = r.cells[c[0]] ?? '';
      const bg = (r.colors || {})[c[0]];
      if (c[0] === 'num' && r.color) return `<td class="r"><span class="chip" style="background:${r.color};color:${r.color_fg}">${esc(v)}</span></td>`;
      return `<td class="${c[2]}"${bg ? ` style="color:${bg};font-weight:600"` : ''}>${esc(v)}</td>`;
    }).join('') + '</tr>';
  }
  document.getElementById('monitor').innerHTML = h + '</table>';
}
function pitlog(d) {
  const stops = d.stops || [];
  if (!stops.length) { document.getElementById('pitlog').innerHTML = '<div class="empty">No stops yet.</div>'; return; }
  let h = '<table><tr><th class="r">Lap</th><th class="r">#</th><th>Driver</th><th class="r">Stop</th><th>Service</th><th>Reason</th>' +
          '<th class="r">Pos in</th><th class="r">Pos out</th><th class="r">+/-</th><th class="r">Service (s)</th><th class="r">Pit road (s)</th><th>Why</th></tr>';
  let group = null;
  for (const s of stops) {
    const g = s.green ? 'GREEN FLAG' : 'CAUTION ' + s.caution_no;
    if (g !== group) { group = g; h += `<tr class="group"><td colspan="12">${esc(g)}</td></tr>`; }
    const gain = (s.pos_in && s.pos_out) ? s.pos_in - s.pos_out : null;
    h += `<tr><td class="r">${s.lap}</td><td class="r">${esc(s.number)}</td><td>${esc(s.name)}</td><td class="r">${s.stop_no}</td>` +
         `<td>${esc(s.code)}</td><td>${esc(s.reason)}</td><td class="r">${s.pos_in || ''}</td><td class="r">${s.pos_out || ''}</td>` +
         `<td class="r" style="color:${gain > 0 ? 'var(--accent)' : gain < 0 ? 'var(--red)' : 'inherit'}">${gain === null ? '' : (gain > 0 ? '+' : '') + gain}</td>` +
         `<td class="r">${s.box_time ? s.box_time.toFixed(1) : ''}</td><td class="r">${s.road_time ? s.road_time.toFixed(1) : ''}</td><td>${esc(s.note)}</td></tr>`;
  }
  document.getElementById('pitlog').innerHTML = h + '</table>';
}
let lastOk = Date.now();
async function tick() {
  try {
    const m = await (await fetch('/monitor.json', { cache: 'no-store' })).json();
    header(m); if (view === 'monitor') monitor(m);
    if (view === 'pitlog') pitlog(await (await fetch('/pitlog.json', { cache: 'no-store' })).json());
    lastOk = Date.now();
  } catch (e) {}
  document.getElementById('stale').innerHTML = Date.now() - lastOk > 4000 ? '<span class="stale">AIMOD not responding</span>' : '';
  setTimeout(tick, 1000);
}
tick();
</script></body></html>
"""
