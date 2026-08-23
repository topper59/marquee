/* First-run wizard. Depends on picker.js. The visible step is derived from
   device state on every poll, so the flow survives the WiFi handoff (phone
   drops off the setup AP, rejoins home WiFi, reopens nowplaying.local). */
"use strict";

let chosenSsid = null;
let handedOff = false;

function showStep(id) {
  document.querySelectorAll(".step").forEach((el) => { el.hidden = el.id !== id; });
}

function wifiMsg(msg, isError = false) {
  const el = $("#wifi-msg");
  el.textContent = msg;
  el.style.color = isError ? "var(--danger)" : "";
  el.hidden = !msg;
}

function renderNetworks(nets) {
  const ul = $("#wifi-list");
  ul.innerHTML = "";
  nets.forEach((n) => {
    const li = document.createElement("li");
    const bars = n.signal > 66 ? "▂▄▆" : n.signal > 33 ? "▂▄" : "▂";
    const label = document.createElement("span");
    label.innerHTML = `<b>${n.ssid}</b> <span class="meta">${bars}${n.secured ? " 🔒" : ""}</span>`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Join";
    btn.addEventListener("click", () => chooseNetwork(n.ssid, n.secured));
    li.append(label, btn);
    ul.appendChild(li);
  });
}

function chooseNetwork(ssid, secured) {
  chosenSsid = ssid;
  $("#wifi-join").hidden = false;
  $("#wifi-chosen").innerHTML = `Joining <b>${ssid}</b>`;
  $("#wifi-psk").value = "";
  $("#wifi-psk").parentElement.hidden = !secured;
  if (secured) $("#wifi-psk").focus();
}

async function scanWifi() {
  const out = await (await fetch("/api/wifi/scan")).json();
  renderNetworks(out.networks || []);
  if (!(out.networks || []).length) {
    wifiMsg("No networks in the list — use the manual field below.");
  }
}

async function joinWifi() {
  if (!chosenSsid) return;
  const res = await fetch("/api/wifi/join", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ssid: chosenSsid, psk: $("#wifi-psk").value }),
  });
  const out = await res.json();
  if (!res.ok) { wifiMsg(out.error || "Could not start joining", true); return; }
  handedOff = true;
  $("#handoff-ssid").textContent = chosenSsid;
  showStep("step-handoff");
}

async function finish() {
  const patch = { "provisioned": true };
  fields().forEach((el) => {
    const cur = fieldValue(el);
    if (String(cur) !== el.dataset.initial) patch[el.dataset.path] = cur;
  });
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const out = await res.json();
  if (!res.ok) { toast(out.error || "Could not save", true); return; }
  toast("Setup complete!");
  setTimeout(() => location.reload(), 1200);   // reloads into the settings page
}

async function loadPrefs() {
  applyConfigToFields(await (await fetch("/api/settings")).json());
}

let prefsLoaded = false;

async function tick() {
  let st;
  try {
    st = await (await fetch("/api/status")).json();
  } catch {
    return; // mid-handoff; keep the current screen
  }
  const net = st.network || { status: "passive" };
  if (net.error && (net.status === "ap")) {
    wifiMsg(net.error, true);
  }
  if (handedOff && net.status === "joining") return;
  if (net.status === "ap") {
    handedOff = false;
    if ($("#step-wifi").hidden) { showStep("step-wifi"); scanWifi(); }
  } else if (net.status === "joining" || net.status === "connecting") {
    showStep("step-handoff");
  } else if (!st.plex_url) {
    handedOff = false;
    showStep("step-plex");
  } else {
    handedOff = false;
    if ($("#step-prefs").hidden) {
      if (!prefsLoaded) { prefsLoaded = true; loadPrefs(); }
      showStep("step-prefs");
    }
  }
}

window.onServerSaved = tick;

$("#wifi-rescan").addEventListener("click", scanWifi);
$("#wifi-connect").addEventListener("click", joinWifi);
$("#wifi-manual-use").addEventListener("click", () => {
  const ssid = $("#wifi-manual").value.trim();
  if (ssid) chooseNetwork(ssid, true);
});
$("#finish").addEventListener("click", finish);

tick();
setInterval(tick, 3000);
