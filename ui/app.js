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
  const tags = item.tags.map((t) => `<span class="pill">${esc(t)}</span>`).join("");
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
      </div>
      <div>${tags}</div>
    </div>`;
  el.onclick = () => openDetail(item.source_sha, item.profile_hash);
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
    $("empty").hidden = data.items.length !== 0;
    renderPager(state, { info: "page-info", prev: "prev", next: "next", first: "first" });
  } catch (err) {
    grid.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
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
    <button class="danger" id="del">delete</button>`;
  $("dl").onclick = () => window.open(`/ui-api/blobs/${sha}?profile=${profile}`, "_blank");
  $("del").onclick = async () => {
    if (!confirm("Delete this image, all its artifacts and blobs?")) return;
    await api(`/images/${sha}`, { method: "DELETE" });
    closeModal(); load(); loadStats();
  };
  $("modal").hidden = false;
}

function closeModal() { $("modal").hidden = true; $("m-img").src = ""; }

function jobQuery() {
  const q = new URLSearchParams();
  if ($("j-status").value) q.set("status", $("j-status").value);
  if ($("j-code").value.trim()) q.set("code", $("j-code").value.trim());
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
            <td class="id">${esc(a.action)}</td>
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

document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    const view = t.dataset.view;
    const images = view === "images";
    $("jobs").hidden = view !== "jobs";
    $("keys").hidden = view !== "keys";
    $("grid").hidden = !images;
    // By id, not by class: `#jobs` sits BEFORE the images pager in the DOM, so
    // querySelector(".pager") picks the jobs one and hides it exactly when the
    // jobs tab needs it.
    $("img-filters").hidden = !images;
    $("img-pager").hidden = !images;
    $("empty").hidden = true;
    if (images) load();
    else if (view === "jobs") loadJobs();
    else loadKeys();
  };
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
  ["j-code", "j-since", "j-until"].forEach((i) => ($(i).value = ""));
  $("j-status").value = ""; $("j-sort").value = "created:desc"; $("j-size").value = "25";
  resetPager(jstate); loadJobs();
};
$("j-sort").onchange = $("j-size").onchange = $("j-status").onchange =
  () => { resetPager(jstate); loadJobs(); };
$("j-code").onkeydown = (e) => { if (e.key === "Enter") { resetPager(jstate); loadJobs(); } };
wirePager(jstate, { prev: "j-prev", next: "j-next", first: "j-first" }, loadJobs);
$("close").onclick = closeModal;
$("modal").onclick = (e) => { if (e.target === $("modal")) closeModal(); };
document.onkeydown = (e) => { if (e.key === "Escape") closeModal(); };
["f-tag", "f-meta", "f-sha", "f-code", "f-since", "f-until"].forEach((i) =>
  ($(i).onkeydown = (e) => { if (e.key === "Enter") { resetPager(state); load(); } }));

load(); loadStats();
