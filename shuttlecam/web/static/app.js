"use strict";

// Court points to mark, in order. "Near" is the half the camera is behind; left and
// right are as the camera sees them.
const GUIDE = [
  ["near_base/left_doubles", "Near baseline, left corner", "Where the back line on your side meets the outer left sideline."],
  ["near_base/right_doubles", "Near baseline, right corner", "Where the back line on your side meets the outer right sideline."],
  ["near_short/left_doubles", "Near service line, left end", "Where the front service line on your side meets the outer left sideline."],
  ["near_short/centre", "Near T", "Where the centre line meets the front service line on your side."],
  ["near_short/right_doubles", "Near service line, right end", "Where the front service line on your side meets the outer right sideline."],
  ["post_left_base", "Left net post, bottom", "Where the left post stands on the floor."],
  ["post_left_top", "Left net post, top", "The very top of the left post."],
  ["post_right_top", "Right net post, top", "The very top of the right post."],
  ["post_right_base", "Right net post, bottom", "Where the right post stands on the floor."],
  ["far_short/left_doubles", "Far service line, left end", "The far side's front service line, at the outer left sideline."],
  ["far_short/centre", "Far T", "Where the centre line meets the far side's front service line."],
  ["far_short/right_doubles", "Far service line, right end", "The far side's front service line, at the outer right sideline."],
  ["far_base/left_doubles", "Far baseline, left corner", "Where the far back line meets the outer left sideline."],
  ["far_base/right_doubles", "Far baseline, right corner", "Where the far back line meets the outer right sideline."],
];
const TITLE = Object.fromEntries(GUIDE.map(([n, t]) => [n, t]));
const MIN_POINTS = 6;
// Court fits looser than this (px RMS) usually mean a bending lens: a 150° fisheye
// fitted to 12 px in testing, a lens-corrected phone camera to about 1-3 px.
const LOOSE_FIT_PX = 6;

const $ = (id) => document.getElementById(id);
const S = { state: null, view: null, timer: null, cal: null, resultId: null, tab: "recent" };

// -- helpers ----------------------------------------------------------------------------

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (init.method !== "GET") init.headers["X-Shuttlecam"] = "1";
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (_) { /* not JSON */ }
  if (r.status === 401 && path !== "/api/login") {
    show("login");
    throw new Error("log in first");
  }
  if (!r.ok) throw new Error((data && data.error) || `error ${r.status}`);
  return data;
}

function show(name) {
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== `view-${name}`;
  $("logout").hidden = name === "login";
  if (name !== "calibrate") $("loupe").hidden = true;
  if (S.view !== name) window.scrollTo(0, 0);
  S.view = name;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function when(sec) {
  const d = new Date(sec * 1000);
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? t : `${d.toLocaleDateString()} ${t}`;
}

function setError(id, msg) {
  $(id).textContent = msg || "";
  $(id).hidden = !msg;
}

// The number to lead with for a measured clip.
function headline(result) {
  if (!result || result.best === null || result.best === undefined) return null;
  const s = result.shots[result.best];
  if (s.launch_reliable) return { kind: "launch", kmh: s.launch_kmh, text: `${Math.round(s.launch_kmh)} km/h`, s };
  if (s.launch_known) return { kind: "rough", kmh: s.launch_kmh, text: `~${Math.round(s.launch_kmh)} km/h`, s };
  return { kind: "first", kmh: s.first_tracked_kmh, text: `≥${Math.round(s.first_tracked_kmh)} km/h`, s };
}

function status(c) {
  const clips = (S.state && S.state.clips) || [];
  if (c.status === "needs_calibration") return "Mark the court first";
  if (c.status === "queued") {
    const line = clips.filter((x) => x.status === "queued").sort((a, b) => a.created - b.created);
    const ahead = line.findIndex((x) => x.id === c.id) + (clips.some((x) => x.status === "running") ? 1 : 0);
    return ahead > 0 ? `Waiting (${ahead} ahead)` : "Starting…";
  }
  if (c.status === "running") return `Measuring… ${Math.max(0, Math.round(Date.now() / 1000 - c.started))} s`;
  if (c.status === "failed") return "Failed";
  return null;
}

// -- home -------------------------------------------------------------------------------

async function refresh() {
  clearTimeout(S.timer);
  try {
    S.state = await api("/api/state");
  } catch (_) {
    return; // logged out, or the server is unreachable: the login view or a retry
  }
  if (!S.view || S.view === "login") show("home");
  renderHome();
  if (S.view === "result") renderResult();
  const busy = S.state.clips.some((c) => c.status === "queued" || c.status === "running");
  S.timer = setTimeout(refresh, busy ? 2500 : 20000);
}

function renderHome() {
  const st = S.state;
  $("setup-choice").hidden = !st.active_setup;
  const body = $("setup-body");
  body.replaceChildren();
  const active = st.setups.find((s) => s.id === st.active_setup);
  const waiting = st.clips.find((c) => c.status === "needs_calibration");
  if (waiting) {
    const setup = st.setups.find((s) => s.id === waiting.setup);
    body.append(el("p", "", "Your last upload is from a new camera spot. Mark the court on it once, then it's measured."));
    const b = el("button", "primary wide", "Mark the court");
    b.onclick = () => startCalibration(setup);
    body.append(b);
  } else if (active) {
    const row = el("div", "setup");
    const img = el("img");
    img.src = `/api/setups/${active.id}/overlay.jpg?t=${active.calibrated}`;
    img.alt = "The court as calibrated";
    const text = el("div");
    text.append(el("div", "", `Set up ${when(active.calibrated)}`));
    text.append(el("div", "muted small", `${Object.keys(active.points).length} points, fit to ${active.rms_px} px`));
    const again = el("button", "link", "Mark again");
    again.onclick = () => startCalibration(active);
    text.append(again);
    row.append(img, text);
    body.append(row);
  } else {
    body.append(el("p", "muted", "No camera spot yet. Your first upload sets one up: you'll mark a few court points on it."));
    const link = el("button", "link", "Where to put the phone ↓");
    link.onclick = () => {
      $("guide").open = true;
      $("guide").scrollIntoView({ behavior: "smooth" });
    };
    body.append(link);
  }
  // Open the set-up guide for newcomers, until they open or close it themselves.
  if (!S.guideTouched) $("guide").open = !st.active_setup;
  $("footer").textContent = `Shuttle drag: terminal velocity ${st.terminal_velocity.toFixed(2)} m/s`;
  renderClips();
}

function renderClips() {
  const list = $("clips");
  list.replaceChildren();
  const clips = S.state.clips;
  if (!clips.length) {
    list.append(el("li", "empty", "No clips yet."));
    return;
  }
  if (S.tab === "best") {
    const best = new Map();
    for (const c of clips) {
      const h = c.status === "done" ? headline(c.result) : null;
      if (!h) continue;
      const who = c.player || "Unnamed";
      const score = [h.kind === "launch" ? 1 : 0, h.kmh];
      const prev = best.get(who);
      if (!prev || score[0] > prev.score[0] || (score[0] === prev.score[0] && score[1] > prev.score[1])) {
        best.set(who, { c, h, score });
      }
    }
    const ranked = [...best.entries()].sort((a, b) => b[1].score[0] - a[1].score[0] || b[1].score[1] - a[1].score[1]);
    if (!ranked.length) list.append(el("li", "empty", "No measured smashes yet."));
    ranked.forEach(([who, { c, h }], i) => list.append(row(c, who, h.text, h.kind === "launch" ? "" : "rough", i + 1)));
    return;
  }
  for (const c of clips) {
    const pending = status(c);
    const h = c.status === "done" ? headline(c.result) : null;
    const text = pending || (h ? h.text : "No smash found");
    const cls = pending || !h ? "pending" : h.kind === "launch" ? "" : "rough";
    list.append(row(c, c.player || "Unnamed", text, cls));
  }
}

function row(c, who, value, cls, rank) {
  const li = el("li");
  const b = el("button", "row");
  const left = el("span", "who", who);
  left.append(el("span", "when", when(c.created)));
  if (rank) b.append(el("span", "rank", `${rank}.`));
  b.append(left, el("span", `val ${cls}`, value));
  b.onclick = () => {
    if (c.status === "needs_calibration") startCalibration(S.state.setups.find((s) => s.id === c.setup));
    else openResult(c.id);
  };
  li.append(b);
  return li;
}

// -- upload -----------------------------------------------------------------------------

function upload() {
  const file = $("video").files[0];
  if (!file) return;
  try { localStorage.setItem("player", $("player").value); } catch (_) { /* private mode */ }
  const form = new FormData();
  form.append("video", file);
  form.append("player", $("player").value);
  form.append("fps", $("fps").value);
  const choice = document.querySelector("input[name=setup]:checked");
  form.append("setup", S.state && S.state.active_setup && choice && choice.value === "current" ? "current" : "new");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/clips");
  xhr.setRequestHeader("X-Shuttlecam", "1");
  $("upload").disabled = true;
  $("progress").hidden = false;
  $("progress-bar").style.width = "0%";
  setError("upload-error", "");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) $("progress-bar").style.width = `${(100 * e.loaded) / e.total}%`;
  };
  const done = () => {
    $("progress").hidden = true;
    $("upload").disabled = !$("video").files[0];
  };
  xhr.onerror = () => {
    done();
    setError("upload-error", "Upload failed: check the connection and try again.");
  };
  xhr.onload = async () => {
    done();
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
    if (xhr.status === 401) return show("login");
    if (xhr.status !== 200) return setError("upload-error", data.error || `Upload failed (error ${xhr.status}).`);
    $("video").value = "";
    $("video-label").textContent = "Choose or record a clip";
    $("upload").disabled = true;
    await refresh();
    if (data.clip.status === "needs_calibration") startCalibration(data.setup);
    else openResult(data.clip.id);
  };
  xhr.send(form);
}

// -- marking the court ------------------------------------------------------------------

function startCalibration(setup) {
  if (!setup) return;
  const points = {};
  for (const [k, v] of Object.entries(setup.points || {})) points[k] = v.slice();
  S.cal = { setup, step: 0, points, drag: null };
  setError("cal-error", "");
  show("calibrate");
  const img = $("frame");
  img.onload = () => {
    $("marks").setAttribute("viewBox", `0 0 ${img.naturalWidth} ${img.naturalHeight}`);
    renderStep();
  };
  img.src = `/api/setups/${setup.id}/frame.jpg`;
  renderStep();
}

const marked = () => Object.keys(S.cal.points).length;
const current = () => GUIDE[S.cal.step][0];

function renderStep() {
  const c = S.cal;
  const [, title, hint] = GUIDE[c.step];
  $("cal-step").textContent = `Point ${c.step + 1} of ${GUIDE.length} · ${marked()} marked`;
  $("cal-name").textContent = title;
  $("cal-hint").textContent = hint;
  $("cal-back").disabled = c.step === 0;
  $("cal-next").disabled = !c.points[current()];
  $("cal-next").textContent = c.step === GUIDE.length - 1 ? "Done" : "Next";
  const n = marked();
  $("cal-done").disabled = n < MIN_POINTS;
  $("cal-done").textContent = n < MIN_POINTS ? `Mark ${MIN_POINTS - n} more` : `Calibrate with ${n} points`;
  drawMap();
  drawMarks();
}

function step(delta) {
  const c = S.cal;
  $("loupe").hidden = true;
  if (c.step + delta >= GUIDE.length) {
    if (marked() >= MIN_POINTS) calibrate();
    return;
  }
  c.step = Math.max(0, c.step + delta);
  renderStep();
}

// Map a pointer position to image pixels.
function toImage(e) {
  const img = $("frame");
  const r = img.getBoundingClientRect();
  const k = img.naturalWidth / r.width;
  return [(e.clientX - r.left) * k, (e.clientY - r.top) * k];
}

function clampToImage(p) {
  const img = $("frame");
  return [Math.min(Math.max(p[0], 0), img.naturalWidth - 1), Math.min(Math.max(p[1], 0), img.naturalHeight - 1)];
}

function setupStage() {
  const stage = $("stage");
  stage.addEventListener("pointerdown", (e) => {
    const img = $("frame");
    if (!S.cal || !img.naturalWidth) return;
    e.preventDefault();
    stage.setPointerCapture(e.pointerId);
    const p = clampToImage(toImage(e));
    const k = img.naturalWidth / img.getBoundingClientRect().width;
    const prev = S.cal.points[current()];
    // A tap far from the point moves it there; a touch near it starts a fine drag.
    if (!prev || Math.hypot(p[0] - prev[0], p[1] - prev[1]) / k > 36) S.cal.points[current()] = p;
    S.cal.drag = { id: e.pointerId, x: e.clientX, y: e.clientY, start: S.cal.points[current()].slice() };
    showLoupe(e.clientX);
    renderStep();
  });
  stage.addEventListener("pointermove", (e) => {
    const d = S.cal && S.cal.drag;
    if (!d || d.id !== e.pointerId) return;
    const img = $("frame");
    const k = (img.naturalWidth / img.getBoundingClientRect().width) * 0.3; // fine: finger moves 3x the point
    S.cal.points[current()] = clampToImage([d.start[0] + (e.clientX - d.x) * k, d.start[1] + (e.clientY - d.y) * k]);
    showLoupe(e.clientX);
    drawMarks();
  });
  const end = (e) => {
    if (S.cal && S.cal.drag && S.cal.drag.id === e.pointerId) {
      S.cal.drag = null;
      renderStep();
    }
  };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", end);
}

function showLoupe(fingerX) {
  const canvas = $("loupe");
  const img = $("frame");
  const p = S.cal.points[current()];
  if (!p) return;
  const dpr = window.devicePixelRatio || 1;
  const size = Math.round(180 * dpr);
  if (canvas.width !== size) canvas.width = canvas.height = size;
  canvas.hidden = false;
  canvas.className = fingerX < window.innerWidth / 2 ? "right" : "left";
  const ctx = canvas.getContext("2d");
  const span = 44 * Math.max(1, img.naturalWidth / 1920); // image pixels across the loupe
  const s = size / span;
  const sx = p[0] - span / 2;
  const sy = p[1] - span / 2;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, size, size);
  ctx.imageSmoothingEnabled = false;
  const x0 = Math.max(0, sx), y0 = Math.max(0, sy);
  const x1 = Math.min(img.naturalWidth, sx + span), y1 = Math.min(img.naturalHeight, sy + span);
  if (x1 > x0 && y1 > y0) ctx.drawImage(img, x0, y0, x1 - x0, y1 - y0, (x0 - sx) * s, (y0 - sy) * s, (x1 - x0) * s, (y1 - y0) * s);
  const c = size / 2, gap = 6 * dpr, arm = 40 * dpr;
  ctx.strokeStyle = "#facc15";
  ctx.lineWidth = 2 * dpr;
  ctx.beginPath();
  ctx.moveTo(c - arm, c); ctx.lineTo(c - gap, c);
  ctx.moveTo(c + gap, c); ctx.lineTo(c + arm, c);
  ctx.moveTo(c, c - arm); ctx.lineTo(c, c - gap);
  ctx.moveTo(c, c + gap); ctx.lineTo(c, c + arm);
  ctx.stroke();
}

function drawMarks() {
  const img = $("frame");
  if (!S.cal || !img.naturalWidth) return;
  const r = img.naturalWidth / 150;
  let svg = "";
  for (const [name] of GUIDE) {
    const p = S.cal.points[name];
    if (!p) continue;
    const now = name === current();
    const [x, y] = p;
    svg += `<circle cx="${x}" cy="${y}" r="${now ? 1.6 * r : r}" fill="none" stroke="${now ? "#facc15" : "#22c55e"}" stroke-width="${r / 3}"/>`;
    if (now) {
      svg += `<path d="M${x - 3 * r} ${y}h${1.8 * r}M${x + 1.2 * r} ${y}h${1.8 * r}M${x} ${y - 3 * r}v${1.8 * r}M${x} ${y + 1.2 * r}v${1.8 * r}" stroke="#facc15" stroke-width="${r / 4}"/>`;
    }
  }
  $("marks").innerHTML = svg;
}

// The little court diagram: seen from above, camera at the bottom.
function drawMap() {
  const st = S.state;
  if (!st) return;
  const X = (x) => 40 + x * 10.4;
  const Y = (y) => 78 - y * 10.4;
  let svg = `<rect x="0" y="0" width="80" height="162" rx="6" fill="#15803d"/>`;
  for (const [a, b] of st.segments) {
    if (a[2] > 0 || b[2] > 0) continue; // posts and net tape: drawn below
    svg += `<line x1="${X(a[0])}" y1="${Y(a[1])}" x2="${X(b[0])}" y2="${Y(b[1])}" stroke="#fff" stroke-width=".8"/>`;
  }
  svg += `<line x1="${X(-3.3)}" y1="${Y(0)}" x2="${X(3.3)}" y2="${Y(0)}" stroke="#e5e7eb" stroke-width="1.6" stroke-dasharray="2 1"/>`;
  svg += `<rect x="35" y="152" width="10" height="7" rx="1.5" fill="#111827"/><circle cx="40" cy="155.5" r="2" fill="#93c5fd"/>`;
  const cur = current();
  for (const [name] of GUIDE) {
    const w = st.landmarks[name];
    const done = S.cal.points[name];
    const now = name === cur;
    if (now) continue;
    svg += `<circle cx="${X(w[0])}" cy="${Y(w[1])}" r="2.2" fill="${done ? "#22c55e" : "#9ca3af"}" stroke="#14532d" stroke-width=".5"/>`;
  }
  const w = st.landmarks[cur];
  svg += `<circle cx="${X(w[0])}" cy="${Y(w[1])}" r="5" fill="none" stroke="#facc15" stroke-width="1.8"/>`;
  svg += `<circle cx="${X(w[0])}" cy="${Y(w[1])}" r="2.2" fill="#facc15"/>`;
  $("court-map").setAttribute("viewBox", "0 0 80 162");
  $("court-map").innerHTML = svg;
}

async function calibrate() {
  const btn = $("cal-done");
  btn.disabled = true;
  btn.textContent = "Calibrating…";
  setError("cal-error", "");
  try {
    const setup = await api(`/api/setups/${S.cal.setup.id}/calibrate`, { method: "POST", json: { points: S.cal.points } });
    S.cal.setup = setup;
    showCheck(setup);
  } catch (e) {
    setError("cal-error", e.message);
    renderStep();
  }
}

function showCheck(setup) {
  show("check");
  $("overlay").src = `/api/setups/${setup.id}/overlay.jpg?t=${setup.calibrated}`;
  const [x, y, z] = setup.camera_position;
  const where = y < -6.7
    ? `about ${(-y - 6.7).toFixed(1)} m behind the near baseline and ${z.toFixed(1)} m up`
    : `at ${x.toFixed(1)}, ${y.toFixed(1)} m and ${z.toFixed(1)} m up`;
  $("check-text").textContent = `The points fit to ${setup.rms_px} px. The camera is ${where}.`;
  const list = $("check-worst");
  list.replaceChildren();
  if (setup.rms_px > LOOSE_FIT_PX) {
    list.append(el("li", "warn",
      "That's a loose fit. A fisheye lens (or a point marked in the wrong place) does this, " +
      "and speeds from this spot could be 10% or more off. Check the points, or film without the fisheye."));
  }
  Object.entries(setup.errors_px)
    .filter(([, e]) => e > Math.max(5, 3 * setup.rms_px))
    .sort((a, b) => b[1] - a[1])
    .forEach(([name, e]) => list.append(el("li", "", `Check “${TITLE[name] || name}”: ${e} px off`)));
}

// -- results ----------------------------------------------------------------------------

function openResult(id) {
  S.resultId = id;
  show("result");
  renderResult();
}

function addDetail(dl, label, value) {
  dl.append(el("dt", "", label), el("dd", "", value));
}

async function renderResult() {
  let c;
  try {
    c = await api(`/api/clips/${S.resultId}`);
  } catch (_) {
    return;
  }
  if (S.view !== "result" || c.id !== S.resultId) return;
  $("result-player").textContent = `${c.player || "Unnamed"} · ${when(c.created)}`;
  const speed = $("result-speed");
  const sub = $("result-sub");
  const dl = $("result-details");
  const plot = $("result-plot");
  dl.replaceChildren();
  $("result-note").textContent = "";
  plot.hidden = true;
  const pending = status(c);
  if (pending) {
    speed.className = "speed none";
    speed.textContent = pending;
    sub.textContent = c.status === "failed" ? c.error || "" : "This page updates by itself.";
    return;
  }
  const h = headline(c.result);
  if (!h) {
    speed.className = "speed none";
    speed.textContent = "No smash found";
    sub.textContent = "Check the clip shows the shuttle coming in and the whole smash, and that the camera spot is right.";
    return;
  }
  const s = h.s;
  speed.className = `speed ${h.kind === "launch" ? "" : "rough"}`;
  speed.textContent = `${h.kind === "first" ? "≥" : h.kind === "rough" ? "~" : ""}${Math.round(h.kmh)}`;
  speed.append(el("small", "", "km/h"));
  const [lo, hi] = s.launch_range_kmh.map(Math.round);
  sub.textContent = h.kind === "launch"
    ? `off the racket · likely ${lo}–${hi} km/h`
    : h.kind === "rough"
      ? `rough: anywhere from ${lo} to ${hi} km/h`
      : "when first seen. The hit wasn't timed, so the launch speed is higher but unknown.";
  addDetail(dl, "First seen", `${Math.round(s.first_tracked_kmh)} km/h` + (s.ms_after_hit !== null ? `, ${s.ms_after_hit} ms after the hit` : ""));
  addDetail(dl, "Hit", `${s.hit_height_m.toFixed(1)} m above the floor`);
  if (s.lands_xy_m) {
    const y = s.lands_xy_m[1];
    addDetail(dl, "Lands", `${Math.abs(y).toFixed(1)} m from the net, ${y > 0 ? "far" : "near"} side`);
  }
  addDetail(dl, "Tracked", `frames ${s.frames[0]}–${s.frames[1]} (${s.points} points)`);
  const others = c.result.shots.filter((_, i) => i !== c.result.best);
  if (others.length) {
    addDetail(dl, "Other tracks", others.map((o) => `${Math.round(o.launch_known ? o.launch_kmh : o.first_tracked_kmh)} km/h`).join(", "));
  }
  const notes = [];
  if (s.little_perspective) notes.push("The camera saw this flight nearly head-on, so its distance (and speed) is less certain.");
  else if (!s.depth_well_determined) notes.push("The shuttle's distance from the camera is uncertain, so the speed is too.");
  if (c.result.real_fps) notes.push(`The file repeats frames: measured at its real ${c.result.real_fps} fps.`);
  $("result-note").textContent = notes.join(" ");
  if (c.has_plot) {
    plot.src = `/api/clips/${c.id}/plot.png?t=${c.finished}`;
    plot.hidden = false;
  }
}

// -- wiring -----------------------------------------------------------------------------

function wire() {
  try { $("player").value = localStorage.getItem("player") || ""; } catch (_) { /* private mode */ }
  $("login-form").onsubmit = async (e) => {
    e.preventDefault();
    setError("login-error", "");
    try {
      await api("/api/login", { method: "POST", json: { passcode: $("passcode").value } });
      $("passcode").value = "";
      show("home");
      refresh();
    } catch (err) {
      setError("login-error", err.message);
    }
  };
  $("logout").onclick = async () => {
    try { await api("/api/logout", { method: "POST" }); } catch (_) { /* already out */ }
    clearTimeout(S.timer);
    show("login");
  };
  $("video").onchange = () => {
    const f = $("video").files[0];
    $("video-label").textContent = f ? `${f.name} · ${(f.size / 1e6).toFixed(1)} MB` : "Choose or record a clip";
    $("upload").disabled = !f;
    setError("upload-error", "");
  };
  $("upload").onclick = upload;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.onclick = () => {
      S.tab = tab.dataset.tab;
      for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t === tab);
      renderClips();
    };
  }
  $("cal-back").onclick = () => step(-1);
  $("cal-next").onclick = () => step(1);
  $("cal-skip").onclick = () => {
    delete S.cal.points[current()];
    step(1);
  };
  $("cal-done").onclick = calibrate;
  $("cal-cancel").onclick = () => {
    S.cal = null;
    show("home");
    refresh();
  };
  $("check-redo").onclick = () => {
    show("calibrate");
    renderStep();
  };
  $("check-ok").onclick = async () => {
    try {
      await api(`/api/setups/${S.cal.setup.id}/accept`, { method: "POST" });
      S.cal = null;
      show("home");
      refresh();
    } catch (e) {
      $("check-text").textContent = e.message;
    }
  };
  $("result-back").onclick = () => {
    show("home");
    refresh();
  };
  $("result-delete").onclick = async () => {
    if (!confirm("Delete this clip and its result?")) return;
    try {
      await api(`/api/clips/${S.resultId}`, { method: "DELETE" });
      show("home");
      refresh();
    } catch (e) {
      $("result-note").textContent = e.message;
    }
  };
  setupStage();
  $("guide").querySelector("summary").addEventListener("click", () => { S.guideTouched = true; });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && S.view !== "login") refresh();
  });
}

wire();
refresh();
