#!/usr/bin/env python3
"""Render spec/openapi.yaml -> ui/api-docs.json for the dashboard's API tab.

Committed output, not runtime work: the browser loads one static JSON, so the
UI needs no YAML parser and the daemon keeps zero doc-serving routes. Run this
whenever the spec changes; tests/api_docs_drift.py fails if the committed JSON
no longer matches what the spec would produce. A path that is not classified
in GROUPS raises — adding an endpoint forces a conscious choice of which
audience it serves.

Grouping is the doc's editorial layer, so it lives here, not in the spec:
'producer' = submit + track, 'consumer' = read redacted output,
'operator' = instance management, 'open' = unauthenticated by design,
'dashboard' = the /ui-api session surface an integrator should NOT call.
"""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec" / "openapi.yaml"
OUT = ROOT / "ui" / "api-docs.json"

# Hand-curated examples beat type-derived mocks: field VALUES carry meaning
# (a source_sha looks like hex, a job_id like "job_…"), which a generated
# stub cannot show. Kept in sync by eye — they are illustrative, not contract.
EXAMPLES = {
    "Job": {
        "job_id": "job_c87d71ab975dbcb4", "status": "done",
        "external_id": "IMG_20250922_114502.jpg",
        "source_sha": "276455eb10e8e32ec238dff30297151b125b32634e1d4c0547e9c698b1996d2e",
        "profile_hash": "1e994f69934887ce",
        "source_kind": "stream", "cached": False, "attempts": 1,
        "created_at": "2026-09-25T08:17:28Z", "started_at": "2026-09-25T08:17:28Z",
        "finished_at": "2026-09-25T08:17:29Z", "duration_ms": 812.4,
        "blob_url": "/v1/blobs/276455eb…",
        "blob_url_by_code": "/v1/blobs/by-code/IMG_20250922_114502.jpg",
        "result": {"source_sha": "276455eb…", "note": "Artifact shape, see schema below"},
    },
    "Artifact": {
        "source_sha": "276455eb10e8e32ec238dff30297151b125b32634e1d4c0547e9c698b1996d2e",
        "profile_hash": "1e994f69934887ce", "cached": False,
        "needs_review": False, "manual_regions": [],
        "blob": {"url": "/v1/blobs/276455eb…",
                 "sha256": "0e393b18…", "bytes": 412308, "mime": "image/jpeg"},
        "image": {"width": 2048, "height": 1536, "mime": "image/jpeg",
                  "source_kind": "stream", "source_ref": None},
        "codes": ["IMG_20250922_114502.jpg"],
        "tags": ["isoprod-demo"], "metadata": {"source": "isoprod"},
        "detections": [{"cls": "face", "box": [611, 402, 118, 132],
                        "score": 0.91, "detector": "yunet-2023mar"}],
        "stats": {"timings": {"fetch": 0, "decode": 41, "detect_face": 380,
                              "detect_plate": 290, "redact": 55, "encode": 38,
                              "store": 8, "total": 812}},
        "created_at": "2026-09-25T08:17:29Z", "errors": [], "warnings": [],
    },
    "Error": {
        "ok": False,
        "error": {"code": 92, "type": "not_found",
                  "message": "no such external_id in this tenant",
                  "recoverable": False, "retry_after": None,
                  "suggestions": []},
    },
}

GROUPS = {
    "producer — submit images, track jobs": {
        "/v1/images": ["post"],
        "/v1/jobs": ["get"],
        "/v1/jobs/{job_id}": ["get"],
    },
    "consumer — fetch redacted output": {
        "/v1/images": ["get"],
        "/v1/images/{sha}": ["get"],
        "/v1/blobs/{sha}": ["get"],
        "/v1/thumbs/{sha}": ["get"],
        "/v1/images/by-code/{external_id}": ["get"],
        "/v1/blobs/by-code/{external_id}": ["get"],
        "/v1/thumbs/by-code/{external_id}": ["get"],
    },
    "open — no API key by design": {
        "/v1/health": ["get"],
        "/_health": ["get"],
        "/v1/feedback": ["post"],
        "/pub/blobs/{sha}": ["get"],
    },
    "operator — instance management": {
        "/_shutdown": ["post"],
        "/v1/feedback": ["get"],
        "/v1/stats": ["get"],
        "/v1/images/{sha}": ["delete"],
    },
    "dashboard — session-authed, not for integrations": {
        "/ui-api/keys": ["get", "post"],
        "/ui-api/keys/{key_id}": ["delete"],
        "/ui-api/audit": ["get"],
    },
}


def classify(path, method):
    for label, table in GROUPS.items():
        if path in table and method in table[path]:
            return label
    raise SystemExit(f"ungrouped endpoint: {method.upper()} {path} — "
                     f"classify it in spec/render_api_docs.py GROUPS")


def type_of(schema):
    if not isinstance(schema, dict):
        return "string"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    t = schema.get("type", "string")
    if t == "array":
        return f"{type_of(schema.get('items'))}[]"
    return t


def params(op):
    out = []
    for p in op.get("parameters") or []:
        s = p.get("schema") or {}
        out.append({
            "name": p.get("name"), "in": p.get("in"),
            "required": bool(p.get("required")),
            "type": type_of(s), "enum": s.get("enum"),
            "default": s.get("default"),
            "description": p.get("description") or "",
        })
    return out


def json_fields(schema, prefix=""):
    """Flatten nested objects into dot-path rows — `blob.url`, `detections[].cls` —
    so one table documents the whole tree instead of an unhelpful `object`."""
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    out = []
    for k, v in props.items():
        v = v if isinstance(v, dict) else {}
        name = prefix + k
        t = type_of(v)
        out.append({"name": name, "type": t, "required": k in required,
                    "description": v.get("description", "")})
        sub = v.get("properties")
        if not sub and v.get("type") == "array":
            sub = (v.get("items") or {}).get("properties")
            if sub:
                name += "[]"
        if sub:
            out.extend(json_fields({"properties": sub, "required": v.get("required")},
                                   name + "."))
    return out


def body(op):
    content = ((op.get("requestBody") or {}).get("content")) or {}
    if not content:
        return None
    out = {"content_types": list(content.keys())}
    js = content.get("application/json") or {}
    if js.get("schema"):
        out["fields"] = json_fields(js["schema"])
    return out


def responses(op):
    out = []
    for status, r in (op.get("responses") or {}).items():
        entry = {"status": status, "description": r.get("description", "")}
        js = ((r.get("content") or {}).get("application/json")) or {}
        schema = js.get("schema") or {}
        if "$ref" in schema:
            entry["schema"] = schema["$ref"].rsplit("/", 1)[-1]
        elif schema.get("properties"):
            entry["fields"] = json_fields(schema)
        out.append(entry)
    return out


def render():
    spec = yaml.safe_load(SPEC.read_text())
    endpoints = []
    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete"):
                continue
            endpoints.append({
                "path": path, "method": method.upper(),
                "group": classify(path, method),
                "summary": op.get("summary", ""),
                "description": (op.get("description") or "").strip(),
                "auth": "none" if op.get("security") == [] else "api key",
                "params": params(op),
                "body": body(op),
                "responses": responses(op),
            })
    schemas = spec.get("components", {}).get("schemas", {})
    return {
        "generated_from": "spec/openapi.yaml",
        "title": spec.get("info", {}).get("title", "blurd API"),
        "endpoints": endpoints,
        "schemas": {
            name: {"fields": json_fields(s), "example": EXAMPLES.get(name)}
            for name, s in schemas.items()
            if isinstance(s, dict) and s.get("properties")
        },
    }


def main():
    doc = render()
    if "--check" in sys.argv:
        current = json.loads(OUT.read_text()) if OUT.exists() else None
        if current != doc:
            print("ui/api-docs.json is stale — run spec/render_api_docs.py")
            sys.exit(1)
        print(f"api docs in sync: {len(doc['endpoints'])} endpoints")
        return
    OUT.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {OUT}: {len(doc['endpoints'])} endpoints, "
          f"{len(doc['schemas'])} schemas")


if __name__ == "__main__":
    main()
