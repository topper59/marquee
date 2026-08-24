/* Settings page. Depends on picker.js (shared helpers + Plex picker). */
"use strict";

const RESTART_NOTE = $("#restart-note").textContent;

/* ── Section navigation (iOS-settings style), driven by the URL hash ────
   Each section is its own little form: Save commits the visible section only,
   and leaving a section with unsaved edits offers to discard them. Saving
   across sections meant a change you could no longer see — a static IP, say —
   riding along with an unrelated one, and any error landing on a page you had
   already left. */
const SECTIONS = ["plex", "filters", "display", "ha", "network", "device"];
const SECTION_NAMES = {
  plex: "Plex server", filters: "What to show", display: "Display",
  ha: "Home Assistant", network: "Network", device: "Device",
};

let currentSection = "";
let restoringHash = false;

function sectionOf(el) {
  const page = el.closest("section.page");
  return page ? page.id.replace("sec-", "") : "";
}

function sectionFields(section) {
  return [...fields()].filter((el) => sectionOf(el) === section);
}

function pendingIn(section) {
  return sectionFields(section)
    .filter((el) => String(fieldValue(el)) !== el.dataset.initial);
}

/* Put a section's controls back to the values last loaded from the device. */
function revertSection(section) {
  sectionFields(section).forEach((el) => {
    if (el.type === "checkbox") el.checked = el.dataset.initial === "true";
    else el.value = el.dataset.initial;
  });
  syncSliders();
  syncGroups();
  applyTheme();
  showAccentDefault();
  if (section === "network") {
    showStaticFields();
    // Any complaint about the values just thrown away goes with them; a live
    // device-side error comes straight back on the next status poll.
    ipv4Error("");
  }
}

/* Save can only ever commit what is on screen, so say so plainly by going
   flat when the visible section has nothing pending. */
function updateSaveState() {
  $("#save").disabled = !currentSection || !pendingIn(currentSection).length;
}

function render() {
  $("#home").hidden = !!currentSection;
  SECTIONS.forEach((s) => { $("#sec-" + s).hidden = s !== currentSection; });
  $("#actionbar").hidden = !currentSection;
  updateSaveState();
  window.scrollTo(0, 0);
}

function navigate() {
  const id = location.hash.replace("#", "");
  const next = SECTIONS.includes(id) ? id : "";

  // Our own hash rewrite from a cancelled navigation; the view never moved.
  if (restoringHash) { restoringHash = false; return; }

  if (currentSection && next !== currentSection && pendingIn(currentSection).length) {
    if (!confirm(`Discard your unsaved changes to ${SECTION_NAMES[currentSection]}?`)) {
      restoringHash = true;
      location.hash = "#" + currentSection;
      return;
    }
    revertSection(currentSection);
  }
  // Walking away from the Plex section abandons any sign-in in progress —
  // this is the common way a link code got stranded on the LED panel — and
  // clears the results it left on screen, which otherwise greeted you again
  // on the next visit.
  if (currentSection === "plex" && next !== "plex") resetPicker();
  // A typed-but-unsaved password should not still be in the box either.
  if (currentSection === "device" && next !== "device") $("#pw-new").value = "";
  currentSection = next;
  render();
}

window.addEventListener("hashchange", navigate);
// Any edit can change whether there is something to save.
$("#settings").addEventListener("input", updateSaveState);
$("#settings").addEventListener("change", updateSaveState);

function netLabel(net) {
  switch (net.status) {
    case "online": return "Connected";
    case "connecting":
    case "joining": return "Connecting…";
    case "ap": return "In setup mode";
    case "error": return "Error";
    default: return net.ip ? "Connected" : "—";
  }
}

async function loadSettings() {
  const cfg = await (await fetch("/api/settings")).json();
  applyConfigToFields(cfg);
  showStaticFields();
  applyTheme();
  showAccentDefault();
  updateSaveState();
  $("#menu-ha-sub").textContent = cfg.ha.enabled ? "On" : "Off";
  $("#menu-filters-sub").textContent = filtersSummary(cfg.plex.filter);
  $("#menu-device-sub").textContent = cfg.device.name || "";
}

/* "Everything" unless a rule is actually doing something, so the menu row
   answers the question people come to this page with — is the panel hiding
   anything from me? */
function filtersSummary(f) {
  if (!f) return "";
  const lists = ["users", "ignore_users", "players", "ignore_players", "media_types"];
  const active = lists.filter((k) => (f[k] || []).length).length + (f.hide_paused ? 1 : 0);
  return active ? `${active} rule${active > 1 ? "s" : ""}` : "Everything";
}

/* Theme is previewed the moment it is picked — waiting for Save to find out
   what dark mode looks like would be silly — but it is still only *kept* by
   Save, so leaving the section without saving puts the old one back with the
   rest of the section (see revertSection). */
function applyTheme() {
  const sel = field("web.theme");
  if (sel) document.documentElement.dataset.theme = sel.value;
}

/* The static address fields only make sense under the manual method. */
function showStaticFields() {
  const sel = document.querySelector('[data-path="network.ipv4_method"]');
  $("#static-ip").hidden = !sel || sel.value !== "manual";
}

function field(path) {
  return document.querySelector(`[data-path="${path}"]`);
}

const IPV4_RE = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

function ipToInt(a) {
  return a.split(".").reduce((n, p) => (n * 256) + Number(p), 0);
}

/* Same rules the device enforces, run before the request so the answer is
   immediate and a half-filled static address never reaches the WiFi. */
function staticIpError() {
  if (field("network.ipv4_method").value !== "manual") return "";
  const addr = field("network.ipv4_address").value.trim();
  const gw = field("network.ipv4_gateway").value.trim();
  const prefix = Number(field("network.ipv4_prefix").value);
  const dns = field("network.ipv4_dns").value.trim();

  if (!addr) return "Enter an IP address, or switch back to automatic (DHCP).";
  if (!IPV4_RE.test(addr)) return `“${addr}” is not a valid IP address.`;
  if (!gw) return "Enter your router's address (the gateway).";
  if (!IPV4_RE.test(gw)) return `“${gw}” is not a valid router address.`;
  if (!(prefix >= 1 && prefix <= 32)) return "The prefix must be between 1 and 32.";

  const mask = prefix === 0 ? 0 : (-1 << (32 - prefix)) >>> 0;
  if ((ipToInt(addr) & mask) >>> 0 !== (ipToInt(gw) & mask) >>> 0) {
    return `The router ${gw} cannot be reached from ${addr}/${prefix}. ` +
           `Check the address and the prefix (24 is the usual one).`;
  }
  for (const one of dns.split(/[ ,]+/).filter(Boolean)) {
    if (!IPV4_RE.test(one)) return `“${one}” is not a valid DNS server address.`;
  }
  return "";
}

function ipv4Error(msg) {
  const el = $("#net-ipv4-error");
  el.textContent = msg;
  el.hidden = !msg;
  $("#net-ipv4-dismiss").hidden = !msg;
}

async function loadStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
    const card = $("#now-playing");
    const list = $("#np-list");
    list.innerHTML = "";
    $("#np-offline").hidden = !st.plex_offline;
    if (st.sessions && st.sessions.length) {
      st.sessions.forEach((s) => {
        const li = document.createElement("li");
        li.textContent = `${s.title} `;
        const who = document.createElement("span");
        who.className = "who";
        const on = s.player ? ` on ${s.player}` : "";
        who.textContent = `— ${s.user}${on}${s.state === "paused" ? " (paused)" : ""}`;
        li.appendChild(who);
        list.appendChild(li);
      });
      card.hidden = false;
    } else {
      // The card still has something to say when Plex is unreachable: an
      // empty page would read as "nothing is on", which is the confusion
      // this is here to prevent.
      card.hidden = !st.plex_offline;
    }
    renderSeen(st.sessions || []);
    const net = st.network || {};
    $("#net-status").textContent = netLabel(net);
    $("#net-ssid").textContent = net.ssid || "—";
    $("#net-ip").textContent = net.ip
      ? net.ip + (net.ipv4_method === "manual" ? " (static)" : "")
      : "—";
    $("#net-mac").textContent = net.mac || "—";
    $("#menu-network-sub").textContent = net.ip || "";
    // A rolled-back static address surfaces here, since the browser was very
    // likely disconnected at the moment the device gave up on it. The poll
    // only ever *sets* the message: clearing is explicit (dismiss, method
    // change, or a new attempt), so a validation error the user is reading
    // cannot be wiped by a background refresh.
    if (net.ipv4_error) ipv4Error(net.ipv4_error);
  } catch { /* status is decorative; ignore */ }
}

/* Plex's spelling of a username or a player name is not always what someone
   would type from memory, and a filter that silently matches nothing is the
   worst outcome here — so offer the real strings to click. */
function renderSeen(sessions) {
  const box = $("#filter-seen");
  const list = $("#filter-seen-list");
  const names = [...new Set(
    sessions.flatMap((s) => [s.user, s.player]).filter(Boolean))];
  list.innerHTML = "";
  names.forEach((name) => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = name;
    b.addEventListener("click", () => {
      // A player name goes to the player rule, a person to the people rule.
      const isPlayer = sessions.some((s) => s.player === name);
      const el = field(isPlayer ? "plex.filter.players" : "plex.filter.users");
      const have = el.value.split(",").map((t) => t.trim()).filter(Boolean);
      if (!have.some((t) => t.toLowerCase() === name.toLowerCase())) {
        have.push(name);
        el.value = have.join(", ");
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    li.appendChild(b);
    list.appendChild(li);
  });
  box.hidden = !names.length;
}

async function showCurrentServer() {
  const cfg = await (await fetch("/api/settings")).json();
  const p = cfg.plex;
  $("#plex-current").textContent = p.url
    ? `Current server: ${p.server_name || p.url}${p.server_name ? ` (${p.url})` : ""}`
    : "No Plex server configured yet.";
  $("#menu-plex-sub").textContent = p.url ? (p.server_name || p.url) : "Not set";
}

async function save() {
  const patch = {};
  pendingIn(currentSection).forEach((el) => {
    patch[el.dataset.path] = fieldValue(el);
  });
  if (!Object.keys(patch).length) { toast("Nothing to save"); return; }

  const touchesIpv4 = Object.keys(patch).some((p) => p.startsWith("network.ipv4"));
  if (touchesIpv4) {
    const bad = staticIpError();
    if (bad) {
      ipv4Error(bad);
      toast("Check the IP address settings", true);
      return;
    }
    // Superseded by this attempt, whatever the device last reported.
    ipv4Error("");
    await fetch("/api/network/clear-error", { method: "POST" }).catch(() => {});
  }

  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const out = await res.json();
  if (!res.ok) {
    // The device runs the same rules; if it still says no, keep the reason on
    // screen next to the fields rather than only in a toast that fades.
    if (touchesIpv4 && out.error) ipv4Error(out.error);
    toast(out.error || "Save failed", true);
    return;
  }
  const addressChanged = out.changed.some((p) => p.startsWith("network.ipv4"));
  await loadSettings();
  if (addressChanged) {
    // The WiFi is about to be re-raised with the new addressing, so this page
    // is about to lose its connection to the device.
    const addr = patch["network.ipv4_address"]
      || document.querySelector('[data-path="network.ipv4_address"]').value;
    const where = patch["network.ipv4_method"] === "manual" && addr
      ? `http://${addr}/` : "http://marquee.local/";
    $("#restart-note").hidden = false;
    $("#restart-note").textContent =
      `Reconnecting the display's WiFi — this page will go quiet for a few ` +
      `seconds. Reopen it at ${where} (or http://marquee.local/). If the ` +
      `address does not work, the display returns to automatic addressing.`;
    toast("Saved — changing the display's IP address");
  } else if (out.restart_required.length) {
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
  $("#restart-note").textContent = RESTART_NOTE;
  toast("Restarting…");
  setTimeout(loadSettings, 8000);
}

/* Factory reset. The confirmation names what is lost, in the order the user
   will miss it — a reset that takes the WiFi with it cannot be undone from
   this page, or from anywhere else on the network. */
async function factoryReset(keepWifi) {
  const question = keepWifi
    ? "Erase all settings and restart into setup?\n\n" +
      "The Plex server, display settings and password will be erased. The " +
      "display stays on this WiFi, so this page will come back at " +
      "http://marquee.local/ in about a minute."
    : "Factory reset the display?\n\n" +
      "This erases the Plex server, all display settings, the password AND " +
      "the saved WiFi network.\n\n" +
      "The display will leave your network and start its own " +
      "Marquee-Setup WiFi, so this page will stop working and you will " +
      "need a phone or laptop to set it up again.\n\n" +
      "This cannot be undone.";
  if (!confirm(question)) return;

  const res = await fetch("/api/factory-reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true, keep_wifi: !!keepWifi }),
  });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) { toast(out.error || "Could not start the reset", true); return; }

  // Nothing on this page is meaningful any more, and with the WiFi gone the
  // polls would only produce failures — stop them and say what happens next.
  clearInterval(statusTimer);
  $("#save").disabled = true;
  $("#restart-note").hidden = false;
  $("#restart-note").textContent = keepWifi
    ? "Erasing settings and restarting… this page will come back at " +
      "http://marquee.local/ in about a minute."
    : "Erasing everything… the display is leaving your WiFi and will start " +
      "its own Marquee-Setup network. Join that network from a phone or " +
      "laptop to set it up again.";
  toast("Factory reset started");
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

bindGroups();

/* The colour well gives no clue what the stock colour was, and "Plex amber"
   is not a thing anyone can pick out of a colour picker by eye. The button
   only appears when it would do something. */
const PLEX_AMBER = "#e5a00d";

function showAccentDefault() {
  $("#accent-default").hidden = field("display.accent").value === PLEX_AMBER;
}

$("#accent-default").addEventListener("click", () => {
  const el = field("display.accent");
  el.value = PLEX_AMBER;
  // Same path a manual pick takes, so the section goes dirty and Save picks
  // it up — resetting the colour is an edit like any other, not an action.
  el.dispatchEvent(new Event("change", { bubbles: true }));
});

field("display.accent").addEventListener("change", showAccentDefault);

field("web.theme").addEventListener("change", applyTheme);

field("network.ipv4_method").addEventListener("change", () => {
  showStaticFields();
  ipv4Error("");
});
$("#net-ipv4-dismiss").addEventListener("click", async () => {
  ipv4Error("");
  await fetch("/api/network/clear-error", { method: "POST" }).catch(() => {});
});

$("#factory-reset").addEventListener("click", () => factoryReset(false));
$("#reset-keep-wifi").addEventListener("click", () => factoryReset(true));

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

navigate();
loadSettings().catch(() => toast("Could not load settings", true));
showCurrentServer().catch(() => {});
loadStatus();
const statusTimer = setInterval(loadStatus, 10000);
