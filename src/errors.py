"""Structured errors with semantic exit codes (cli-output-spec)."""

from typing import Any, Dict, List, Optional

EXIT_SUCCESS = 0
EXIT_GENERIC_FAILURE = 1
# 80-89 input/validation - do not retry, fix the input
EXIT_INVALID_ARGUMENT = 85
EXIT_BAD_PERMISSIONS = 86
EXIT_VALIDATION_ERROR = 87
# 90-99 resource/state
EXIT_RESOURCE_NOT_FOUND = 92
EXIT_RESOURCE_CONFLICT = 94
EXIT_RESOURCE_EXPIRED = 95
# 100-109 integration/external - transient, retry with backoff
EXIT_CONNECTION_TIMEOUT = 105
EXIT_API_UNAVAILABLE = 106
EXIT_AUTH_FAILED = 107
# Backpressure: the service is up and working, and is asking the caller to slow
# down. Deliberately NOT 106/502 -- 502 means "upstream returned garbage", and
# load balancers act on that: repeated 502s eject a backend, which is the
# opposite of what a queue shedding load wants. 503 + Retry-After is the signal
# every balancer and client library already understands.
EXIT_OVERLOADED = 108
# Per-IP rate limiting: unlike `overloaded` this is about the CALLER, not the
# queue -- so 429, which is the status every client already maps it to.
EXIT_RATE_LIMITED = 109
# 110 internal - report a bug
EXIT_INTERNAL_ERROR = 110
# 111+ capacity - the instance is full, not the caller's queue slice
EXIT_STORAGE_FULL = 111

# Exit code -> HTTP status, so the CLI and the API agree on what went wrong.
HTTP_FOR_EXIT = {
    EXIT_INVALID_ARGUMENT: 400,
    EXIT_VALIDATION_ERROR: 422,
    EXIT_BAD_PERMISSIONS: 403,
    EXIT_RESOURCE_NOT_FOUND: 404,
    EXIT_RESOURCE_CONFLICT: 409,
    EXIT_RESOURCE_EXPIRED: 410,
    EXIT_CONNECTION_TIMEOUT: 504,
    EXIT_API_UNAVAILABLE: 502,
    EXIT_AUTH_FAILED: 401,
    EXIT_OVERLOADED: 503,
    EXIT_RATE_LIMITED: 429,
    EXIT_INTERNAL_ERROR: 500,
    EXIT_STORAGE_FULL: 507,
}


class BlurdError(Exception):
    """Base error carrying everything an agent needs to decide what to do next."""

    def __init__(
        self,
        code: int,
        error_type: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        recoverable: bool = False,
        retry_after: Optional[int] = None,
        suggestions: Optional[List[str]] = None,
    ):
        self.code = code
        self.error_type = error_type
        self.message = message
        self.details = details or {}
        self.recoverable = recoverable
        self.retry_after = retry_after
        self.suggestions = suggestions or []
        super().__init__(message)

    @property
    def http_status(self) -> int:
        return HTTP_FOR_EXIT.get(self.code, 500)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "type": self.error_type,
                "message": self.message,
                "details": self.details,
                "recoverable": self.recoverable,
                "retry_after": self.retry_after,
                "suggestions": self.suggestions,
            },
        }


class InvalidArgument(BlurdError):
    def __init__(self, message, details=None, suggestions=None):
        super().__init__(
            EXIT_INVALID_ARGUMENT, "invalid_argument", message, details,
            suggestions=suggestions or ["Check command syntax", "Run: blurd guide"],
        )


class ValidationError(BlurdError):
    def __init__(self, message, details=None, suggestions=None):
        super().__init__(
            EXIT_VALIDATION_ERROR, "validation_error", message, details,
            suggestions=suggestions or ["Check the input image and parameters"],
        )


class NotFound(BlurdError):
    def __init__(self, resource_type, resource_id):
        super().__init__(
            EXIT_RESOURCE_NOT_FOUND, "resource_not_found",
            f"{resource_type} '{resource_id}' not found",
            {"resource_type": resource_type, "resource_id": resource_id},
            suggestions=[f"List existing {resource_type}s", "Check the identifier"],
        )


class Conflict(BlurdError):
    def __init__(self, message, details=None):
        super().__init__(EXIT_RESOURCE_CONFLICT, "resource_conflict", message, details)


class Expired(BlurdError):
    """The record exists but its bytes were pruned by TTL -- distinct from
    NotFound so a poller can tell "resubmit" from "wrong id"."""
    def __init__(self, resource_type, resource_id, details=None):
        super().__init__(
            EXIT_RESOURCE_EXPIRED, "resource_expired",
            f"{resource_type} '{resource_id}' has expired",
            {"resource_type": resource_type, "resource_id": resource_id,
             **(details or {})},
            recoverable=True,
            suggestions=["Resubmit the image to regenerate it"])


class AuthFailed(BlurdError):
    def __init__(self, message="Invalid or missing API key", details=None):
        super().__init__(
            EXIT_AUTH_FAILED, "auth_failed", message, details,
            suggestions=["Pass --api-key or set BLURD_API_KEY",
                         "Create a key with: blurd keys add <name>"],
        )


class Upstream(BlurdError):
    """Something outside the process failed: a fetch, the remote API."""

    def __init__(self, message, details=None, retry_after=2, code=EXIT_CONNECTION_TIMEOUT):
        super().__init__(
            code, "upstream_error", message, details,
            recoverable=True, retry_after=retry_after,
            suggestions=["Retry with backoff", "Check network connectivity"],
        )


class Overloaded(BlurdError):
    """The queue cannot take more work right now.

    Not a failure of the request: it is well-formed and would have succeeded a
    moment earlier or a moment later. The producer's correct response is to
    retry after `retry_after` seconds, so this carries that and is marked
    recoverable.
    """

    def __init__(self, message, details=None, retry_after=5):
        super().__init__(
            EXIT_OVERLOADED, "overloaded", message, details,
            recoverable=True, retry_after=retry_after,
            suggestions=[f"Retry after {retry_after}s with backoff",
                         "Sustained backpressure means the instance is at "
                         "capacity: add a replica, or slow the producer"],
        )


class StorageFull(BlurdError):
    """The blob store is at its configured `storage.max_bytes` cap.

    Recoverable: expired blobs are pruned before this is raised, so a retry
    succeeds once the TTL sweep or an admin frees space. Distinct from
    `overloaded` -- queue pressure is transient, a full disk is not.
    """
    def __init__(self, message="Blob storage cap reached", details=None,
                 retry_after=60):
        super().__init__(
            EXIT_STORAGE_FULL, "storage_full", message, details,
            recoverable=True, retry_after=retry_after,
            suggestions=[f"Retry after {retry_after}s -- TTL expiry may free "
                         "space", "Raise `storage.max_bytes` or free disk"],
        )


class RateLimited(BlurdError):
    """Too many requests from this address in the current window."""
    def __init__(self, message="Rate limit exceeded", details=None, retry_after=60):
        super().__init__(
            EXIT_RATE_LIMITED, "rate_limited", message, details,
            recoverable=True, retry_after=retry_after,
            suggestions=[f"Retry after {retry_after}s",
                         "Spread requests out or use a cached URL"],
        )


class Internal(BlurdError):
    def __init__(self, message, details=None):
        super().__init__(
            EXIT_INTERNAL_ERROR, "internal_error", message, details,
            suggestions=["Check the daemon log (blurd status)", "Re-run with --human for context"],
        )
