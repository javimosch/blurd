/* Tooltips for every labelled control in the dashboard.

   One map, keyed by the CONTROL's id (or "name=value" for radios, "class:value"
   for repeated checkboxes, "th:<text>" for table headers that label a column of
   inputs). An observer applies it to whatever the tabs render, so a label never
   needs its own title attribute in the markup: add the id here instead.
   The text explains what the control does and its consequences, not its name. */
const HELP = {
  // images tab filters
  "f-since": "Only images processed on or after this day (UTC).",
  "f-until": "Only images processed on or before this day (UTC).",
  "f-sort": "Order of the grid. Every option is index-backed, so paging stays fast at any depth.",
  "f-size": "Images per page. The total is counted on the first page only (capped at 10,000).",
  "f-review": "Only images flagged needs_review: no detection at all, or a weak one (score below 0.55). Saving manual regions clears the flag.",
  "f-faces": "Only images where at least one face was detected.",
  "f-plates": "Only images where at least one licence plate was detected.",
  "f-nofaces": "Only images with no face detected.",
  "f-noplates": "Only images with no licence plate detected.",
  // jobs tab filters
  "j-status": "queued: waiting for a worker · running: being processed · done: result stored · failed: see the error (resubmit uploads; URL jobs are retried after a crash).",
  "j-since": "Only jobs submitted on or after this day (UTC).",
  "j-until": "Only jobs submitted on or before this day (UTC).",
  "j-sort": "Order of the job list; slowest/fastest sort by processing duration.",
  "j-size": "Jobs per page.",
  // keys tab: minting
  "k-name": "A label for people (e.g. the integrating app). Renamable later; it is not the secret.",
  "k-secret": "The second secret (BLURD_DASHBOARD_KEY_SECRET) required for every mint, because a minted key outlives the dashboard password.",
  "k-stags": "Restrict the key to images carrying ALL these tags. Writes are stamped with them; the key cannot see anything else. Empty + empty metadata = unrestricted key.",
  "k-smeta": "Restrict the key to images with these metadata pairs (k=v, comma separated), ANDed with the tags. Defines the key's tenant.",
  // keys tab: scope limits (column headers)
  "th:max input (MB)": "Largest source image this scope may send; bigger ones are refused with 422 naming the limit. Empty = the instance's max input.",
  "th:optimize above (MB)": "Sources larger than this are downscaled and recompressed before redaction. A request may ask for a lower threshold, never a higher one.",
  "th:max side (px)": "Longest side an optimized image is resized to (256–16384, default 2560). A request may only ask for smaller.",
  "th:quality": "JPEG quality of an optimized image (30–95, default 82). A request may only ask for lower.",
  "th:retention (h)": "How long redacted images of this scope are kept before their bytes are pruned (reads then answer 410). A request may only shorten it. Empty = forever.",
  "th:keep original max (h)": "Ceiling for keep_original on this scope's requests (only if original retention is enabled). Empty = the instance maximum.",
  // public tab
  "pr-name": "A label for people; shown in the rule list and the audit trail.",
  "pr-tag": "Images carrying this tag become readable without a key at /pub/blobs/<sha>. Evaluated at read time: removing the tag or the image revokes access at once.",
  "pr-meta": "Alternative to the tag: images with this metadata pair (k=v) become public.",
  "pr-tenant": "Only labels set by this tenant count, because tags are caller-chosen: without it, any app could make an image public by tagging it.",
  // settings: redaction
  "s-mode": "How detected regions are hidden by default: pixelate, blur or solid (black). Changes the profile, so new submissions produce new artifacts.",
  "s-face": "Redaction mode for faces only; overrides the default mode. Empty = use the default.",
  "s-plate": "Redaction mode for licence plates only; overrides the default mode. Empty = use the default.",
  "class:s-allow": "Modes a request may ask for. A request using a mode outside this list is refused with 422.",
  // settings: detection
  "d-fs": "Minimum confidence for a face to be redacted. Lower = more faces caught but more false positives (and more redacted pixels).",
  "d-ps": "Minimum confidence for a licence plate to be redacted. Lower = more plates caught but more false positives.",
  "d-pa": "Minimum width/height ratio for a plate box (plates are wider than tall); boxes narrower than this are ignored as false positives. 0 = no filter.",
  "d-pm": "Plate boxes larger than this share of the image are ignored as false positives (a plate is never most of a photo). Empty = no limit.",
  // settings: storage & defaults
  "r-in": "Largest source accepted (upload or URL fetch) for every key. Scope limits can only be lower.",
  "r-oa": "Instance default: sources above this size are downscaled and recompressed before redaction. Empty = off.",
  "r-os": "Longest side an optimized image is resized to (default 2560 px).",
  "r-oq": "JPEG quality used for optimized images (default 82).",
  "r-q": "JPEG quality of every redacted output (30–95). Higher = larger files.",
  "r-ttl": "Default retention of redacted images; after it, bytes are pruned and reads answer 410 (the record stays). Empty = forever. Scope limits can only be shorter.",
  "r-cap": "Budget for redacted images + kept originals, on disk or in S3. Without rotation, new work is refused with 507 at the cap. Empty = no cap.",
  "r-ev": "oldest: past the high mark, delete the oldest images' bytes down to the low mark (images with open feedback last; records stay, reads answer 410 evicted). off: refuse new work at the cap.",
  "r-hi": "Rotation starts when usage passes this share of the cap.",
  "r-lo": "Rotation stops when usage is back under this share of the cap. Must be below the high mark; the gap is how much is freed per pass.",
  // settings: orphans
  "class:o-cls": "Tick to include this class in the deletion. Only objects no record points at are listed; nothing an image, code or link uses is touched.",
  // settings: original retention
  "o-enabled": "Allow requests to keep the SOURCE image (keep_original) for a limited time, so an image can be reprocessed later. Off = sources are never stored.",
  "o-max": "Longest a request may keep an original. Every kept original is deleted at its deadline by the sweep.",
  "o-mode": "How original URLs are handed out: authenticated (the reader needs an API key) or signed (a time-limited link that needs no key).",
  "o-sign": "How long a signed original URL stays valid after it is issued.",
  // image detail: reprocess
  "rp-src=retained": "Use the original kept at submission (keep_original). Available only while it is retained.",
  "rp-src=source_url": "Fetch the image again from the URL it was submitted with. Fails if the URL no longer serves the same bytes.",
  "rp-src=url": "Fetch from another URL; the bytes must be identical to the stored image (same sha256).",
  "rp-src=upload": "Upload the file from this computer; it must be byte-identical to the stored image.",
  "rp-keep": "Also retain the original for 30 days so the image can be reprocessed again later (needs original retention enabled).",
};

function helpKey(el) {
  if (el.type === "radio" && el.name) return `${el.name}=${el.value}`;
  if (el.id && HELP[el.id]) return el.id;
  for (const c of el.classList || []) if (HELP[`class:${c}`]) return `class:${c}`;
  return el.id;
}

function controlOf(label) {
  if (label.htmlFor) return document.getElementById(label.htmlFor);
  const inner = label.querySelector("input,select,textarea");
  if (inner) return inner;
  // <div><label>x</label><input></div>: the control is a sibling.
  return label.parentElement && label.parentElement.querySelector("input,select,textarea");
}

function applyHelp(root) {
  root.querySelectorAll("label:not([data-help])").forEach((label) => {
    const ctl = controlOf(label);
    const text = ctl && HELP[helpKey(ctl)];
    label.dataset.help = text ? "1" : "0";
    if (!text) return;
    label.title = text;
    label.classList.add("has-help");
    if (!ctl.title) ctl.title = text;
  });
  root.querySelectorAll("th:not([data-help])").forEach((th) => {
    const text = HELP[`th:${th.textContent.trim()}`];
    th.dataset.help = text ? "1" : "0";
    if (!text) return;
    th.title = text;
    th.classList.add("has-help");
  });
}

new MutationObserver(() => applyHelp(document.body))
  .observe(document.documentElement, { childList: true, subtree: true });
document.addEventListener("DOMContentLoaded", () => applyHelp(document.body));
