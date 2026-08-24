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
const ROOT = __dirname + "/../marquee/web/";
const SRC = ROOT + "static/";
const TPL = ROOT + "templates/";

const CFG = {
  device: { name: "Marquee" }, web: { password: null, theme: "auto" },
  plex: { url: "http://x", server_name: "S", verify_ssl: false, poll_seconds: 5,
          filter: { users: [], ignore_users: [], players: [], ignore_players: [],
                    media_types: [], hide_paused: false } },
  display: { cycle_seconds: 10, brightness_normal: 60, brightness_dim: 20,
             schedule_start: "00:00", schedule_stop: "00:00",
             dim_start: "00:00", dim_stop: "00:00", accent: "#e5a00d",
             idle_mode: "clock", clock_24h: false, poster_side: "left",
             scroll_speed: "normal", show_user: true },
  ha: { enabled: false, require_sunset: true, url: "", token: "", tv_entity: "" },
  network: { ipv4_method: "auto", ipv4_address: "", ipv4_prefix: 24,
             ipv4_gateway: "", ipv4_dns: "" },
  log_level: "INFO",
};
const STATUS = { sessions: [], plex_offline: false,
                 network: { status: "online", ip: "192.168.2.129",
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
  url: "http://marquee.local/", runScripts: "outside-only",
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
       "window.__save = save;\nwindow.__renderCandidates = renderCandidates;\n" +
       "window.__loadStatus = loadStatus;\nwindow.__loadSettings = loadSettings;");

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
        /Marquee-Setup/.test($("#restart-note").textContent));

  posted = []; confirmed = []; confirmAnswer = true;
  $("#reset-keep-wifi").click(); await settle();
  const soft = posted.find((p) => p.url === "/api/factory-reset");
  check("keep-wifi reset asks too", confirmed.length === 1);
  check("keep-wifi question does not threaten the network",
        !/cannot be undone/i.test(confirmed[0]));
  check("keep-wifi posts the flag", soft.body.keep_wifi === true);
  check("keep-wifi promises the page back",
        /marquee\.local/.test($("#restart-note").textContent));

  check("keep-wifi link does not navigate away", w.location.hash === "#device");


  console.log("what to show");
  go("#filters"); await settle();
  check("filters section opens", !$("#sec-filters").hidden);
  check("nothing filtered by default", $("#save").disabled);

  // Media types are several checkboxes writing through one hidden field.
  const movies = w.document.querySelector('[data-group="plex.filter.media_types"][value="movie"]');
  movies.checked = true;
  movies.dispatchEvent(new w.Event("change", { bubbles: true }));
  await settle();
  check("ticking a media type marks the section dirty", !$("#save").disabled);
  check("the group writes through to the hidden field",
        field("plex.filter.media_types").value === "movie");

  posted = [];
  await w.__save();
  const fbody = posted.find((p) => p.url === "/api/settings").body;
  check("media types posted as one path", fbody["plex.filter.media_types"] === "movie");
  check("posted nothing else", Object.keys(fbody).length === 1);

  console.log("filter rules revert with their section");
  await settle();
  edit("plex.filter.ignore_users", "Guest");
  const music = w.document.querySelector('[data-group="plex.filter.media_types"][value="track"]');
  music.checked = true;
  music.dispatchEvent(new w.Event("change", { bubbles: true }));
  await settle();
  confirmAnswer = true; confirmed = [];
  go("#"); await settle();
  check("leaving a dirty filter section asks", confirmed.length === 1 &&
        /What to show/.test(confirmed[0]));
  check("the text rule is put back", field("plex.filter.ignore_users").value === "");
  check("and the checkbox group is put back too", music.checked === false);

  console.log("names from the running sessions can be clicked in");
  STATUS.sessions = [{ title: "Ted Lasso", user: "James", state: "playing",
                       player: "Living Room TV", type: "episode" }];
  await w.__loadStatus(); await settle();
  go("#filters"); await settle();
  const chips = [...w.document.querySelectorAll("#filter-seen-list button")]
    .map((b) => b.textContent);
  check("both the person and the player are offered",
        chips.includes("James") && chips.includes("Living Room TV"));
  [...w.document.querySelectorAll("#filter-seen-list button")]
    .find((b) => b.textContent === "James").click();
  await settle();
  check("clicking a person fills the people rule",
        field("plex.filter.users").value === "James");
  check("a player goes to the player rule instead",
        field("plex.filter.players").value === "");
  check("clicking a name enables save", !$("#save").disabled);
  // Clicking the same name twice must not duplicate it.
  [...w.document.querySelectorAll("#filter-seen-list button")]
    .find((b) => b.textContent === "James").click();
  await settle();
  check("the same name is not added twice",
        field("plex.filter.users").value === "James");

  confirmAnswer = true; confirmed = [];
  go("#"); await settle();

  console.log("an unreachable server is called out");
  STATUS.sessions = [];
  STATUS.plex_offline = true;
  await w.__loadStatus(); await settle();
  check("the now-playing card stays up", !$("#now-playing").hidden);
  check("and says the server cannot be reached", !$("#np-offline").hidden);
  STATUS.plex_offline = false;
  await w.__loadStatus(); await settle();
  check("the notice clears when it comes back", $("#np-offline").hidden);
  check("and the empty card goes away", $("#now-playing").hidden);


  console.log("display personalisation");
  go("#display"); await settle();
  check("the accent loads as a colour", field("display.accent").value === "#e5a00d");
  check("idle mode loads", field("display.idle_mode").value === "clock");
  check("poster side loads", field("display.poster_side").value === "left");
  check("scroll speed loads", field("display.scroll_speed").value === "normal");
  check("the user line starts shown", field("display.show_user").checked === true);
  check("dimmed brightness moved to Display",
        field("display.brightness_dim").closest("section.page").id === "sec-display");
  check("and is no longer duplicated in Home Assistant",
        w.document.querySelectorAll('[data-path="display.brightness_dim"]').length === 1);

  edit("display.accent", "#3aa0ff");
  edit("display.idle_mode", "poster");
  edit("display.poster_side", "right");
  edit("display.scroll_speed", "slow");
  edit("display.show_user", false);
  edit("display.clock_24h", true);
  edit("display.dim_start", "22:00");
  edit("display.dim_stop", "07:00");
  posted = [];
  await w.__save();
  const dbody = posted.find((p) => p.url === "/api/settings").body;
  check("accent posted", dbody["display.accent"] === "#3aa0ff");
  check("idle mode posted", dbody["display.idle_mode"] === "poster");
  check("poster side posted", dbody["display.poster_side"] === "right");
  check("scroll speed posted", dbody["display.scroll_speed"] === "slow");
  check("hiding the user line posted", dbody["display.show_user"] === false);
  check("24-hour clock posted", dbody["display.clock_24h"] === true);
  check("dim window posted",
        dbody["display.dim_start"] === "22:00" && dbody["display.dim_stop"] === "07:00");
  check("untouched display fields stayed out",
        dbody["display.cycle_seconds"] === undefined);

  console.log("reset to Plex amber");
  await settle();
  check("no reset offered while already amber", $("#accent-default").hidden);
  edit("display.accent", "#3aa0ff");
  check("reset appears once the colour differs", !$("#accent-default").hidden);
  $("#accent-default").click(); await settle();
  check("the button restores plex amber",
        field("display.accent").value === "#e5a00d");
  check("and hides itself again", $("#accent-default").hidden);
  // Back at the stored value, so there is nothing left to save.
  check("resetting to the saved colour leaves nothing pending", $("#save").disabled);

  // From a *saved* non-amber colour the reset is a real edit and must save.
  CFG.display.accent = "#3aa0ff";
  await w.__loadSettings(); await settle();
  check("reset is offered for a saved non-amber colour", !$("#accent-default").hidden);
  $("#accent-default").click(); await settle();
  check("resetting marks the section dirty", !$("#save").disabled);
  posted = [];
  await w.__save();
  const abody = posted.find((p) => p.url === "/api/settings").body;
  check("the reset posts plex amber", abody["display.accent"] === "#e5a00d");
  CFG.display.accent = "#e5a00d";
  await w.__loadSettings(); await settle();

  console.log("display edits revert with the section");
  await settle();
  edit("display.accent", "#ff0000");
  confirmAnswer = true; confirmed = [];
  go("#"); await settle();
  check("leaving asks", confirmed.length === 1 && /Display/.test(confirmed[0]));
  check("the accent is put back", field("display.accent").value === "#e5a00d");

  console.log(fails ? `\n${fails} FAILED` : "\nall passed");
  process.exit(fails ? 1 : 0);
})();
