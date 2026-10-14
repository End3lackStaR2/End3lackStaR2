/*
 * เทสต์ออฟไลน์ของ kick_probe.html (ไม่ต้องต่อเน็ต ไม่ต้องมีเบราว์เซอร์)
 *
 *   node tests/test_kick_probe_html.js
 *
 * วิธีทำงาน: จำลอง DOM ขนาดเล็ก + WebSocket ปลอมที่ทำตัวเหมือน Pusher
 * (รับเฉพาะ key 32cbd69e4b950bf97679 บน cluster us2) แล้วสั่งให้หน้าทำงานจริง
 * เพื่อพิสูจน์ 3 อย่าง:
 *   1. โค้ดอ้าง element id ทุกตัวมีอยู่จริงในไฟล์ HTML
 *   2. ช่อง "วางข้อความจาก DevTools" ดึง key/cluster ออกจาก URL ได้
 *   3. การไล่ทดสอบทุกคู่ทำงานครบ และรายงานคู่ที่ใช้ได้พร้อม URL จริง
 */
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'kick_probe.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];


const created = [];
function makeEl(tag) {
  const el = { tagName: tag, children: [], innerHTML: '', textContent: '', value: '', className: '', style: {},
    classList: { add(){}, remove(){}, toggle(){} },
    appendChild(child) { this.children.push(child); this.lastElementChild = child; return child; },
    addEventListener(){}, select(){}, focus(){}, remove(){}, scrollIntoView(){}, get lastElementChild() { return this._last; },
    set lastElementChild(v) { this._last = v; } };
  Object.defineProperty(el, 'lastElementChild', { get() { return this.children[this.children.length - 1]; }, configurable: true });
  let _html = '';
  Object.defineProperty(el, 'innerHTML', {
    get() { return _html; },
    set(v) {
      _html = v;
      // จำลองการที่เบราว์เซอร์แยก <td> ออกมาเป็นลูก (เพื่อให้ lastElementChild ใช้ได้)
      const cells = [...String(v).matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]);
      this.children.length = 0;
      for (const c of cells) { const cell = makeEl('td'); cell.innerHTML = c; cell.textContent = c.replace(/<[^>]+>/g, ''); this.children.push(cell); }
    },
    configurable: true,
  });
  created.push(el);
  return el;
}

// ---- DOM shim: เก็บ element ตาม id, ถ้าโค้ดอ้าง id ที่ไม่มีอยู่ จะ thrown ให้เห็นทันที ----
const els = {};
for (const m of html.matchAll(/id="([^"]+)"/g)) {
  els[m[1]] = { id: m[1], value: '', textContent: '', innerHTML: '', disabled: false, style: {},
                classList: { add(){}, remove(){}, toggle(){} },
                addEventListener(){}, appendChild(){}, scrollTop: 0, scrollHeight: 0 };
}
// ค่าเริ่มต้นจาก textarea ใน HTML
for (const m of html.matchAll(/<textarea id="([^"]+)"[^>]*>([\s\S]*?)<\/textarea>/g)) els[m[1]].value = m[2];
const missing = [];
const document = {
  getElementById(id) { if (!els[id]) missing.push(id); if (!els[id]) els[id] = { value:'', textContent:'', style:{}, classList:{add(){},remove(){}}, addEventListener(){} }; return els[id]; },
  querySelectorAll() { return []; }, addEventListener() {},
  querySelector(sel) { return els['__q' + sel] || (els['__q' + sel] = { id: sel, innerHTML:'', textContent:'', value:'', style:{}, classList:{add(){},remove(){}}, appendChild(){}, addEventListener(){}, rows: [], insertRow(){ const cells = new Proxy({}, { get(t,k){ if (typeof k === 'string' && !isNaN(Number(k))) return t[k] || (t[k] = { innerHTML:'', textContent:'', style:{}, classList:{add(){},remove(){}} }); return t[k]; } }); const r = new Proxy({ cells, style:{}, classList:{add(){},remove(){}} }, { get(t,k){ if (k === 'lastElementChild') return t.lastElementChild || (t.lastElementChild = { innerHTML:'', textContent:'', style:{}, classList:{add(){},remove(){}} }); if (!(k in t)) t[k] = function(){}; return t[k]; }, set(t,k,v){ t[k] = v; return true; } }); this.rows.push(r); return r; } }); },
  createElement(tag) { return makeEl(tag); },
  body: { appendChild(){} }
};

// ---- WebSocket shim: จำลอง Pusher — รับเฉพาะ 32cbd69e4b950bf97679 @ us2 ----
const GOOD_KEY = '32cbd69e4b950bf97679';
let opened = [];
class FakeWS {
  constructor(url) {
    this.url = url; this.readyState = 0; FakeWS.last = this;
    const m = url.match(/ws-([a-z0-9]+)\.pusher\.com\/app\/([0-9a-zA-Z]+)/);
    const cluster = m && m[1], key = m && m[2];
    opened.push({ cluster, key });
    setTimeout(() => {
      if (key === GOOD_KEY && cluster === 'us2') {
        this.readyState = 1; this.onopen && this.onopen({});
        this.onmessage && this.onmessage({ data: JSON.stringify({ event: 'pusher:connection_established', data: JSON.stringify({ socket_id: '456.789', activity_timeout: 30 }) }) });
      } else {
        this.readyState = 1; this.onopen && this.onopen({});
        this.onmessage && this.onmessage({ data: JSON.stringify({ event: 'pusher:error', data: JSON.stringify({ code: 4001, message: `App key ${key} not in this cluster. Did you forget to specify the cluster?` }) }) });
      }
    }, 1);
  }
  send() {} close() { this.readyState = 3; }
}
const window = { addEventListener(){}, location: { href: 'file:///kick_probe.html' } };
const navigator = { clipboard: { writeText: async () => {} } };

const sandbox = { document, window, navigator, WebSocket: FakeWS, setTimeout, clearTimeout, setInterval, clearInterval, console, location: window.location };
const fn = new Function('document','window','navigator','WebSocket','setTimeout','clearTimeout','setInterval','clearInterval','console','location', script);
fn(document, window, navigator, FakeWS, setTimeout, clearTimeout, setInterval, clearInterval, console, window.location);

function failExit(msg) { console.log('❌ ' + msg); console.log('\nมีข้อที่ไม่ผ่าน'); process.exit(1); }

(async () => {
  if (missing.length) failExit('โค้ดอ้าง element ที่ไม่มีใน HTML: ' + [...new Set(missing)].join(', '));
  console.log('✅ element ทุกตัวที่โค้ดอ้าง มีอยู่จริงใน HTML');

  // 1) ทดสอบการวางข้อความจาก DevTools
  els.pasted.value = '[{"name":"wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false"},{"name":"wss://ws-eu.pusher.com/app/34bf7a0ff419a2a775b9?x=1"}]';
  els.usePasted.onclick();
  const cfg = { keys: els.keys.value.split('\n').filter(Boolean), clusters: els.clusters.value.split('\n').filter(Boolean) };
  console.log('   ดึงได้ keys =', JSON.stringify(cfg.keys), '| clusters =', JSON.stringify(cfg.clusters));
  if (!cfg.keys.includes(GOOD_KEY)) failExit('ไม่ได้ key จาก URL');
  if (!cfg.clusters.includes('us2') || !cfg.clusters.includes('eu')) failExit('ไม่ได้ cluster จาก URL');

  // 2) ปล่อยให้ sweep ทำงานแล้วอ่านผลลัพธ์
  await new Promise(r => setTimeout(r, 4000));
  const out = (els.winner && els.winner.innerHTML) || '';
  const st = els.status ? els.status.textContent : '';
  console.log('   ทดสอบจริง', opened.length, 'คู่ (cluster×key)');
  const goodHit = opened.some(o => o.key === GOOD_KEY && o.cluster === 'us2');
  if (!goodHit) failExit('sweep ไม่ได้ทดสอบคู่ที่ถูก (us2 + key ที่ดี)');
  const text = JSON.stringify(out) + ' ' + st;
  if (!/32cbd69e4b950bf97679/.test(text)) failExit('ผลลัพธ์ไม่แสดง key ที่ชนะ | ' + text.slice(0, 400));
  if (!/us2/.test(text)) failExit('ผลลัพธ์ไม่แสดง cluster ที่ชนะ');
  console.log('✅ sweep ทดสอบทุกคู่ และรายงานผู้ชนะ (us2 + key) ออกทาง UI');
  console.log('   สถานะ:', st); console.log('   การ์ดผู้ชนะ:', text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 300));
  console.log('\nผ่านทั้งหมด');
  process.exit(0);
})();
