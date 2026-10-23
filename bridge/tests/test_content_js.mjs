// Headless test of bridge/extension/content.js against bridge/mock_chat.html.
//
// What this proves: given a page with a real <form>/<textarea>/<button>, the
// content script finds the composer, types the question with its marker, waits
// for the transcript to settle, and posts the reply back with the right id.
// What it cannot prove: real Chrome layout/paint, or Arena's actual DOM.
//
// Run:  npm install jsdom && node test_content_js.mjs
//   or: NODE_PATH=/path/to/node_modules node test_content_js.mjs

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PAGE = path.resolve(HERE, "..", "mock_chat.html");
const SCRIPT = path.resolve(HERE, "..", "extension", "content.js");

// CJS loader so a normal `npm install jsdom` *or* NODE_PATH=... both work
// (ESM import ignores NODE_PATH).
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (e) {
  console.log("SKIP: jsdom is not installed (npm install jsdom)");
  process.exit(0);
}

let pass = 0;
let fail = 0;
const check = (name, cond, extra = "") => {
  if (cond) {
    pass += 1;
    console.log(`  [PASS] ${name}`);
  } else {
    fail += 1;
    console.log(`  [FAIL] ${name} ${extra}`);
  }
};

const html = fs.readFileSync(PAGE, "utf8");
const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "http://127.0.0.1:8799/" });
const { window } = dom;
const { document } = window;

// --- jsdom gaps that matter for this script -------------------------------
// 1) no layout: give the composer and transcript believable boxes
const rect = (el, { top = 600, left = 10, width = 500, height = 52 } = {}) =>
  el.getBoundingClientRect = () => ({
    top, left, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON() {},
  });
rect(document.getElementById("composer"), { top: 620, height: 52, width: 480 });
const transcript = document.getElementById("transcript");
rect(transcript, { top: 60, height: 520, width: 900 });
Object.defineProperty(transcript, "scrollHeight", { get: () => 900 });
Object.defineProperty(transcript, "clientHeight", { get: () => 520 });

// 2) jsdom has no innerText; the script reads it on purpose (real browsers do)
Object.defineProperty(window.HTMLElement.prototype, "innerText", {
  get() { return this.textContent; },
  set(v) { this.textContent = v; },
  configurable: true,
});

// 3) no chrome API: stand in for the background worker
const seen = { nextCalls: 0, answer: null, failures: [] };
window.chrome = {
  runtime: {
    sendMessage(msg, cb) {
      setTimeout(() => {
        if (msg.type === "next") {
          seen.nextCalls += 1;
          cb(seen.nextCalls === 1 ? { ok: true, task: { id: "A-20261017-120000-abcdef", text: "สวัสดี mock" } } : { ok: true, task: null });
        } else if (msg.type === "answer") {
          seen.answer = msg;
          cb({ ok: true });
        } else if (msg.type === "fail") {
          seen.failures.push(msg);
          cb({ ok: true });
        } else {
          cb({ ok: false });
        }
      }, 0);
    },
  },
};
Object.defineProperty(document, "visibilityState", { get: () => "visible", configurable: true });
globalThis.chrome = window.chrome;

// silence the script's own logging
const logs = [];
window.console.log = (...a) => logs.push(a.join(" "));

// --- run -------------------------------------------------------------------
const content = fs.readFileSync(SCRIPT, "utf8");
window.eval(content);
console.log("content.js loaded into a fake page\n");

const waitFor = async (fn, ms) => {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    if (fn()) return true;
    await new Promise((r) => setTimeout(r, 100));
  }
  return false;
};

const picked = await waitFor(() => seen.answer !== null, 20000);

const typed = document.querySelectorAll(".msg.me");
check("found the composer and sent the question", typed.length === 1, `msgs=${typed.length}`);
check("the question carries a [friday:…] marker",
  typed.length === 1 && /\[friday:[a-z0-9]+\]/.test(typed[0].textContent),
  typed.length ? typed[0].textContent.slice(0, 80) : "(nothing sent)");
check("the composer was cleared after sending",
  typed.length === 1 && document.getElementById("composer").value === "");
check("the reply was posted back", picked && seen.answer && seen.answer.id === "A-20261017-120000-abcdef",
  JSON.stringify(seen.answer));
check("the posted reply contains the mock answer",
  !!(seen.answer && /คำตอบจำลอง #1/.test(seen.answer.text)), seen.answer ? seen.answer.text.slice(0, 90) : "");
check("the posted reply is not the echoed question",
  !!(seen.answer && !/สวัสดี mock/.test(seen.answer.text)), seen.answer ? seen.answer.text.slice(0, 60) : "");
check("no failure was reported", seen.failures.length === 0, JSON.stringify(seen.failures));

console.log(`\ncontent.js test: ${pass} passed, ${fail} failed`);
dom.window.close();
process.exit(fail === 0 ? 0 : 1);
