/*
 * เทสต์ออฟไลน์ของหน้า overlay ที่อยู่ใน kick_events.py (ไม่ต้องมีเบราว์เซอร์ ไม่ต้องต่อเน็ต)
 *
 *   node tests/test_overlay_html.js
 *
 * วิธีทำงาน: ดึง HTML ของ overlay ออกมาจาก kick_events.py ตรง ๆ (ไม่ใช่สำเนา)
 * แล้วรันสคริปต์ในนั้นบน DOM จำลอง เพื่อพิสูจน์ว่า:
 *   1. id ที่สคริปต์อ้าง (box / stat) มีอยู่จริงใน HTML
 *   2. เปิด EventSource ไปที่ /events
 *   3. ข้อความจาก webhook ขึ้นเป็นกล่อง + ชื่อ + สีชื่อ
 *   4. [emote:ID:NAME] ของ Kick ถูกแปลงเป็น <img> ที่ถูก URL
 *   5. แบดจ์ขึ้นเป็นชิป และ kickTest() ใช้ทดสอบภาพใน OBS ได้
 *   6. จำนวนข้อความถูกจำกัด (ไม่โตไม่จำกัด)
 *
 * เกณฑ์ผ่าน: ไม่มีบรรทัด ❌ และ exit code = 0
 */
const { execFileSync } = require('child_process');
const path = require('path');

const repoDir = path.join(__dirname, '..');
const overlayHtml = execFileSync('python3', ['-c',
  'import sys; sys.path.insert(0, r"' + repoDir + '"); import kick_events as ke; sys.stdout.write(ke.OVERLAY_HTML)'],
  { encoding: 'utf8' });
const script = overlayHtml.split('<script>')[1].split('</script>')[0];

const htmlIds = new Set([...overlayHtml.matchAll(/id="([^"]+)"/g)].map(m => m[1]));

let ok = true;
const fail = (msg) => { console.log('❌ ' + msg); ok = false; };
const pass = (msg) => console.log('✅ ' + msg);

// ---------- DOM จำลองขนาดเล็ก ----------
function makeEl(tag) {
  const el = {
    tagName: tag, children: [], className: '', textContent: '', value: '', style: {},
    src: '', alt: '', title: '', removed: false, nodeValue: '',
    appendChild(c) {
      // ต่อ DocumentFragment = ย้ายลูกของ fragment เข้ามา (ตามสเปก DOM)
      if (c && c.tagName === 'fragment') { this.children.push(...c.children); c.children = []; return c; }
      this.children.push(c); return c;
    },
    removeChild(c) { this.children = this.children.filter(x => x !== c); },
    replaceWith(node) { if (this.parent) { const i = this.parent.children.indexOf(this); if (i >= 0) this.parent.children[i] = node; } },
    remove() { this.removed = true; },
    addEventListener() {}, scrollIntoView() {}, select() {},
    classList: { add() {}, remove() {}, toggle() {} },
    get firstChild() { return this.children[0]; },
    get childElementCount() { return this.children.length; },
  };
  return el;
}

const byId = {};
for (const id of htmlIds) byId[id] = makeEl('div');
const document = {
  getElementById(id) {
    if (!byId[id]) { byId[id] = makeEl('div'); }
    return byId[id];
  },
  createElement(tag) { const el = makeEl(tag); el.parent = null; return el; },
  createDocumentFragment() { return makeEl('fragment'); },
  createTextNode(text) { const n = makeEl('#text'); n.textContent = text; n.nodeValue = text; return n; },
  addEventListener() {}, querySelector() { return makeEl('div'); }, body: makeEl('body'),
};

let sse = null;
class FakeEventSource {
  constructor(url) { this.url = url; sse = this; setTimeout(() => this.onopen && this.onopen(), 0); }
  close() {}
  addEventListener() {}
}
const window = { addEventListener() {}, location: { href: 'http://localhost:8081/overlay', pathname: '/overlay' } };
const jsErrors = [];
const consoleShim = Object.assign({}, console, {
  error: (...args) => { jsErrors.push(args.map(String).join(' ')); },
});

const fn = new Function('document', 'window', 'EventSource', 'setTimeout', 'clearTimeout', 'console', 'location', script);
fn(document, window, FakeEventSource, setTimeout, clearTimeout, consoleShim, window.location);

// ---------- ตรวจ 1: id ที่อ้างมีจริง ----------
if (htmlIds.has('box') && htmlIds.has('stat')) pass('HTML มี id="box" และ id="stat" ตามที่สคริปต์อ้าง (' + [...htmlIds].join(', ') + ')');
else fail('HTML ขาด id ที่สคริปต์อ้าง — มี: ' + [...htmlIds].join(', '));

// ---------- ตรวจ 2: EventSource ----------
if (!sse) fail('หน้า overlay ไม่ได้เปิด EventSource');
else if (sse.url !== '/events') fail('EventSource ต่อผิด URL: ' + sse.url);
else pass('เปิด EventSource ไปที่ ' + sse.url);

// ---------- ส่ง event เข้ามาเหมือน webhook ยิงจริง ----------
const push = (obj) => { if (sse && sse.onmessage) sse.onmessage({ data: JSON.stringify(obj) }); };
const box = document.getElementById('box');
const flatten = (node, out = []) => { (node.children || []).forEach(c => { out.push(c); flatten(c, out); }); return out; };

push({ kind: 'chat', username: 'viewer', content: 'สวัสดี [emote:12345:KEKW] จบ', badges: ['moderator', 'sub'], color: '#53fc18' });
const last = box.children[box.children.length - 1];
if (!last) fail('ได้ event แล้วแต่ไม่มีกล่องข้อความถูกเพิ่มเข้ามา (box ยังว่าง)');
else {
  const nodes = flatten(last);
  const who = nodes.find(n => n.className === 'who');
  if (!who) fail('ไม่พบชื่อผู้ส่งในกล่องข้อความ');
  else if (who.style.color !== '#53fc18') fail('ไม่ใช้สีชื่อผู้ใช้จาก event: ' + who.style.color);
  else pass('ชื่อผู้ส่งขึ้นพร้อมสีจาก event: ' + who.style.color);

  const text = typeof last.textContent === 'string' && last.textContent ? last.textContent : nodes.map(n => n.textContent).join(' ');
  if (!/สวัสดี/.test(text) || !/จบ/.test(text)) fail('ข้อความไม่ครบ: ' + JSON.stringify(text).slice(0, 120));
  else pass('ข้อความแสดงครบ: ' + text.trim().replace(/\s+/g, ' ').slice(0, 60));

  const imgs = nodes.filter(n => n.tagName === 'img');
  if (!imgs.length) fail('อีโมต [emote:12345:KEKW] ไม่ถูกแปลงเป็น <img>');
  else if (imgs[0].src !== 'https://files.kick.com/emotes/12345/fullsize') fail('URL รูปอีโมตผิด: ' + imgs[0].src);
  else pass('อีโมตแปลงเป็นรูปถูกต้อง: ' + imgs[0].src + ' (alt=' + imgs[0].alt + ')');

  const badges = nodes.filter(n => n.className === 'badge');
  if (badges.length < 2) fail('แบดจ์ไม่ขึ้นครบ (ได้ ' + badges.length + ')');
  else pass('แบดจ์ขึ้นเป็นชิป: ' + badges.map(b => b.textContent).join(', '));
}

// ---------- ตรวจ 3: kickTest() สำหรับทดสอบภาพใน OBS ----------
if (typeof window.kickTest !== 'function') fail('ไม่มี window.kickTest() ให้ทดสอบภาพใน OBS');
else {
  const before = box.children.length;
  window.kickTest('tester', 'ข้อความทดสอบ');
  if (box.children.length <= before) fail('kickTest() ไม่ได้เพิ่มข้อความ');
  else pass('kickTest() เพิ่มข้อความทดสอบได้ (ใช้ตอนตั้ง OBS)');
}

// ---------- ตรวจ 4: เพดานจำนวนข้อความ ----------
for (let i = 0; i < 40; i++) push({ kind: 'chat', username: 'u' + i, content: 'x'.repeat(5), badges: [], color: null });
if (box.children.length > 12) fail('จำนวนข้อความไม่ถูกจำกัดตาม MAX=12 (ได้ ' + box.children.length + ')');
else pass('จำกัดจำนวนข้อความตามที่ประกาศไว้: ' + box.children.length + ' กล่อง');

// ---------- ตรวจ 5: event ผิดรูปต้องไม่ทำให้หน้าพัง ----------
push({ kind: 'chat' });
push({ nonsense: true });
push({ kind: 'stat', text: 'connected' });
if (box.children.length > 12) fail('event ผิดรูปทำให้จำนวนกล่องเกินเพดาน');
else pass('event ผิดรูปไม่ทำให้หน้าพัง (ยังมี ' + box.children.length + ' กล่อง)');

if (jsErrors.length) pass('บันทึก error ลง console ให้ตามงานได้: ' + jsErrors.length + ' ครั้ง');
console.log(ok ? '\nผ่านทั้งหมด' : '\nมีข้อที่ไม่ผ่าน');
process.exit(ok ? 0 : 1);
