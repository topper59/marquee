"use strict";

const $ = (sel) => document.querySelector(sel);
const fields = () => document.querySelectorAll("[data-path]");

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}

function fieldValue(el) {
  return el.type === "checkbox" ? el.checked : el.value;
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 4000);
}

async function loadSettings() {
  const res = await fetch("/api/settings");
  const cfg = await res.json();
  fields().forEach((el) => {
    const v = getPath(cfg, el.dataset.path);
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v == null ? "" : v;
    el.dataset.initial = String(fieldValue(el));
  });
}

async function loadStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
    const card = $("#now-playing");
    const list = $("#np-list");
    list.innerHTML = "";
    if (st.sessions && st.sessions.length) {
      st.sessions.forEach((s) => {
        const li = document.createElement("li");
        li.textContent = `${s.title} `;
        const who = document.createElement("span");
        who.className = "who";
        who.textContent = `— ${s.user}${s.state === "paused" ? " (paused)" : ""}`;
        li.appendChild(who);
        list.appendChild(li);
      });
      card.hidden = false;
    } else {
      card.hidden = true;
    }
  } catch { /* status is decorative; ignore */ }
}

async function save() {
  const patch = {};
  fields().forEach((el) => {
    const cur = fieldValue(el);
    if (String(cur) !== el.dataset.initial) patch[el.dataset.path] = cur;
  });
  if (!Object.keys(patch).length) { toast("Nothing to save"); return; }

  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const out = await res.json();
  if (!res.ok) { toast(out.error || "Save failed", true); return; }
  await loadSettings();
  if (out.restart_required.length) {
    $("#restart-note").hidden = false;
    toast("Saved — restart needed for some changes");
  } else {
    toast("Saved");
  }
}

async function restart() {
  if (!confirm("Restart the display app? The panel will blank for a few seconds.")) return;
  await fetch("/api/restart", { method: "POST" });
  $("#restart-note").hidden = true;
  toast("Restarting…");
  setTimeout(loadSettings, 8000);
}

$("#save").addEventListener("click", save);
$("#restart").addEventListener("click", restart);

loadSettings().catch(() => toast("Could not load settings", true));
loadStatus();
setInterval(loadStatus, 10000);
