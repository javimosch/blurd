/* blurd dashboard.
   Talks to /ui-api/*, which is behind the same basic-auth session as this page.
   That is deliberate: no API key is ever embedded in this JavaScript, so a
   browser session and a machine credential stay separate secrets. */

const $ = (id) => document.getElementById(id);

/* Cursor (keyset) pagination.
   OFFSET is O(offset) -- the server walks and discards every skipped row -- and
   it is also wrong while rows are being inserted, because the window shifts and
   the reader sees duplicates or gaps. Keyset seeks straight to the cursor, so
   page 1000 costs what page 1 costs. It only moves forward, so "prev" is a
   stack of the cursors already visited. */
function pager(limit) {
  return { limit, cursor: null, stack: [], total: 0, capped: false, shown: 0 };
}
const state = pager(24);
const jstate = pager(25);

function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)blurd_csrf=([^;]+)/);
  return m ? m[1] : "";
}

async function api(path, opts = {}) {
  // Every mutation carries the double-submit token; the cookie is
  // SameSite=Strict so a cross-site page never has it to echo.
  const method = (opts.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    opts.headers = Object.assign({ "X-Blurd-CSRF": csrf() }, opts.headers || {});
  }
  const res = await fetch("/ui-api" + path, opts);
  const body = await res.json();
  if (!res.ok || body.ok === false) {
    const err = new Error(body?.error?.message || ("HTTP " + res.status));
    err.detail = body?.error;
    throw err;
  }
  return body.data;
}

function csv(v) {
  return v.split(",").map((s) => s.trim()).filter(Boolean);
}

function dayStart(v) { return v ? v + "T00:00:00Z" : null; }
function dayEnd(v) { return v ? v + "T23:59:59Z" : null; }

/* "Get this via API": renders the exact /v1 call that reproduces what the
   screen shows. Deliberately copy-paste only — the dashboard session can never
   be an API credential (AGENTS.md rule 9), so snippets carry a key placeholder
   rather than a real one. */
const API_KEY_VAR = "$BLURD_KEY";
function apiSheet(title, blocks) {
  $("api-title").textContent = title;
  $("api-code").textContent = blocks.join("\n\n");
  $("api-sheet").hidden = false;
}
function apiGet(path) {
  return `curl -H "Authorization: Bearer ${API_KEY_VAR}" "${location.origin}${path}"`;
}
const API_KEY_NOTE =
  `# The dashboard login is NOT an API credential — /v1 needs its own Bearer key.
# mint one:  blurd keys add <name>   (or dashboard → keys, if enabled)`;
$("api-copy").onclick = () =>
  navigator.clipboard?.writeText($("api-code").textContent);
$("api-close").onclick = () => { $("api-sheet").hidden = true; };

function filterQuery() {
  const q = new URLSearchParams();
  csv($("f-tag").value).forEach((t) => q.append("tag", t));
  csv($("f-meta").value).forEach((kv) => {
    const i = kv.indexOf("=");
    if (i > 0) q.append("meta." + kv.slice(0, i).trim(), kv.slice(i + 1).trim());
  });
  if ($("f-sha").value.trim()) q.set("sha", $("f-sha").value.trim());
  if ($("f-code").value.trim()) q.set("code", $("f-code").value.trim());
  if ($("f-review").checked) q.set("needs_review", "1");
  if ($("f-since").value) q.set("since", dayStart($("f-since").value));
  if ($("f-until").value) q.set("until", dayEnd($("f-until").value));
  const [sort, direction] = $("f-sort").value.split(":");
  q.set("sort", sort); q.set("direction", direction);
  state.limit = parseInt($("f-size").value, 10) || 24;
  q.set("limit", state.limit);
  if (state.cursor) q.set("cursor", state.cursor);
  return q;
}

function renderPager(p, ids) {
  const from = p.shown ? p.stack.length * p.limit + 1 : 0;
  const to = p.stack.length * p.limit + p.shown;
  const total = p.total.toLocaleString() + (p.capped ? "+" : "");
  $(ids.info).innerHTML =
    `${from.toLocaleString()}\u2013${to.toLocaleString()} of ` +
    (p.capped ? `<span class="capped" title="counting stops at 10,000 so a filtered
      million-row table is never fully scanned to render a number">${total}</span>`
              : total);
  $(ids.prev).disabled = p.stack.length === 0;
  $(ids.first).disabled = p.stack.length === 0;
  $(ids.next).disabled = !p.nextCursor;
}

function card(item) {
  const el = document.createElement("div");
  el.className = "card" + (item.needs_review ? " review" : "");
  const tags = item.tags.map((t) =>
    `<span class="pill tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("");
  el.innerHTML = `
    <img loading="lazy" src="/ui-api/thumbs/${item.source_sha}?profile=${item.profile_hash}" alt="">
    <div class="info">
      <div class="sha">${item.source_sha.slice(0, 24)}…</div>
      ${(item.codes || []).map((c) => `<div class="code">${esc(c)}</div>`).join("")}
      <div>${item.created_at} · ${item.width}×${item.height}</div>
      <div>
        <span class="pill face">${item.n_faces} face</span>
        <span class="pill plate">${item.n_plates} plate</span>
        ${item.needs_review ? '<span class="pill review">review</span>' : ""}
        ${item.expires_at && item.expires_at <= new Date().toISOString()
            ? '<span class="pill warn">expired</span>' : ""}
      </div>
      <div>${tags}</div>
    </div>`;
  el.onclick = () => openDetail(item.source_sha, item.profile_hash);
  el.querySelectorAll(".pill.tag").forEach((p) => {
    p.onclick = (e) => {
      e.stopPropagation();
      csvToggle("f-tag", p.dataset.tag);
      resetPager(state); load();
    };
  });
  return el;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function load() {
  const grid = $("grid");
  grid.innerHTML = "";
  try {
    const data = await api("/images?" + filterQuery().toString());
    // The server counts only on the first page -- paging deeper cannot change
    // the total, so carry it forward instead of paying for the count again.
    if (data.total !== null && data.total !== undefined) {
      state.total = data.total;
      state.capped = data.total_capped;
    }
    state.shown = data.items.length;
    state.nextCursor = data.next_cursor;
    data.items.forEach((i) => grid.appendChild(card(i)));
    // Empty has two causes: no match under active filters, or a fresh install
    // with nothing stored. The second is the onboarding moment — show the two
    // submit calls a human needs, not just "nothing here".
    if (data.items.length === 0) {
      const filtered = $("f-review").checked ||
        ["f-tag", "f-meta", "f-sha", "f-code", "f-since", "f-until"]
          .some((i) => $(i).value.trim());
      $("empty").innerHTML = (filtered || state.total > 0)
        ? "Nothing matches these filters."
        : `<div style="margin-bottom:14px">No images yet — submit the first one:</div>
           <pre style="text-align:left;max-width:720px;margin:0 auto 12px">${
             esc('# upload bytes\ncurl -X POST -H "Authorization: Bearer ' + API_KEY_VAR +
               '" --data-binary @photo.jpg \\\n  -H "Content-Type: image/jpeg" \\\n  "' +
               location.origin + '/v1/images?code=IMG_0001.jpg&tags=demo"')
           }</pre>
           <pre style="text-align:left;max-width:720px;margin:0 auto 12px">${
             esc('# or hand blurd a URL to fetch itself\ncurl -X POST -H "Authorization: Bearer ' +
               API_KEY_VAR + '" -H "Content-Type: application/json" \\\n  -d \'{"url":"https://example.com/photo.jpg","external_id":"IMG_0001.jpg","tags":["demo"]}\' \\\n  "' +
               location.origin + '/v1/images"')
           }</pre>
           <div>${esc(API_KEY_NOTE.replace(/\n/g, " · "))}</div>
           <div style="margin-top:10px">Poll the returned job — it settles in a
             second or two — then refresh this page.</div>`;
    }
    $("empty").hidden = data.items.length !== 0;
    renderPager(state, { info: "page-info", prev: "prev", next: "next", first: "first" });
    loadFacets();
  } catch (err) {
    grid.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

/* Clickable filter chips built from what is actually stored. Clicking a chip
   toggles the term in the matching input and re-runs the filter, so chips and
   the text boxes can never disagree about what is active. */
function csvHas(input, term) {
  return csv($(input).value).includes(term);
}
function csvToggle(input, term) {
  const items = csv($(input).value);
  const i = items.indexOf(term);
  if (i >= 0) items.splice(i, 1); else items.push(term);
  $(input).value = items.join(",");
}

function chip(label, active, onclick, title) {
  const b = document.createElement("button");
  b.className = "chip" + (active ? " active" : "");
  b.textContent = label;
  if (title) b.title = title;
  b.onclick = onclick;
  return b;
}

async function loadFacets() {
  try {
    const f = await api("/facets");
    const box = $("chips");
    box.innerHTML = "";
    const apply = () => { resetPager(state); load(); };
    (f.tags || []).forEach((t) => box.appendChild(chip(
      `${t.tag} (${t.n})`, csvHas("f-tag", t.tag),
      () => { csvToggle("f-tag", t.tag); apply(); },
      "filter by tag")));
    Object.entries(f.meta || {}).forEach(([k, vals]) => vals.forEach((v) => {
      const term = `${k}=${v.value}`;
      box.appendChild(chip(
        `${term} (${v.n})`, csvHas("f-meta", term),
        () => { csvToggle("f-meta", term); apply(); },
        "filter by metadata"));
    }));
  } catch (_) { /* chips are a convenience, not a requirement */ }
}

async function loadStats() {
  try {
    const s = await api("/stats");
    $("counters").innerHTML = [
      ["images", s.images], ["artifacts", s.artifacts],
      ["faces", s.faces], ["plates", s.plates],
      ["needs review", s.needs_review],
      ["stored", (s.bytes_stored / 1048576).toFixed(1) + " MB"],
    ].map(([k, v]) => `<div>${k} <b>${v}</b></div>`).join("");
  } catch (_) { /* header counters are cosmetic */ }
}

async function openDetail(sha, profile) {
  const d = await api(`/images/${sha}?profile=${profile}`);
  $("m-img").src = `/ui-api/blobs/${sha}?profile=${profile}`;
  const kv = (o) => Object.entries(o).map(
    ([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("");
  $("m-meta").innerHTML = `
    <h3>source</h3>
    <div class="kv">
      <span>sha256</span><span>${d.source_sha}</span>
      <span>profile</span><span>${d.profile_hash}</span>
      <span>size</span><span>${d.image.width}×${d.image.height}</span>
      <span>origin</span><span>${esc(d.image.source_kind)}${
        d.image.source_ref ? " · " + esc(d.image.source_ref) : ""}</span>
      <span>created</span><span>${d.created_at}</span>
      <span>review</span><span>${d.needs_review ? "YES" : "no"}</span>
    </div>
    <h3>detections (${d.detections.length})</h3>
    <div class="kv">${d.detections.map((x) =>
      `<span class="pill ${x.cls}">${x.cls}</span><span>${x.score.toFixed(3)} @ [${
        x.box.join(", ")}] · ${esc(x.detector)}</span>`).join("")}</div>
    <h3>manual regions (${(d.manual_regions || []).length})</h3>
    <div>${(d.manual_regions || []).map((r) =>
      `<span class="pill">${r.shape}</span>`).join("") || "—"}</div>
    <h3>unique codes</h3>
    <div>${(d.codes || []).map((c) => `<span class="pill">${esc(c)}</span>`).join("") || "—"}</div>
    <h3>tags</h3>
    <div>${d.tags.map((t) => `<span class="pill">${esc(t)}</span>`).join("") || "—"}</div>
    <h3>metadata</h3>
    <div class="kv">${kv(d.metadata) || "—"}</div>
    <h3>stats</h3>
    <pre>${esc(JSON.stringify(d.stats, null, 2))}</pre>
    <h3>actions</h3>
    <button class="ghost" id="dl">download</button>
    <button class="ghost" id="api-det">get via API</button>
    <button class="ghost" id="redact">manual redact</button>
    <button class="danger" id="del">delete</button>`;
  $("dl").onclick = () => window.open(`/ui-api/blobs/${sha}?profile=${profile}`, "_blank");
  $("api-det").onclick = () => {
    const prof = `?profile=${profile}`;
    const blocks = [
      "# full record\n" + apiGet(`/v1/images/${sha}${prof}`),
      "# redacted bytes\n" + apiGet(`/v1/blobs/${sha}${prof}`),
      "# thumbnail\n" + apiGet(`/v1/thumbs/${sha}${prof}`),
    ];
    (d.codes || []).forEach((c) => blocks.push(
      `# by producer unique code "${c}" — the consumer hot path\n` +
      apiGet(`/v1/blobs/by-code/${encodeURIComponent(c)}${prof}`)));
    blocks.push(API_KEY_NOTE);
    apiSheet(`GET /v1 — ${sha.slice(0, 16)}…`, blocks);
  };
  $("redact").onclick = () => startRedact(sha, profile, d);
  $("del").onclick = async () => {
    if (!confirm("Delete this image, all its artifacts and blobs?")) return;
    await api(`/images/${sha}`, { method: "DELETE" });
    closeModal(); load(); loadStats();
  };
  $("modal").hidden = false;
}

function closeModal() {
  $("modal").hidden = true; $("m-img").src = "";
  $("m-img").hidden = false; $("m-canvas").hidden = true;
  $("m-redact-bar").hidden = true;
}

// Manual redaction: draw black shapes onto the STORED redacted blob. The
// original is gone, so this can only ever remove information.
const redact = { regions: [], shape: "rect", img: null, drag: null, on: false };

function startRedact(sha, profile, d) {
  redact.on = true;
  redact.regions = (d.manual_regions || []).map((r) => ({ ...r }));
  redact.drag = null;
  const cv = $("m-canvas"), img = $("m-img"), bar = $("m-redact-bar");
  const src = `/ui-api/blobs/${sha}?profile=${profile}&v=${d.blob ? d.blob.sha256 : Date.now()}`;
  const el = new Image();
  el.onload = () => {
    redact.img = el;
    const scale = Math.min(1, 720 / el.naturalWidth);
    cv.width = el.naturalWidth * scale;
    cv.height = el.naturalHeight * scale;
    img.hidden = true; bar.hidden = false; cv.hidden = false;
    drawRedact();
  };
  el.src = src;
  const save = async (regions) => {
    await api(`/images/${sha}/regions?profile=${profile}`,
              { method: "PUT", body: JSON.stringify({ regions }) });
    stopRedact(); openDetail(sha, profile); load(); loadStats();
  };
  $("r-save").onclick = () => save(redact.regions);
  $("r-reviewed").onclick = () => save([]);
  $("r-clear").onclick = () => { redact.regions = []; drawRedact(); };
  $("r-cancel").onclick = stopRedact;
  $("r-rect").onclick = () => setShape("rect");
  $("r-ellipse").onclick = () => setShape("ellipse");
  setShape(redact.shape);
}

function setShape(s) {
  redact.shape = s;
  $("r-rect").classList.toggle("active", s === "rect");
  $("r-ellipse").classList.toggle("active", s === "ellipse");
}

function stopRedact() {
  redact.on = false; redact.img = null; redact.drag = null;
  $("m-canvas").hidden = true; $("m-redact-bar").hidden = true;
  $("m-img").hidden = false;
}

function drawRedact() {
  const cv = $("m-canvas"), ctx = cv.getContext("2d");
  ctx.drawImage(redact.img, 0, 0, cv.width, cv.height);
  ctx.strokeStyle = "#22c55e"; ctx.lineWidth = 2; ctx.setLineDash([5, 4]);
  const all = redact.drag ? redact.regions.concat([redact.drag]) : redact.regions;
  for (const r of all) {
    const x = r.x * cv.width, y = r.y * cv.height,
          w = r.w * cv.width, h = r.h * cv.height;
    if (r.shape === "ellipse") {
      ctx.beginPath();
      ctx.ellipse(x + w / 2, y + h / 2, w / 2, h / 2, 0, 0, 2 * Math.PI);
      ctx.stroke();
    } else ctx.strokeRect(x, y, w, h);
  }
}

$("m-canvas").addEventListener("mousedown", (e) => {
  if (!redact.on) return;
  const cv = $("m-canvas"), b = cv.getBoundingClientRect();
  const nx = (e.clientX - b.left) / b.width, ny = (e.clientY - b.top) / b.height;
  // Click (no drag) on an existing region deletes it, topmost first.
  const hit = [...redact.regions].reverse().find((r) => {
    const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
    return r.shape === "ellipse"
      ? (((nx - cx) / (r.w / 2)) ** 2 + ((ny - cy) / (r.h / 2)) ** 2) <= 1
      : (nx >= r.x && nx <= r.x + r.w && ny >= r.y && ny <= r.y + r.h);
  });
  redact.drag = { shape: redact.shape, sx: nx, sy: ny,
                  x: nx, y: ny, w: 0, h: 0, _hit: hit };
});
$("m-canvas").addEventListener("mousemove", (e) => {
  if (!redact.on || !redact.drag) return;
  const cv = $("m-canvas"), b = cv.getBoundingClientRect();
  const nx = Math.max(0, Math.min(1, (e.clientX - b.left) / b.width));
  const ny = Math.max(0, Math.min(1, (e.clientY - b.top) / b.height));
  const d = redact.drag;
  d.x = Math.min(d.sx, nx); d.y = Math.min(d.sy, ny);
  d.w = Math.abs(nx - d.sx); d.h = Math.abs(ny - d.sy);
  drawRedact();
});
$("m-canvas").addEventListener("mouseup", (e) => {
  if (!redact.on || !redact.drag) return;
  const d = redact.drag; redact.drag = null;
  if (d.w < 0.01 && d.h < 0.01) {          // treated as a click
    if (d._hit) redact.regions = redact.regions.filter((r) => r !== d._hit);
  } else {
    redact.regions.push({ shape: d.shape, x: d.x, y: d.y, w: d.w, h: d.h });
  }
  drawRedact();
});

function jobQuery() {
  const q = new URLSearchParams();
  if ($("j-status").value) q.set("status", $("j-status").value);
  if ($("j-code").value.trim()) q.set("code", $("j-code").value.trim());
  if ($("j-sha").value.trim()) q.set("sha", $("j-sha").value.trim());
  if ($("j-tag").value.trim()) q.set("tag", $("j-tag").value.trim());
  if ($("j-since").value) q.set("since", dayStart($("j-since").value));
  if ($("j-until").value) q.set("until", dayEnd($("j-until").value));
  const [sort, direction] = $("j-sort").value.split(":");
  q.set("sort", sort);
  q.set("direction", direction);
  jstate.limit = parseInt($("j-size").value, 10) || 25;
  q.set("limit", jstate.limit);
  if (jstate.cursor) q.set("cursor", jstate.cursor);
  return q;
}

async function loadJobs() {
  const el = $("j-body");
  try {
    const d = await api("/jobs?" + jobQuery().toString());
    if (d.total !== null && d.total !== undefined) {
      jstate.total = d.total;
      jstate.capped = d.total_capped;
    }
    jstate.shown = d.items.length;
    jstate.nextCursor = d.next_cursor;
    const q = d.queue || {};
    el.innerHTML = `
      <div class="qdepth">${q.queued || 0} queued \u00b7 ${q.running || 0} running \u00b7
        ${q.workers} workers \u00b7 queue cap ${q.queue_max}</div>
      <table><thead><tr><th>job</th><th>status</th><th>unique code</th>
        <th>duration</th><th>created</th><th>detail</th></tr></thead><tbody>` +
      (d.items.length
        ? d.items.map((j) => `<tr>
            <td class="id">${esc(j.job_id)}</td>
            <td><span class="st ${esc(j.status)}">${esc(j.status)}</span>
                ${j.cached ? '<span class="st">cached</span>' : ""}</td>
            <td class="code">${esc(j.external_id || "\u2014")}</td>
            <td>${j.duration_ms == null ? "\u2014" : j.duration_ms.toFixed(0) + " ms"}</td>
            <td>${esc(j.created_at)}</td>
            <td>${j.error ? `<span class="st failed">${esc(j.error.message)}</span>`
                          : esc(j.source_sha ? j.source_sha.slice(0, 16) + "\u2026" : "\u2014")}</td>
          </tr>`).join("")
        : `<tr><td colspan="6">no jobs match these filters</td></tr>`) +
      "</tbody></table>";
    renderPager(jstate, { info: "j-info", prev: "j-prev", next: "j-next", first: "j-first" });
  } catch (err) {
    el.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

async function loadKeys() {
  const el = $("keys");
  try {
    const d = await api("/keys");
    const gate = d.creation_enabled
      ? `<div class="mintbox">
           <div><label>name</label>
             <input id="k-name" placeholder="photo-ingest"></div>
           <div><label>admin secret</label>
             <input id="k-secret" type="password" placeholder="not the dashboard password"></div>
           <div><label>scope: tags (optional, comma separated)</label>
             <input id="k-stags" placeholder="acme"></div>
           <div><label>scope: metadata (optional, k=v)</label>
             <input id="k-smeta" placeholder="appId=acme"></div>
           <button id="k-mint">Create key</button>
         </div>
         <div class="notice" style="border-left-color:var(--accent)">
           <b>Scope</b> restricts a key to one slice of this instance, for
           running several apps off one blurd. All constraints must hold (AND).
           A scoped key's uploads are stamped with its scope automatically, its
           unique codes live in their own namespace — so two apps can both use
           <code>IMG_0042.jpg</code> — and it can neither see nor label another
           tenant's images. Leave both blank for an unrestricted key.
         </div>`
      : `<div class="notice">
           <b>Minting keys here is disabled.</b> A minted key outlives the
           dashboard password, so letting one shared browser login create
           permanent API access is opt-in.<br>
           Enable it with <code>blurd dashboard-keys enable --secret &lt;secret&gt;</code>
           — creation then also requires that second secret.<br>
           <b>Revoking</b> is always available below: it is fail-safe, and it is
           the thing you want to be able to do quickly.
         </div>`;
    el.innerHTML = gate + '<div id="k-new"></div>' + `
      <table><thead><tr><th>id</th><th>name</th><th>scope</th><th>prefix</th>
        <th>created</th><th>last used</th><th>status</th><th></th></tr></thead><tbody>` +
      d.keys.map((k) => `<tr class="${k.revoked ? "revoked" : ""}">
        <td class="id">${esc(k.id)}</td>
        <td class="code">${esc(k.name)}</td>
        <td>${k.scope ? `<span class="pill">${esc(k.scope_description)}</span>`
                      : '<span style="color:var(--dim)">unrestricted</span>'}</td>
        <td>${esc(k.prefix)}…</td>
        <td>${esc(k.created_at)}</td>
        <td>${esc(k.last_used || "never")}</td>
        <td><span class="st ${k.revoked ? "failed" : "live"}">${
              k.revoked ? "revoked" : "active"}</span></td>
        <td>${k.revoked ? "" :
              `<button class="ghost revoke" data-id="${esc(k.id)}"
                       data-name="${esc(k.name)}">revoke</button>`}</td>
      </tr>`).join("") + "</tbody></table>";

    try {
      const log = await api("/audit?limit=25");
      el.insertAdjacentHTML("beforeend", `
        <h3 style="font-size:11px;text-transform:uppercase;letter-spacing:1px;
                   color:var(--dim);margin:26px 0 8px">audit trail</h3>
        <div class="qdepth">Every privileged mutation. The dashboard login is a
          shared password, so "actor" records the channel and address — the most
          that credential can honestly attest to.</div>
        <table><thead><tr><th>when</th><th>actor</th><th>action</th>
          <th>target</th><th>from</th></tr></thead><tbody>` +
        (log.length ? log.map((a) => `<tr>
            <td>${esc(a.at)}</td><td class="code">${esc(a.actor)}</td>
            <td class="id">${esc(a.action)}${a.detail?.blocked
                  ? ` <span class="pill warn">×${a.detail.blocked}</span>` : ""}</td>
            <td>${esc(a.target || "—")}</td><td>${esc(a.source_ip || "—")}</td>
          </tr>`).join("")
          : `<tr><td colspan="5">no privileged mutations recorded</td></tr>`) +
        "</tbody></table>");
    } catch (_) { /* the audit view is informational */ }

    el.querySelectorAll(".revoke").forEach((b) => {
      b.onclick = async () => {
        if (!confirm(`Revoke "${b.dataset.name}"? Any caller using it stops working immediately.`)) return;
        try { await api(`/keys/${b.dataset.id}`, { method: "DELETE" }); loadKeys(); }
        catch (err) { alert(err.message); }
      };
    });

    const mint = $("k-mint");
    if (mint) mint.onclick = async () => {
      const name = $("k-name").value.trim();
      const secret = $("k-secret").value;
      if (!name) { alert("give the key a name"); return; }
      const stags = $("k-stags").value.split(",").map(s => s.trim()).filter(Boolean);
      const smeta = {};
      $("k-smeta").value.split(",").map(s => s.trim()).filter(Boolean).forEach(kv => {
        const i = kv.indexOf("=");
        if (i > 0) smeta[kv.slice(0, i).trim()] = kv.slice(i + 1).trim();
      });
      const scope = (stags.length || Object.keys(smeta).length)
        ? { tags: stags, metadata: smeta } : null;
      mint.disabled = true;
      try {
        const k = await api("/keys", {
          method: "POST",
          headers: { "Content-Type": "application/json",
                     "X-Blurd-Admin-Secret": secret },
          body: JSON.stringify(scope ? { name, scope } : { name }),
        });
        // Shown once. It is never stored in plaintext, so there is no second
        // chance to read it.
        $("k-new").innerHTML = `<div class="newkey">
          <b style="color:var(--ok)">${esc(k.name)}</b> created —
          scope <b>${esc(k.scope_description)}</b>, tenant
          <b>${esc(k.tenant)}</b>. Copy it now, it is stored only as a hash and
          cannot be shown again.
          <code>${esc(k.key)}</code>
          <button class="ghost" id="k-copy">copy</button>
          <button class="ghost" id="k-hide">done</button></div>`;
        $("k-copy").onclick = () => navigator.clipboard?.writeText(k.key);
        $("k-hide").onclick = () => loadKeys();
        $("k-name").value = ""; $("k-secret").value = "";
        $("k-stags").value = ""; $("k-smeta").value = "";
        const rows = await api("/keys");
        void rows;
      } catch (err) {
        alert(err.message + (err.detail?.details?.enable
              ? "\n\n" + err.detail.details.enable : ""));
      } finally { mint.disabled = false; }
    };
  } catch (err) {
    el.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

async function loadPublic() {
  const el = $("public");
  try {
    const rules = await api("/public-rules");
    el.innerHTML = `
      <div class="notice">
        <b>Public blob URLs.</b> A rule publishes every image carrying its
        predicate — one tag <i>or</i> one metadata <code>k=v</code>, optionally
        limited to a tenant's labels. Public reads are
        <code>GET /pub/blobs/&lt;sha&gt;</code> — no API key, no code lookup
        (codes are enumerable; a sha256 is not). Rules are evaluated on every
        request, so deleting one revokes access immediately. Public endpoints
        are rate-limited per address; blocked bursts appear in the audit
        trail below.
      </div>
      <div class="mintbox">
        <div><label>name</label>
          <input id="pr-name" placeholder="geored public dataset"></div>
        <div><label>tag</label><input id="pr-tag" placeholder="geored:public"></div>
        <div><label>or metadata</label>
          <input id="pr-meta" placeholder="visibility=public"></div>
        <div><label>tenant (optional)</label>
          <input id="pr-tenant" placeholder="only this tenant's labels count"></div>
        <button id="pr-add">Add rule</button>
      </div>
      <table><thead><tr><th>name</th><th>predicate</th><th>tenant</th>
        <th>created</th><th></th></tr></thead><tbody>` +
      (rules.length ? rules.map((r) => `<tr>
        <td>${esc(r.name)}</td>
        <td class="code">${r.tag ? `tag: ${esc(r.tag)}`
            : `meta: ${esc(r.meta_key)}=${esc(r.meta_value)}`}</td>
        <td>${r.tenant ? `<span class="pill">${esc(r.tenant)}</span>`
            : '<span style="color:var(--dim)">any</span>'}</td>
        <td>${esc(r.created_at)}</td>
        <td><button class="ghost pr-del" data-id="${esc(r.id)}"
                    data-name="${esc(r.name)}">delete</button></td>
      </tr>`).join("")
        : `<tr><td colspan="5">no public rules — everything requires a key</td></tr>`) +
      "</tbody></table>";

    // Rate-limited bursts surface here, already grouped: one row per
    // (address, window), with the suppressed-hit count in detail.blocked.
    try {
      const log = await api("/audit?limit=200");
      const hits = log.filter((a) => a.action === "rate_limited").slice(0, 25);
      el.insertAdjacentHTML("beforeend", `
        <h3 style="font-size:11px;text-transform:uppercase;letter-spacing:1px;
                   color:var(--dim);margin:26px 0 8px">rate-limit events</h3>
        <table><thead><tr><th>when</th><th>class</th><th>path</th>
          <th>from</th><th>blocked</th></tr></thead><tbody>` +
        (hits.length ? hits.map((a) => `<tr>
            <td>${esc(a.at)}</td><td>${esc(a.detail?.class || "—")}</td>
            <td class="code">${esc(a.target || "—")}</td>
            <td>${esc(a.source_ip || "—")}</td>
            <td><span class="pill warn">×${a.detail?.blocked || 1}</span></td>
          </tr>`).join("")
          : `<tr><td colspan="5">no rate-limit events</td></tr>`) +
        "</tbody></table>");
    } catch (_) { /* informational */ }

    el.querySelectorAll(".pr-del").forEach((b) => {
      b.onclick = async () => {
        if (!confirm(`Delete rule "${b.dataset.name}"? Public URLs it granted stop working immediately.`)) return;
        try { await api(`/public-rules/${b.dataset.id}`, { method: "DELETE" }); loadPublic(); }
        catch (err) { alert(err.message); }
      };
    });
    $("pr-add").onclick = async () => {
      const name = $("pr-name").value.trim();
      const tag = $("pr-tag").value.trim();
      const meta = $("pr-meta").value.trim();
      const tenant = $("pr-tenant").value.trim();
      if (!name) { alert("give the rule a name"); return; }
      const body = { name };
      if (tag) body.tag = tag;
      if (meta) {
        const i = meta.indexOf("=");
        if (i <= 0) { alert("metadata predicate must be k=v"); return; }
        body.meta_key = meta.slice(0, i).trim();
        body.meta_value = meta.slice(i + 1).trim();
      }
      if (tenant) body.tenant = tenant;
      try { await api("/public-rules", { method: "POST", body: JSON.stringify(body) });
            loadPublic(); }
      catch (err) { alert(err.message); }
    };
  } catch (err) {
    el.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

/* The API tab renders spec/openapi.yaml pre-compiled to /api-docs.json by
   spec/render_api_docs.py — no YAML parser ships to the browser, and
   tests/api_docs_drift.py keeps the JSON honest. It is a reference for humans,
   not a console: nothing here executes a call. */
function paramRows(ps) {
  if (!ps || !ps.length) return "";
  return `<table><thead><tr><th>param</th><th>in</th><th>type</th>
    <th>req</th><th></th></tr></thead><tbody>` +
    ps.map((p) => `<tr>
      <td class="code">${esc(p.name)}</td><td>${esc(p.in)}</td>
      <td>${esc(p.type)}${p.enum ? `<div class="enum">${esc(p.enum.join(" | "))}</div>` : ""}${
        p.default !== undefined && p.default !== null
          ? `<div class="enum">default ${esc(String(p.default))}</div>` : ""}</td>
      <td>${p.required ? '<span class="pill warn">yes</span>' : ""}</td>
      <td>${esc(p.description)}</td></tr>`).join("") +
    "</tbody></table>";
}

function bodyRows(b) {
  if (!b) return "";
  return `<h4>request body — ${b.content_types.map(esc).join(", ")}</h4>` +
    (b.fields ? `<table><thead><tr><th>field</th><th>type</th><th>req</th><th></th></tr>
      </thead><tbody>` +
      b.fields.map((f) => `<tr><td class="code">${esc(f.name)}</td>
        <td>${esc(f.type)}</td>
        <td>${f.required ? '<span class="pill warn">yes</span>' : ""}</td>
        <td>${esc(f.description)}</td></tr>`).join("") +
      "</tbody></table>"
    : `<div class="qdepth">raw image bytes</div>`);
}

function responseRows(rs, schemas) {
  if (!rs || !rs.length) return "";
  return `<table><thead><tr><th>status</th><th>shape</th><th>meaning</th></tr>
    </thead><tbody>` +
    rs.map((r) => {
      let shape = "";
      if (r.schema) shape = `<a class="schemaref" href="#api-schema-${esc(r.schema)}">${esc(r.schema)}</a>`;
      else if (r.fields) shape = r.fields.map((f) => esc(f.name)).join(", ");
      return `<tr><td class="id">${esc(r.status)}</td><td>${shape}</td>
        <td>${esc(r.description)}</td></tr>`;
    }).join("") + "</tbody></table>";
}

async function loadApiDocs() {
  const el = $("api");
  try {
    const doc = await (await fetch("/api-docs.json")).json();
    const groups = {};
    doc.endpoints.forEach((e) => (groups[e.group] = groups[e.group] || []).push(e));
    const order = ["producer — submit images, track jobs",
                   "consumer — fetch redacted output",
                   "open — no API key by design",
                   "operator — instance management",
                   "dashboard — session-authed, not for integrations"];
    el.innerHTML = `<div class="notice">
        <b>Machine API reference</b> — generated from <code>spec/openapi.yaml</code>,
        the same file the Go/machin ports implement. Every call is JSON in,
        JSON out; errors share one typed vocabulary with the CLI. All
        <code>/v1</code> calls need <code>Authorization: Bearer &lt;key&gt;</code>
        — a scoped key only ever sees its own tenant's rows, and out-of-scope
        reads answer 404, never 403.
        Mint keys in the <b>keys</b> tab or with <code>blurd keys add</code>.
      </div>` +
      order.filter((g) => groups[g]).map((g) =>
        `<h3 class="api-group">${esc(g)}</h3>` +
        groups[g].map((e) => `<div class="endpoint">
          <div class="ep-head">
            <span class="method ${e.method.toLowerCase()}">${e.method}</span>
            <code class="ep-path">${esc(e.path)}</code>
            <span class="ep-auth ${e.auth === "none" ? "open" : ""}">${esc(e.auth)}</span>
          </div>
          ${e.summary ? `<div class="ep-summary">${esc(e.summary)}</div>` : ""}
          ${e.description ? `<div class="ep-desc">${esc(e.description)}</div>` : ""}
          ${paramRows(e.params)}
          ${bodyRows(e.body)}
          ${e.responses && e.responses.length ? "<h4>responses</h4>" + responseRows(e.responses, doc.schemas) : ""}
        </div>`).join("")).join("") +
      (Object.keys(doc.schemas || {}).length
        ? `<h3 class="api-group">response schemas</h3>` +
          Object.entries(doc.schemas).map(([name, s]) => `<div class="endpoint">
            <div class="ep-head"><code class="ep-path" id="api-schema-${esc(name)}">${esc(name)}</code></div>
            <table><thead><tr><th>field</th><th>type</th><th>req</th><th></th></tr>
              </thead><tbody>` +
            s.fields.map((f) => `<tr><td class="code">${esc(f.name)}</td>
              <td>${esc(f.type)}</td>
              <td>${f.required ? '<span class="pill warn">yes</span>' : ""}</td>
              <td>${esc(f.description)}</td></tr>`).join("") +
            "</tbody></table>" +
            (s.example ? `<details class="example"><summary>example</summary>
              <pre>${esc(JSON.stringify(s.example, null, 2))}</pre></details>` : "") +
            "</div>").join("")
        : "");
    // Deep link: /#api-schema-<Name> scrolls to the card once it exists.
    const anchor = location.hash.slice(1);
    if (anchor.startsWith("api-schema-")) {
      const t = document.getElementById(anchor);
      if (t) t.scrollIntoView({ block: "center" });
    }
  } catch (err) {
    el.innerHTML = `<div class="empty">could not load /api-docs.json — ${esc(err.message)}</div>`;
  }
}

/* The CLI tab renders the embedded guide (src/guide.py — the same document
   `blurd guide` and --help-json emit) pre-compiled to /cli-docs.json by
   spec/render_cli_docs.py, kept honest by tests/cli_docs_drift.py. */
async function loadCliDocs() {
  const el = $("cli");
  try {
    const doc = await (await fetch("/cli-docs.json")).json();
    const g = doc.guide, cat = doc.catalog;
    el.innerHTML = `<div class="notice">
        <b>The blurd CLI.</b> ${esc(g.one_liner)}<br><br>
        <b>The model:</b> ${esc(g.model)}<br><br>
        <b>The loop:</b> ${esc(g.loop)}<br><br>
        Every command runs three ways unchanged — locally in-process,
        via <code>--remote &lt;url&gt; --api-key &lt;key&gt;</code> against a
        daemon like this one, or inside the dashboard's api tab endpoints.
      </div>
      <h3 class="api-group">commands</h3>
      <table><thead><tr><th>command</th><th>does</th></tr></thead><tbody>` +
      Object.entries(cat.commands).map(([c, d]) =>
        `<tr><td class="code">blurd ${esc(c)}</td><td>${esc(d)}</td></tr>`).join("") +
      `</tbody></table>
      <details class="example" open><summary>full reference — every flag,
        as the binary prints it (blurd guide)</summary>
        <pre>${esc(doc.text)}</pre></details>
      <h3 class="api-group">concepts</h3>
      <table><thead><tr><th>term</th><th>meaning</th></tr></thead><tbody>` +
      Object.entries(g.concepts || {}).map(([k, v]) =>
        `<tr><td class="code">${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") +
      `</tbody></table>
      <h3 class="api-group">global flags & output</h3>
      <div class="qdepth">${cat.global_flags.map((f) =>
        `<span class="pill">${esc(f)}</span>`).join(" ")}</div>
      <div class="qdepth">output formats: ${cat.output_formats.map(esc).join(", ")}
        — JSON is the contract (<code>{"version","data","timestamp"}</code> on
        stdout; logs and progress on stderr, always).</div>
      <h3 class="api-group">examples</h3>
      <pre>${(g.examples || []).map(esc).join("\n")}</pre>
      <h3 class="api-group">gotchas</h3>
      <ul class="gotchas">${(g.gotchas || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <h3 class="api-group">exit codes</h3>
      <div class="qdepth">exit code == error.code — one vocabulary for CLI and API.</div>
      <table><thead><tr><th>code</th><th>type</th></tr></thead><tbody>` +
      Object.entries(cat.exit_codes).map(([c, t]) =>
        `<tr><td class="id">${esc(c)}</td><td class="code">${esc(t)}</td></tr>`).join("") +
      "</tbody></table>";
  } catch (err) {
    el.innerHTML = `<div class="empty">could not load /cli-docs.json — ${esc(err.message)}</div>`;
  }
}

/* Tabs are routed through location.hash so a view can be shared as a URL —
   /#cli opens the CLI docs directly, /#api-schema-Artifact deep-links one
   schema card. */
const TAB_LOADERS = { images: load, jobs: loadJobs, keys: loadKeys,
                      public: loadPublic, api: loadApiDocs, cli: loadCliDocs };
function activateTab(view) {
  document.querySelectorAll(".tab").forEach((x) =>
    x.classList.toggle("active", x.dataset.view === view));
  const images = view === "images";
  $("jobs").hidden = view !== "jobs";
  $("keys").hidden = view !== "keys";
  $("public").hidden = view !== "public";
  $("api").hidden = view !== "api";
  $("cli").hidden = view !== "cli";
  $("grid").hidden = !images;
  // By id, not by class: `#jobs` sits BEFORE the images pager in the DOM, so
  // querySelector(".pager") picks the jobs one and hides it exactly when the
  // jobs tab needs it.
  $("img-filters").hidden = !images;
  $("img-pager").hidden = !images;
  $("empty").hidden = true;
  (TAB_LOADERS[view] || load)();
}
function tabFromHash() {
  const h = location.hash.slice(1);
  if (TAB_LOADERS[h]) return h;
  if (h.startsWith("api-schema-")) return "api";
  return "images";
}
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => { location.hash = t.dataset.view; activateTab(t.dataset.view); };
});
window.addEventListener("hashchange", () => {
  const v = tabFromHash();
  const current = document.querySelector(".tab.active");
  if (current && current.dataset.view === v) return; // click already routed it
  activateTab(v);
});

/* Keyset paging only moves forward, so "prev" is the stack of cursors already
   visited and "first" is an empty stack. */
function wirePager(p, ids, reload) {
  $(ids.next).onclick = () => {
    if (!p.nextCursor) return;
    p.stack.push(p.cursor);
    p.cursor = p.nextCursor;
    reload();
  };
  $(ids.prev).onclick = () => {
    if (!p.stack.length) return;
    p.cursor = p.stack.pop();
    reload();
  };
  $(ids.first).onclick = () => { p.stack = []; p.cursor = null; reload(); };
}

function resetPager(p) { p.stack = []; p.cursor = null; p.nextCursor = null; }

$("apply").onclick = () => { resetPager(state); load(); };
$("reset").onclick = () => {
  ["f-tag", "f-meta", "f-sha", "f-code", "f-since", "f-until"].forEach((i) => ($(i).value = ""));
  $("f-review").checked = false;
  $("f-sort").value = "created:desc"; $("f-size").value = "24";
  resetPager(state); load();
};
$("f-sort").onchange = $("f-size").onchange = () => { resetPager(state); load(); };
wirePager(state, { prev: "prev", next: "next", first: "first" }, load);

$("j-apply").onclick = () => { resetPager(jstate); loadJobs(); };
$("j-reset").onclick = () => {
  ["j-code", "j-sha", "j-tag", "j-since", "j-until"].forEach((i) => ($(i).value = ""));
  $("j-status").value = ""; $("j-sort").value = "created:desc"; $("j-size").value = "25";
  resetPager(jstate); loadJobs();
};
$("j-sort").onchange = $("j-size").onchange = $("j-status").onchange =
  () => { resetPager(jstate); loadJobs(); };
["j-code", "j-sha", "j-tag", "j-since", "j-until"].forEach((i) =>
  ($(i).onkeydown = (e) => { if (e.key === "Enter") { resetPager(jstate); loadJobs(); } }));
wirePager(jstate, { prev: "j-prev", next: "j-next", first: "j-first" }, loadJobs);
$("close").onclick = closeModal;
$("modal").onclick = (e) => { if (e.target === $("modal")) closeModal(); };
document.onkeydown = (e) => { if (e.key === "Escape") closeModal(); };
["f-tag", "f-meta", "f-sha", "f-code", "f-since", "f-until"].forEach((i) =>
  ($(i).onkeydown = (e) => { if (e.key === "Enter") { resetPager(state); load(); } }));

activateTab(tabFromHash()); loadStats(); loadFacets();
