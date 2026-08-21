/* Settings page. Depends on picker.js (shared helpers + Plex picker). */
"use strict";

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

async function showCurrentServer() {
  const cfg = await (await fetch("/api/settings")).json();
  const p = cfg.plex;
  $("#plex-current").textContent = p.url
    ? `Current server: ${p.server_name || p.url}${p.server_name ? ` (${p.url})` : ""}`
    : "No Plex server configured yet.";
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

async function setPassword(pw) {
  const res = await fetch("/api/password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw }),
  });
  const out = await res.json();
  if (!res.ok) { toast(out.error || "Failed", true); return; }
  $("#pw-new").value = "";
  $("#pw-state").textContent = out.enabled ? "(enabled)" : "(off)";
  toast(out.enabled ? "Password set" : "Password removed");
}

async function showPasswordState() {
  const cfg = await (await fetch("/api/settings")).json();
  $("#pw-state").textContent = cfg.web.password ? "(enabled)" : "(off)";
}

window.onServerSaved = showCurrentServer;

$("#save").addEventListener("click", save);
$("#restart").addEventListener("click", restart);
$("#pw-set").addEventListener("click", () => {
  const pw = $("#pw-new").value;
  if (pw) setPassword(pw);
});
$("#pw-clear").addEventListener("click", () => {
  if (confirm("Remove the settings password?")) setPassword("");
});
showPasswordState().catch(() => {});

loadSettings().catch(() => toast("Could not load settings", true));
showCurrentServer().catch(() => {});
loadStatus();
setInterval(loadStatus, 10000);
