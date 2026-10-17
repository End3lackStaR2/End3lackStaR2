// The only place that talks to the local bridge.
// Doing the fetch here (not in the content script) avoids page CORS entirely:
// the extension origin has host permission for 127.0.0.1.

const DEFAULTS = { port: 8787, token: "" };

async function settings() {
  return await chrome.storage.local.get(DEFAULTS);
}

async function call(path, body) {
  const { port, token } = await settings();
  if (!token) throw new Error("no token saved - open the popup and paste it");

  // A dead bridge must not hang the service worker forever.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 120000);
  try {
    const res = await fetch(`http://127.0.0.1:${port}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Token": token },
      body: JSON.stringify(body || {}),
      cache: "no-store",
      signal: ctrl.signal,
    });
    const text = await res.text();
    let data;
    try {
      data = text ? JSON.parse(text) : { ok: false, reason: `http ${res.status}` };
    } catch (e) {
      data = { ok: false, reason: `bad json from bridge (http ${res.status})` };
    }
    if (res.status === 401) data.reason = "bridge rejected the token";
    return data;
  } finally {
    clearTimeout(timer);
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      switch (msg && msg.type) {
        case "attach":
          // The popup already resolved the tab id from a user gesture (activeTab).
          if (!msg.tabId) return sendResponse({ ok: false, reason: "no tab" });
          await chrome.scripting.executeScript({ target: { tabId: msg.tabId }, files: ["content.js"] });
          return sendResponse({ ok: true, tabId: msg.tabId });
        case "next":
          return sendResponse(await call("/next"));
        case "answer":
          return sendResponse(await call("/answer", { id: msg.id, text: msg.text, partial: !!msg.partial }));
        case "fail":
          return sendResponse(await call("/fail", { id: msg.id, reason: msg.reason }));
        case "cancel":
          return sendResponse(await call("/cancel"));
        case "health":
          return sendResponse(await call("/health"));
        default:
          return sendResponse({ ok: false, reason: "unknown message" });
      }
    } catch (e) {
      sendResponse({ ok: false, reason: String(e && e.message ? e.message : e) });
    }
  })();
  return true; // async response
});
