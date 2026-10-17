const $ = (id) => document.getElementById(id);
const setStatus = (text, cls = "") => {
  const el = $("status");
  el.textContent = text;
  el.className = cls;
};

async function load() {
  const s = await chrome.storage.local.get({ port: 8787, token: "" });
  $("port").value = s.port;
  $("token").value = s.token;
}

async function save() {
  const port = parseInt($("port").value, 10) || 8787;
  const token = $("token").value.trim();
  await chrome.storage.local.set({ port, token });
  setStatus("บันทึกแล้ว", "ok");
  return { port, token };
}

$("save").addEventListener("click", save);

$("check").addEventListener("click", async () => {
  await save();
  setStatus("กำลังทดสอบ…");
  const h = await chrome.runtime.sendMessage({ type: "health" });
  if (!h || h.ok !== true) {
    setStatus(`เชื่อมไม่ได้: ${(h && h.reason) || "ไม่ทราบสาเหตุ"}\nรัน bridge ไว้หรือยัง?`, "bad");
    return;
  }
  const seen = h.last_seen_seconds == null ? "ยังไม่มีแท็บไหนมาเกาะ" : `เห็นแท็บล่าสุด ${h.last_seen_seconds}s ที่แล้ว`;
  setStatus(`bridge ตอบ OK\n${h.attached ? "มีแท็บเกาะอยู่" : "ยังไม่มีแท็บเกาะ"} · ${seen}\ncิว ${h.pending} · ถามแล้ว ${h.asked} · ตอบแล้ว ${h.answered}`, "ok");
});

$("attach").addEventListener("click", async () => {
  await save();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) return setStatus("ไม่พบแท็บปัจจุบัน", "bad");
  const r = await chrome.runtime.sendMessage({ type: "attach", tabId: tab.id });
  if (r && r.ok) {
    setStatus("เกาะแท็บนี้แล้ว\nโพสต์/ข้อความที่ Friday ถามจะพิมพ์ลงช่องแชทของแท็บนี้ (มีป้าย [friday:xxxxxx] ต่อท้าย)", "ok");
  } else {
    setStatus(`เกาะไม่สำเร็จ: ${(r && r.reason) || "ไม่ทราบสาเหตุ"}`, "bad");
  }
});

load();
