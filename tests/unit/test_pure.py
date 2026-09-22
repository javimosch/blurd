"""Unit tests for the pure functions — no daemon, no I/O.

These complement the black-box suites, which can only observe behaviour over
HTTP. The functions here are the ones where a wrong answer is silent:
canonical_json feeds profile_hash (cache identity), Scope builds the tenant
predicates, and the error vocabulary maps exit codes to HTTP statuses.
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src import canonical, errors, scope  # noqa: E402


# -- canonical ---------------------------------------------------------------

def test_canonical_json_sorted_no_whitespace():
    out = canonical.canonical_json({"b": 1, "a": 2})
    assert out == '{"a":2,"b":1}'


def test_canonical_json_nested_sorted():
    out = canonical.canonical_json({"z": {"b": 1, "a": 2}, "a": [{"y": 1, "x": 2}]})
    assert out == '{"a":[{"x":2,"y":1}],"z":{"a":2,"b":1}}'


def test_canonical_json_no_unicode_escapes():
    # the portability contract says non-ASCII is emitted literally
    out = canonical.canonical_json({"k": "é"})
    assert "é" in out and "\\u" not in out


def test_profile_hash_stable_and_ordered():
    a = canonical.profile_hash({"threshold": 0.5, "model": "yunet"})
    b = canonical.profile_hash({"model": "yunet", "threshold": 0.5})
    assert a == b and len(a) == canonical.PROFILE_HASH_LEN


def test_profile_hash_changes_on_value():
    a = canonical.profile_hash({"threshold": 0.5})
    b = canonical.profile_hash({"threshold": 0.6})
    assert a != b


def test_sha256_helpers_agree():
    assert canonical.sha256_hex(b"abc") == canonical.sha256_text("abc")


# -- errors ------------------------------------------------------------------

def test_every_exit_code_has_http_status():
    for name in dir(errors):
        if name.startswith("EXIT_") and name not in ("EXIT_SUCCESS", "EXIT_GENERIC_FAILURE"):
            code = getattr(errors, name)
            assert code in errors.HTTP_FOR_EXIT, f"{name} missing from HTTP_FOR_EXIT"


def test_http_status_defaults_500():
    e = errors.BlurdError(999, "weird", "x")
    assert e.http_status == 500


def test_error_dict_shape():
    e = errors.NotFound("image", "abc123")
    d = e.to_dict()
    assert d["ok"] is False
    err = d["error"]
    assert err["code"] == errors.EXIT_RESOURCE_NOT_FOUND
    assert e.http_status == 404
    assert err["type"] == "resource_not_found"


def test_error_klasses_map_codes():
    cases = [
        (errors.ValidationError("x"), 422),
        (errors.AuthFailed(), 401),
        (errors.Conflict("x"), 409),
        (errors.Expired("blob", "x"), 410),
        (errors.Overloaded("x"), 503),
        (errors.RateLimited(), 429),
        (errors.StorageFull(), 507),
        (errors.Internal("x"), 500),
    ]
    for e, status in cases:
        assert e.http_status == status, (e.error_type, status)


def test_retry_after_serialised():
    e = errors.RateLimited(retry_after=42)
    assert e.to_dict()["error"]["retry_after"] == 42


# -- scope -------------------------------------------------------------------

def test_empty_scope_is_global():
    s = scope.Scope()
    assert s.is_global and s.tenant == scope.GLOBAL and not s


def test_scope_normalises_tags():
    s = scope.Scope({"tags": [" b ", "a", "a", ""]})
    assert s.tags == ["a", "b"]


def test_tenant_deterministic_regardless_of_order():
    a = scope.Scope({"tags": ["x", "y"], "metadata": {"k1": "1", "k2": "2"}})
    b = scope.Scope({"tags": ["y", "x"], "metadata": {"k2": "2", "k1": "1"}})
    assert a.tenant == b.tenant


def test_different_scopes_different_tenants():
    a = scope.Scope({"tags": ["a"]})
    b = scope.Scope({"tags": ["b"]})
    assert a.tenant != b.tenant


def test_stamp_adds_scope_labels():
    s = scope.Scope({"tags": ["t1"], "metadata": {"app": "x"}})
    tags, meta = s.stamp(["mine"], {"other": "1"})
    assert "t1" in tags and "mine" in tags
    assert meta == {"app": "x", "other": "1"}


def test_stamp_refuses_contradicting_metadata():
    s = scope.Scope({"metadata": {"app": "x"}})
    try:
        s.stamp([], {"app": "y"})
        assert False, "expected ScopeViolation"
    except scope.ScopeViolation as e:
        assert e.details["required"] == "x" and e.details["got"] == "y"


def test_global_scope_sql_is_noop():
    where, params = scope.Scope().sql()
    assert where == [] and params == []


def test_scope_sql_constrains_to_own_tenant():
    s = scope.Scope({"tags": ["a"], "metadata": {"k": "v"}})
    where, params = s.sql()
    assert len(where) == 2
    assert all("tenant=?" in w for w in where)
    assert s.tenant in params
    assert params == [s.tenant, "a", s.tenant, "k", "v"]


def test_parse_bad_json():
    try:
        scope.parse("{not json")
        assert False
    except errors.ValidationError:
        pass


def test_from_cli():
    s = scope.from_cli(["t"], ["a=1", "b = 2"])
    assert s.tags == ["t"] and s.metadata == {"a": "1", "b": "2"}


def test_from_cli_rejects_bad_meta():
    try:
        scope.from_cli([], ["noequals"])
        assert False
    except errors.ValidationError:
        pass
