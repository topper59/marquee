/* Settings-page logic tests. Unlike the Python suite these run on the dev
   machine — no panel, no Plex, no device:
 *
 *     npm install jsdom && node tests/web_ui_test.js
 *
 * The page is built from the real templates and driven with the real
 * picker.js/app.js, with fetch and confirm stubbed. Covers the parts of the
 * UI that are logic rather than looks: which section a Save commits, the
 * unsaved-changes guard, and static-IP validation.
 */
const fs = require("fs");
const { JSDOM } = require("jsdom");
const ROOT = __dirname + "/../nowplaying/web/";
const SRC = ROOT + "static/";
const TPL = ROOT + "templates/";

const CFG = {
  device: { name: "NowPlaying" }, web: { password: null, theme: "auto" },
  plex: { url: "http://x", server_name: "S", verify_ssl: false, poll_seconds: 5 },
  display: { cycle_seconds: 10, brightness_normal: 60, brightness_dim: 20,
             schedule_start: "00:00", schedule_stop: "00:00" },
  ha: { enabled: false, require_sunset: true, url: "", token: "", tv_entity: "" },
  network: { ipv4_method: "auto", ipv4_address: "", ipv4_prefix: 24,
             ipv4_gateway: "", ipv4_dns: "" },
  log_level: "INFO",
};
const STATUS = { sessions: [], network: { status: "online", ip: "192.168.2.129",
                 ssid: "Wifi", mac: "DC:A6:32:02:9C:47", ipv4_method: "auto",
                 ipv4_error: "" } };

let posted = [];
let confirmAnswer = true;
let confirmed = [];

/* The templates are the source of truth for the markup app.js reaches into,
   so build the page from them rather than from a copy that can drift. Only
   Jinja's tags need removing — none of it is control flow on this page. */
function renderTemplate(name) {
  const raw = fs.readFileSync(TPL + name, "utf8");
  return raw.replace(/\{%[^%]*%\}/g, "").replace(/\{\{[^}]*\}\}/g, "");
}

const page = renderTemplate("base.html").replace(
  "</main>", renderTemplate("settings.html") + "</main>");

const dom = new JSDOM(page, {
  url: "http://nowplaying.local/", runScripts: "outside-only",
});
const w = dom.window;
w.scrollTo = () => {};
w.confirm = (msg) => { confirmed.push(msg); return confirmAnswer; };
w.alert = () => {};
w.fetch = async (url, opts) => {
  const post = !!(opts && opts.method === "POST");
  const body = post ? JSON.parse(opts.body || "{}") : {};
  if (post) posted.push({ url, body });
  let json = { ok: true };
  if (url === "/api/settings") {
    json = post ? { changed: Object.keys(body), restart_required: [] } : CFG;
  } else if (url === "/api/status") {
    json = STATUS;
  } else if (url === "/api/plex/auth/start") {
    json = { code: "WXYZ" };
  } else if (url === "/api/plex/auth/poll") {
    json = { claimed: false };
  }
  return { ok: true, json: async () => json };
};
// One eval: separate ones do not share top-level const/function scope.
w.eval(fs.readFileSync(SRC + "picker.js", "utf8") + "\n" +
       fs.readFileSync(SRC + "app.js", "utf8") + "\n" +
       "window.__save = save;\nwindow.__renderCandidates = renderCandidates;");

const $ = (s) => w.document.querySelector(s);
const renderCandidatesForTest = () =>
  w.__renderCandidates([{ name: "Tower", url: "http://a:32400" }], () => {});
const field = (p) => w.document.querySelector(`[data-path="${p}"]`);
let fails = 0;
const check = (name, cond) => {
  if (!cond) fails++;
  console.log(`  ${cond ? "ok  " : "FAIL"}  ${name}`);
};
const go = (hash) => { w.location.hash = hash; };
const settle = () => new Promise((r) => setTimeout(r, 30));
const edit = (path, value) => {
  const el = field(path);
  if (el.type === "checkbox") el.checked = value;
  else el.value = value;
  el.dispatchEvent(new w.Event("input", { bubbles: true }));
  el.dispatchEvent(new w.Event("change", { bubbles: true }));
};

(async () => {
  await settle();
  check("loads with fields populated", field("display.cycle_seconds").value === "10");

  console.log("save is scoped to the visible section");
  go("#display"); await settle();
  check("save disabled on a pristine section", $("#save").disabled);
  edit("display.cycle_seconds", "25");
  check("save enabled once something changes", !$("#save").disabled);

  // Leave with the edit pending; accept the discard.
  confirmAnswer = true; confirmed = [];
  go("#ha"); await settle();
  check("leaving a dirty section asks", confirmed.length === 1 &&
        /Display/.test(confirmed[0]));
  check("discard reverts the field", field("display.cycle_seconds").value === "10");
  check("save disabled again on the new section", $("#save").disabled);

  console.log("cancelling the navigation keeps you put");
  edit("ha.require_sunset", false);
  confirmAnswer = false; confirmed = [];
  go("#"); await settle();
  check("cancel asks once", confirmed.length === 1 && /Home Assistant/.test(confirmed[0]));
  check("cancel stays on the section", !$("#sec-ha").hidden && $("#home").hidden);
  check("cancel keeps the edit", field("ha.require_sunset").checked === false);
  check("cancel restores the hash", w.location.hash === "#ha");
  check("cancel does not re-ask", confirmed.length === 1);

  console.log("save sends only the visible section");
  posted = [];
  await w.__save();
  check("one settings POST", posted.filter((p) => p.url === "/api/settings").length === 1);
  const body = posted.find((p) => p.url === "/api/settings").body;
  check("posted the HA field", body["ha.require_sunset"] === false);
  check("posted nothing else", Object.keys(body).length === 1);

  console.log("a change on another section never rides along");
  await settle();
  go("#display"); await settle();
  edit("display.brightness_normal", "80");
  confirmAnswer = true;
  go("#network"); await settle();          // discards the brightness edit
  edit("network.ipv4_method", "manual");
  edit("network.ipv4_address", "192.168.2.50");
  edit("network.ipv4_gateway", "192.168.2.1");
  posted = [];
  await w.__save();
  const b2 = posted.find((p) => p.url === "/api/settings").body;
  check("only network paths posted",
        Object.keys(b2).every((k) => k.startsWith("network.")));
  check("discarded brightness not posted", !("display.brightness_normal" in b2));

  console.log("invalid static IP is caught before the request");
  await settle();
  go("#network"); await settle();
  edit("network.ipv4_method", "manual");
  edit("network.ipv4_address", "");
  edit("network.ipv4_gateway", "");
  posted = [];
  await w.__save();
  check("nothing was POSTed to /api/settings",
        !posted.some((p) => p.url === "/api/settings"));
  check("error shown inline", !$("#net-ipv4-error").hidden &&
        /IP address/.test($("#net-ipv4-error").textContent));
  check("dismiss button offered", !$("#net-ipv4-dismiss").hidden);

  console.log("plex sign-in can be abandoned");
  go("#plex"); await settle();
  posted = [];
  $("#plex-signin").click(); await settle();
  check("sign-in shows the code panel", !$("#plex-link").hidden);
  check("code is displayed", $("#plex-code").textContent === "WXYZ");

  $("#plex-link-cancel").click(); await settle();
  check("cancel hides the panel", $("#plex-link").hidden);
  check("cancel tells the device",
        posted.some((p) => p.url === "/api/plex/auth/cancel"));
  check("cancel says so", /cancelled/i.test($("#plex-picker-msg").textContent));

  // The reported bug: start a sign-in, then just navigate away.
  posted = [];
  $("#plex-signin").click(); await settle();
  check("sign-in restarted", !$("#plex-link").hidden);
  go("#display"); await settle();
  check("leaving the section cancels the sign-in",
        posted.some((p) => p.url === "/api/plex/auth/cancel"));
  check("and the panel is gone", $("#plex-link").hidden);

  // Cancelling on the way out must not fire for unrelated navigation.
  posted = [];
  go("#device"); await settle();
  check("no stray cancel when nothing is linking",
        !posted.some((p) => p.url === "/api/plex/auth/cancel"));

  // The picker's leftovers must not greet you on the next visit.
  go("#plex"); await settle();
  $("#plex-signin").click(); await settle();
  $("#plex-link-cancel").click(); await settle();
  check("cancel message is shown while still on the section",
        !$("#plex-picker-msg").hidden);
  go("#"); await settle();
  go("#plex"); await settle();
  check("cancel message is gone on return", $("#plex-picker-msg").hidden);
  check("message text cleared too", $("#plex-picker-msg").textContent === "");
  check("code panel still hidden", $("#plex-link").hidden);

  // A stale server list is the same staleness, one element over.
  renderCandidatesForTest();
  check("candidates listed", !$("#plex-candidates").hidden);
  go("#"); await settle();
  go("#plex"); await settle();
  check("stale server list cleared on return", $("#plex-candidates").hidden &&
        $("#plex-candidates").children.length === 0);

  // And a typed-but-unsaved password should not survive either.
  go("#device"); await settle();
  $("#pw-new").value = "hunter2";
  go("#"); await settle();
  go("#device"); await settle();
  check("unsaved password not left in the box", $("#pw-new").value === "");

  console.log("theme");
  go("#device"); await settle();
  check("theme applied from the loaded settings",
        w.document.documentElement.dataset.theme === "auto");
  edit("web.theme", "dark");
  check("previewed the moment it is picked",
        w.document.documentElement.dataset.theme === "dark");
  check("but still needs a save", !$("#save").disabled);

  confirmAnswer = true;
  go("#"); await settle();                       // discard the theme change
  check("discarding puts the old theme back",
        w.document.documentElement.dataset.theme === "auto");
  check("and the control agrees", field("web.theme").value === "auto");

  go("#device"); await settle();
  edit("web.theme", "light");
  posted = [];
  await w.__save();
  const themeBody = posted.find((p) => p.url === "/api/settings").body;
  check("saving posts the theme", themeBody["web.theme"] === "light");
  check("saving posts nothing else", Object.keys(themeBody).length === 1);

  console.log("factory reset asks before it wipes anything");
  go("#device"); await settle();
  posted = []; confirmed = []; confirmAnswer = false;
  $("#factory-reset").click(); await settle();
  check("cancelling asks", confirmed.length === 1);
  check("cancelling posts nothing", !posted.length);
  check("the question names the WiFi loss", /WiFi/.test(confirmed[0]));
  check("the question says it cannot be undone",
        /cannot be undone/i.test(confirmed[0]));

  posted = []; confirmed = []; confirmAnswer = true;
  $("#factory-reset").click(); await settle();
  const reset = posted.find((p) => p.url === "/api/factory-reset");
  check("confirming posts the reset", !!reset);
  check("reset is explicitly confirmed", reset.body.confirm === true);
  check("full reset does not keep wifi", reset.body.keep_wifi === false);
  check("page explains what happens next", !$("#restart-note").hidden &&
        /NowPlaying-Setup/.test($("#restart-note").textContent));

  posted = []; confirmed = []; confirmAnswer = true;
  $("#reset-keep-wifi").click(); await settle();
  const soft = posted.find((p) => p.url === "/api/factory-reset");
  check("keep-wifi reset asks too", confirmed.length === 1);
  check("keep-wifi question does not threaten the network",
        !/cannot be undone/i.test(confirmed[0]));
  check("keep-wifi posts the flag", soft.body.keep_wifi === true);
  check("keep-wifi promises the page back",
        /nowplaying\.local/.test($("#restart-note").textContent));

  check("keep-wifi link does not navigate away", w.location.hash === "#device");

  console.log(fails ? `\n${fails} FAILED` : "\nall passed");
  process.exit(fails ? 1 : 0);
})();
