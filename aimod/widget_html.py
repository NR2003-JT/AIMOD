"""The pit board as a web page, for the companion app's bridge (http://localhost:8766/widget/aimodpits)
and OBS browser sources. Installed into the companion app folder as nr2003_aimodpits.html.
Same layout as the in-game board (overlay.py)."""

WIDGET_FILE = "nr2003_aimodpits.html"

WIDGET_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AIMOD Pit Board</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { background: transparent; overflow: hidden; }
  body { font-family: 'Bahnschrift SemiBold Condensed', 'Bahnschrift', 'Arial Narrow', sans-serif;
         color: #f4f4f4; transform-origin: 0 0; font-style: italic; font-weight: 700; }
  #board { display: none; width: var(--w); }
  #board.show { display: block; }
  .series.logo { height: 36px; line-height: 36px; }
  .series.logo img { max-height: 30px; max-width: 70%; vertical-align: middle; }
  .series { height: 20px; background: #070707; text-align: center; font-size: 13px; line-height: 20px;
            text-transform: uppercase; letter-spacing: .3px; }
  .title { height: 46px; background: #101010; position: relative; border-bottom: 3px solid #3ddc84; }
  .title.caution { border-bottom-color: #ffcc33; }
  .title .track { position: absolute; left: 34px; top: 4px; font-size: 20px; }
  .title .stripes { position: absolute; left: 10px; top: 7px; display: flex; gap: 2px; }
  .title .stripes i { width: 4px; height: 14px; transform: skewX(-18deg); display: block; }
  .title .lap { position: absolute; right: 8px; top: 8px; font-size: 12px; color: #9a9a9a; font-style: normal; }
  .title .mode { position: absolute; left: 0; right: 0; top: 23px; text-align: center; font-size: 18px; }
  .stay { height: 17px; background: #070707; display: flex; align-items: center; font-size: 13px; }
  .stay .p { width: 30px; padding-left: 8px; color: #9a9a9a; font-size: 12px; }
  .stay .n { width: 30px; text-align: center; } .stay .n img { height: 14px; vertical-align: middle; }
  .stay .l { margin-left: 4px; }
  .soon { height: 18px; background: #070707; display: flex; align-items: center; font-size: 12px; padding-left: 8px;
          white-space: nowrap; overflow: hidden; }
  .soon .lbl { color: #9a9a9a; margin-right: 8px; } .soon.now .lbl { color: #ffcc33; }
  .soon .n { margin-right: 5px; font-size: 14px; }
  .chip { padding: 0 4px; line-height: 15px; display: inline-block; }
  .staybar { height: 18px; background: #1d3f8f; text-align: center; font-size: 13px; line-height: 18px; }
  #rows { position: relative; overflow: hidden; transition: height .35s ease; }
  #rows .row { position: absolute; left: 0; right: 0; transition: top .35s ease; }
  .more { position: relative; }
  body.compact .row { height: 24px; }
  body.compact .numcell { width: 46px; font-size: 16px; }
  body.compact .numcell img { max-height: 20px; }
  body.compact .head { display: none; }
  body.compact .info .pos { left: 6px; top: 4px; bottom: auto; }
  body.compact .info .name { left: 36px; right: auto; top: 2px; font-size: 15px; }
  body.compact .info .line2 { top: 5px; bottom: auto; }
  .row { height: 40px; display: flex; background: #141414; border-bottom: 1px solid #000;
         }
  .row:nth-child(even) { background: #1b1b1b; }
  .numcell { width: 30%; height: 100%; display: flex; align-items: center; justify-content: center;
             font-size: 28px; text-shadow: 1px 1px 0 #000; }
  .numcell img { max-width: 88%; max-height: 32px; }
  .head { width: 38px; height: 38px; margin-top: 1px; margin-left: 2px; object-fit: cover; object-position: top; }
  .info { flex: 1; position: relative; }
  .info .name { position: absolute; right: 8px; top: 2px; font-size: 18px; white-space: nowrap; }
  .info .line2 { position: absolute; right: 8px; bottom: 3px; font-size: 13px; white-space: nowrap; }
  .info .pos { position: absolute; left: 6px; bottom: 3px; font-size: 13px; color: #9a9a9a; }
  .dim { color: #9a9a9a; } .live { color: #ffcc33; } .up { color: #3ddc84; } .down { color: #ff4d4d; }
  .retired { opacity: .45; }
  .more { height: 18px; background: #070707; text-align: center; font-size: 12px; color: #9a9a9a; line-height: 18px; }
</style>
</head>
<body>
<div id="board">
  <div class="series" id="series"></div>
  <div class="title" id="title"><span class="stripes"><i style="background:#e10600"></i><i style="background:#fff"></i><i style="background:#0b3fa5"></i></span>
    <span class="track" id="track"></span><span class="lap" id="lap"></span><span class="mode" id="mode"></span></div>
  <div id="stayed"></div>
  <div id="coming"></div>
  <div id="rows"></div>
  <div id="more"></div>
</div>
<script>
const q = new URLSearchParams(location.search);
const SRC = q.get('src') || (/^\/overlay\/?$/.test(location.pathname) ? location.origin + '/pits'
                                                                       : 'http://127.0.0.1:8767/pits');
const ASSET = SRC.replace(/\/pits.*$/, '/asset/');
const MAX = parseInt(q.get('rows') || '10');
const SCALE = parseFloat(q.get('scale') || '1');
const WIDTH = parseInt(q.get('width') || '280');
const ALWAYS = q.get('always') === '1';
const COMPACT = q.get('compact') === '1';
if (COMPACT) document.body.classList.add('compact');
const RH = COMPACT ? 24 : 40;
document.body.style.transform = `scale(${SCALE})`;
document.getElementById('board').style.setProperty('--w', WIDTH + 'px');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const asset = rel => rel ? ASSET + rel.split('/').map(encodeURIComponent).join('/') : '';
function textOn(hex) {
  const n = parseInt((hex || '#3a3a3a').slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? '#000' : '#fff';
}
function lighten(hex, t) {
  const n = parseInt((hex || '#3a3a3a').slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  const f = v => Math.round(v + (255 - v) * t);
  return `rgb(${f(r)},${f(g)},${f(b)})`;
}
function numHtml(d, cls) {
  const chip = `<span class="chip" style="background:${d.color || '#3a3a3a'};color:${textOn(d.color)}">${esc(d.number)}</span>`;
  return d.number_img ? `<img src="${asset(d.number_img)}" onerror="this.outerHTML=this.dataset.f" data-f='${chip}'>` : chip;
}
function row(d, mode) {
  const st = d.state, retired = st === 'retired';
  let right = '', left = '';
  if (retired) right = '<span class="dim">RETIRED</span>';
  else if (st === 'stall') { right = `<span class="live">${d.service.toFixed(1)}</span>`; left = `<span class="dim">${esc(d.label)}</span>`; }
  else if (st === 'exit') { right = esc(d.label); left = d.service ? `<span class="dim">${d.service.toFixed(1)}s</span>` : ''; }
  else if (st === 'out') {
    right = esc(d.label);
    if (d.service) left = `<span class="dim">${d.service.toFixed(1)}s</span>`;
  } else right = `<span class="dim">${st === 'in' ? 'THIS TIME BY' : 'PIT ROAD'}</span>`;
  const pos = st === 'out' ? d.pos_out : (d.pos_now || d.pos_in);
  const posHtml = pos && !retired ? `<span class="pos">P${pos}</span>` : '';
  const g = st === 'out' ? d.gained : null;
  const gainHtml = g ? `<span class="${g > 0 ? 'up' : 'down'}" style="font-size:14px">${g > 0 ? '+' + g + ' ▲' : g + ' ▼'}</span>&nbsp; ` : '';
  const bg = `linear-gradient(90deg, ${lighten(d.color, .35)}, ${d.color})`;
  return `<div class="row ${retired ? 'retired' : ''}">
    <div class="numcell" style="background:${bg}">${numHtml(d)}</div>
    ${d.headshot ? `<img class="head" src="${asset(d.headshot)}" onerror="this.style.visibility='hidden'">` : ''}
    <div class="info">${posHtml}<span class="name">${gainHtml}${esc(d.last)}</span>
      <span class="line2">${left}${left ? '&nbsp;&nbsp;' : ''}${right}</span></div></div>`;
}
let lastKeys = '';
async function tick() {
  try {
    const r = await fetch(SRC, { cache: 'no-store' });
    const data = await r.json();
    const stops = data.stops || [];
    const coming = data.coming_in || [];
    document.getElementById('board').classList.toggle('show', stops.length > 0 || coming.length > 0 || ALWAYS);
    document.getElementById('coming').innerHTML = [['THIS TIME BY', true], ['PITTING SOON', false]].map(([lbl, now]) => {
      const cars = coming.filter(c => c.this_time_by === now);
      return cars.length ? `<div class="soon ${now ? 'now' : ''}"><span class="lbl">${lbl}</span>` +
        cars.map(c => `<span class="n chip" style="background:${c.color};color:${textOn(c.color)}">${esc(c.number)}</span>`).join('') + '</div>' : '';
    }).join('');
    const ser = document.getElementById('series');
    if (data.logo) {
      if (ser.dataset.logo !== data.logo) {
        ser.innerHTML = `<img src="${asset(data.logo)}" alt="">`;
        ser.dataset.logo = data.logo;
      }
      ser.classList.add('logo');
    } else {
      ser.textContent = data.series_name || 'AIMOD';
      ser.dataset.logo = '';
      ser.classList.remove('logo');
    }
    document.getElementById('track').textContent = data.track || '';
    document.getElementById('lap').textContent = data.lap ? `LAP ${data.lap}/${data.total}` : '';
    document.getElementById('mode').textContent = data.mode === 'race_off' ? 'RACE OFF PIT ROAD' : 'PIT ROAD';
    document.getElementById('title').classList.toggle('caution', data.flag !== 'green');
    const stayed = (data.stayed_out || []).slice(0, 6);
    const n = (data.stayed_out || []).length;
    document.getElementById('stayed').innerHTML = stayed.map(d =>
      `<div class="stay"><span class="p">${d.pos}.</span><span class="n">${numHtml(d)}</span><span class="l">${esc(d.last)}</span></div>`
    ).join('') + (n ? `<div class="staybar">${n} DRIVER${n !== 1 ? 'S' : ''} STAYED OUT</div>` : '');
    const shown = stops.slice(0, MAX);
    const rows = document.getElementById('rows');
    rows.style.height = (shown.length * RH) + 'px';
    const keep = new Set();
    shown.forEach((d, i) => {
      const k = d.idx + '-' + d.stop;
      keep.add(k);
      const tmp = document.createElement('div');
      tmp.innerHTML = row(d, data.mode);
      const fresh = tmp.firstElementChild;
      let el = rows.querySelector(`[data-k="${k}"]`);
      if (!el) {                                   // new row: rise in from below its slot
        el = fresh; el.dataset.k = k;
        el.style.transition = 'none'; el.style.top = ((i + 1) * RH) + 'px';
        rows.appendChild(el);
        el.getBoundingClientRect();
        el.style.transition = '';
      } else if (el.innerHTML !== fresh.innerHTML || el.className !== fresh.className) {
        el.className = fresh.className; el.innerHTML = fresh.innerHTML;
      }
      el.style.zIndex = String(100 - i);
      el.style.top = (i * RH) + 'px';              // rows move up into freed slots
    });
    [...rows.children].forEach(el => { if (!keep.has(el.dataset.k)) el.remove(); });
    document.getElementById('more').innerHTML =
      stops.length > MAX ? `<div class="more">+${stops.length - MAX} more</div>` : '';
  } catch (e) {
    document.getElementById('board').classList.toggle('show', ALWAYS);
  }
}
setInterval(tick, 250);
tick();
</script>
</body>
</html>
"""
