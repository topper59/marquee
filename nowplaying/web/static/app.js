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

/* ── Plex server picker ─────────────────────────────────────────────── */

function pickerMsg(msg, isError = false) {
  const el = $("#plex-picker-msg");
  el.textContent = msg;
  el.style.color = isError ? "var(--danger)" : "";
  el.hidden = !msg;
}

async function showCurrentServer() {
  const cfg = await (await fetch("/api/settings")).json();
  const p = cfg.plex;
  $("#plex-current").textContent = p.url
    ? `Current server: ${p.server_name || p.url}${p.server_name ? ` (${p.url})` : ""}`
    : "No Plex server configured yet.";
}

function renderCandidates(servers, useHandler) {
  const ul = $("#plex-candidates");
  ul.innerHTML = "";
  servers.forEach((s) => {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.innerHTML = `<b>${s.name || s.url}</b> <span class="meta">${
      s.url || (s.connections || [])[0] || ""}${s.auth_required ? " · needs sign-in" : ""}</span>`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Use";
    btn.addEventListener("click", () => useHandler(s, btn));
    li.append(label, btn);
    ul.appendChild(li);
  });
  ul.hidden = !servers.length;
}

async function selectServer(body, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Checking…"; }
  try {
    const res = await fetch("/api/plex/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const out = await res.json();
    if (out.saved) {
      pickerMsg("");
      toast(`Now using ${out.url}`);
      $("#plex-candidates").hidden = true;
      await showCurrentServer();
      return true;
    }
    pickerMsg(out.error || "Could not use that server", true);
    return false;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Use"; }
  }
}

async function useCandidate(s, btn) {
  if (s.auth_required) {
    await startLink({ url: s.url, name: s.name, machine_id: s.machine_id });
  } else if (s.connections) {
    await selectServer({ urls: s.connections, name: s.name,
                         machine_id: s.machine_id }, btn);
  } else {
    await selectServer({ url: s.url, name: s.name, machine_id: s.machine_id }, btn);
  }
}

async function scan() {
  const btn = $("#plex-scan");
  btn.disabled = true; btn.textContent = "Scanning…";
  pickerMsg("");
  try {
    const out = await (await fetch("/api/plex/discover", { method: "POST" })).json();
    renderCandidates(out.servers, useCandidate);
    if (!out.servers.length) {
      pickerMsg("No servers answered on this network. If your server is on " +
                "another subnet, enter its address below or sign in with Plex.");
    }
  } finally {
    btn.disabled = false; btn.textContent = "Find servers";
  }
}

let linkTimer = null;

async function startLink(server) {
  clearInterval(linkTimer);
  const res = await fetch("/api/plex/auth/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(server ? { server } : {}),
  });
  const out = await res.json();
  if (!res.ok) { pickerMsg(out.error || "Could not start sign-in", true); return; }
  $("#plex-link").hidden = false;
  $("#plex-code").textContent = out.code;
  $("#plex-link-status").textContent = "Waiting for you to enter the code…";
  linkTimer = setInterval(pollLink, 3000);
}

async function pollLink() {
  const res = await fetch("/api/plex/auth/poll", { method: "POST" });
  const out = await res.json();
  if (out.claimed) {
    clearInterval(linkTimer);
    if (out.saved) {
      $("#plex-link").hidden = true;
      toast(`Now using ${out.server}`);
      $("#plex-candidates").hidden = true;
      await showCurrentServer();
    } else if (out.servers) {
      $("#plex-link").hidden = true;
      if (out.servers.length) {
        pickerMsg("Signed in — pick your server:");
        renderCandidates(out.servers, useCandidate);
      } else {
        pickerMsg("Signed in, but no servers are on this Plex account.", true);
      }
    } else {
      $("#plex-link").hidden = true;
      pickerMsg(out.error || "Sign-in finished but the server rejected the token", true);
    }
  } else if (out.expired) {
    clearInterval(linkTimer);
    $("#plex-link-status").textContent = "Code expired — start again.";
  }
}

$("#plex-scan").addEventListener("click", scan);
$("#plex-signin").addEventListener("click", () => startLink(null));
$("#plex-use").addEventListener("click", async () => {
  const url = $("#plex-manual").value.trim();
  if (!url) return;
  const probe = await (await fetch("/api/plex/probe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  })).json();
  if (!probe.ok) { pickerMsg(probe.error || "No Plex server there", true); return; }
  if (probe.auth_required) {
    await startLink({ url: probe.url, machine_id: probe.machine_id });
  } else {
    await selectServer({ url: probe.url, machine_id: probe.machine_id }, $("#plex-use"));
  }
});

$("#save").addEventListener("click", save);
$("#restart").addEventListener("click", restart);
showCurrentServer().catch(() => {});

loadSettings().catch(() => toast("Could not load settings", true));
loadStatus();
setInterval(loadStatus, 10000);
