// Injected only when you press "Attach" in the popup, into the tab you chose.
// It types a question into the chat box, waits for the reply to stop changing,
// and hands the text back to the local bridge.
//
// No site-specific selectors: it hunts for the message box, then uses a marker
// that is visible in your chat on purpose - you can always see what Friday said.

(() => {
  if (window.__fridayBridgeAttached) return;
  window.__fridayBridgeAttached = true;

  const POLL_MS = 1500;
  const SETTLE_MS = 3000;      // reply counts as finished after this much silence
  const MAX_WAIT_MS = 8 * 60 * 1000;
  const FIND_BOX_MS = 15000;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const say = (...a) => console.log("%c[friday-bridge]", "color:#7c5cff", ...a);

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 8) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && parseFloat(s.opacity || "1") > 0.05;
  }

  function scoreBox(el) {
    const r = el.getBoundingClientRect();
    const hint = `${el.getAttribute("placeholder") || ""} ${el.getAttribute("aria-label") || ""} ` +
      `${el.getAttribute("data-placeholder") || ""}`.toLowerCase();
    let s = 1 - r.top / Math.max(1, innerHeight); // lowest on screen wins
    if (/message|ask|reply|prompt|chat|พิมพ์|ถาม/.test(hint)) s += 1.5;
    if (el.tagName === "TEXTAREA") s += 0.5;
    if (el.getAttribute("role") === "textbox") s += 0.2;
    if (el.readOnly || el.disabled) s -= 5;
    return s;
  }

  function findComposer() {
    const list = [...document.querySelectorAll(
      'textarea, [contenteditable="true"], [contenteditable=""], [role="textbox"]')].filter(visible);
    if (!list.length) return null;
    list.sort((a, b) => scoreBox(b) - scoreBox(a));
    return list[0];
  }

  function findContainer(composer) {
    let el = composer.parentElement;
    while (el && el !== document.body) {
      if (el.scrollHeight > el.clientHeight + 40) return el;
      el = el.parentElement;
    }
    return document.querySelector("main") || document.body;
  }

  function findSendButton(composer) {
    let root = composer.closest("form");
    if (!root) {
      root = composer.parentElement;
      for (let i = 0; i < 3 && root && root.parentElement; i++) root = root.parentElement;
    }
    const btns = [...(root || document).querySelectorAll("button, [role='button']")].filter(visible);
    let best = null;
    let bestScore = 0;
    for (const b of btns) {
      const hint = `${b.getAttribute("aria-label") || ""} ${b.getAttribute("title") || ""} ` +
        `${b.getAttribute("data-testid") || ""} ${b.textContent || ""}`.toLowerCase();
      let s = 0;
      if (/send|submit|arrow.?up|ส่ง/.test(hint)) s += 3;
      if (b.type === "submit") s += 1.5;
      if (/stop|cancel|attach|file|image|mic|voice/.test(hint)) s -= 8;
      if (b.disabled || b.getAttribute("aria-disabled") === "true") s -= 6;
      if (s > bestScore) { bestScore = s; best = b; }
    }
    return bestScore > 0 ? best : null;
  }

  function setText(el, text) {
    el.focus();
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
    let ok = false;
    try { ok = document.execCommand("insertText", false, text); } catch (e) { ok = false; }
    if (!ok) el.textContent = text;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
  }

  function pressEnter(el) {
    const base = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true };
    el.dispatchEvent(new KeyboardEvent("keydown", base));
    el.dispatchEvent(new KeyboardEvent("keypress", base));
    el.dispatchEvent(new KeyboardEvent("keyup", base));
  }

  function tidy(raw) {
    let t = raw.replace(/\r/g, "");
    const noise = /^(copy|copied|regenerate|retry|edit|share|like|dislike|good response|bad response|read aloud|new chat|stop generating)$/i;
    t = t.split("\n").filter((line) => !noise.test(line.trim())).join("\n");
    t = t.replace(/\n{3,}/g, "\n\n").trim();
    return t.slice(0, 100000);
  }

  function tell(message) {
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage(message, (r) => resolve(r || {})); } catch (e) { resolve({}); }
    });
  }

  async function handle(task) {
    const marker = `[friday:${String(task.id).slice(-6).toLowerCase()}]`;
    const text = `${task.text}\n\n${marker}`;
    say("taking task", task.id);

    let composer = null;
    const until = Date.now() + FIND_BOX_MS;
    while (Date.now() < until) {
      composer = findComposer();
      if (composer) break;
      await sleep(500);
    }
    if (!composer) {
      await tell({ type: "fail", id: task.id, reason: "no message box found on this page" });
      return;
    }

    const box = findContainer(composer);

    setText(composer, text);
    await sleep(150);
    const btn = findSendButton(composer);
    if (btn) btn.click(); else pressEnter(composer);
    await sleep(700);

    const leftover = `${composer.value ?? composer.innerText ?? ""}`;
    if (leftover.includes(marker)) pressEnter(composer); // click was swallowed

    const started = Date.now();
    let last = "";
    let lastChange = Date.now();
    let answer = "";

    while (Date.now() - started < MAX_WAIT_MS) {
      await sleep(700);
      const t = box.innerText || "";
      // lastIndexOf, not indexOf(offset): the newest question carries the newest
      // marker, and a scroll container can already hold text before it.
      const at = t.lastIndexOf(marker);
      if (at >= 0) {
        answer = t.slice(at + marker.length);
        if (answer !== last) { last = answer; lastChange = Date.now(); }
        const tail = answer.slice(-80);
        const streaming = /stop generating|stop response|หยุดสร้าง/i.test(tail);
        if (answer.trim() && Date.now() - lastChange > SETTLE_MS && !streaming) break;
      }
    }

    answer = tidy(answer);
    if (!answer) {
      await tell({ type: "fail", id: task.id, reason: "typed the question but no reply appeared" });
      return;
    }
    say("answer", task.id, `${answer.length} chars`);
    await tell({ type: "answer", id: task.id, text: answer });
  }

  (async () => {
    say("attached - polling the local bridge");
    while (true) {
      try {
        // Never type while you are not looking at the tab.
        if (document.visibilityState === "visible") {
          const res = await tell({ type: "next" });
          if (res && res.task) await handle(res.task);
        }
      } catch (e) {
        say("stopped:", e && e.message ? e.message : e);
        return;
      }
      await sleep(POLL_MS);
    }
  })();
})();
