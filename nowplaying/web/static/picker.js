/* Shared helpers + the Plex server picker (used by both setup and settings).
   Pages can set window.onServerSaved to react when a server is chosen. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const fields = () => document.querySelectorAll("[data-path]");

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}

function fieldValue(el) {
  return el.type === "checkbox" ? el.checked : el.value;
}

/* ── Slider ⇄ number pairs ─────────────────────────────────────────────────
   The range input carries the data-path (so it is the value that gets saved);
   a companion `data-slider-for` number input mirrors it so a value can also be
   typed exactly. */
function syncSliders() {
  document.querySelectorAll("input[type=range][data-path]").forEach((r) => {
    const num = document.querySelector(`[data-slider-for="${r.dataset.path}"]`);
    if (num) num.value = r.value;
  });
}

function bindSliders() {
  document.querySelectorAll("input[type=range][data-path]").forEach((r) => {
    const num = document.querySelector(`[data-slider-for="${r.dataset.path}"]`);
    if (!num) return;
    r.addEventListener("input", () => { num.value = r.value; });
    const push = () => {
      const lo = Number(r.min), hi = Number(r.max);
      let v = Number(num.value);
      if (!Number.isFinite(v)) return;
      v = Math.min(hi, Math.max(lo, Math.round(v)));
      num.value = v;
      r.value = v;
    };
    // input keeps the slider tracking as digits are typed; change clamps once
    // the field is committed, so a half-typed "1" of "100" is not snapped.
    num.addEventListener("input", () => { r.value = num.value; });
    num.addEventListener("change", push);
  });
}

/* Fill every [data-path] control from a settings snapshot and remember the
   loaded value, so save() can send only what actually changed. */
function applyConfigToFields(cfg) {
  fields().forEach((el) => {
    const v = getPath(cfg, el.dataset.path);
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v == null ? "" : v;
    el.dataset.initial = String(fieldValue(el));
  });
  syncSliders();
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 4000);
}

function pickerMsg(msg, isError = false) {
  const el = $("#plex-picker-msg");
  el.textContent = msg;
  el.style.color = isError ? "var(--danger)" : "";
  el.hidden = !msg;
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
      if (window.onServerSaved) window.onServerSaved();
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

let linkTimer = null;

/* Abandoning the link flow has to tell the device, or the code stays on the
   LED panel after the user gives up on it — for the full PIN lifetime, which
   is a quarter of an hour of a stale code on a display in someone's room.
   Every way out routes through here: the Cancel button, configuring the
   server another way, and leaving the page (see linkOpen callers below). */
function linkOpen() {
  const panel = $("#plex-link");
  return !!panel && !panel.hidden;
}

async function cancelLink() {
  clearInterval(linkTimer);
  linkTimer = null;
  if (linkOpen()) {
    $("#plex-link").hidden = true;
    try { await fetch("/api/plex/auth/cancel", { method: "POST" }); } catch {}
  }
}

async function plexScan() {
  const btn = $("#plex-scan");
  btn.disabled = true; btn.textContent = "Scanning…";
  await cancelLink();
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
      if (window.onServerSaved) window.onServerSaved();
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

/* Everything the picker puts on screen — the message line, the candidate
   list, a code panel — is the result of something the user just did in this
   section. None of it should still be sitting there on a later visit, so
   leaving the section wipes the lot. */
async function resetPicker() {
  await cancelLink();
  pickerMsg("");
  const ul = $("#plex-candidates");
  if (ul) { ul.innerHTML = ""; ul.hidden = true; }
}

function bindPicker() {
  if (!$("#plex-scan")) return;
  $("#plex-scan").addEventListener("click", plexScan);
  $("#plex-signin").addEventListener("click", () => startLink(null));
  $("#plex-link-cancel").addEventListener("click", async () => {
    await cancelLink();
    pickerMsg("Sign-in cancelled — the code has been taken off the display.");
  });
  $("#plex-use").addEventListener("click", async () => {
    const url = $("#plex-manual").value.trim();
    if (!url) return;
    await cancelLink();
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
}

/* Closing the tab or reloading kills the poll loop, so a code left on the
   panel would have nothing watching it. sendBeacon still gets the cancel out
   during unload, when a normal fetch would be abandoned. */
window.addEventListener("pagehide", () => {
  if (linkOpen() && navigator.sendBeacon) {
    navigator.sendBeacon("/api/plex/auth/cancel");
  }
});

bindPicker();
bindSliders();
