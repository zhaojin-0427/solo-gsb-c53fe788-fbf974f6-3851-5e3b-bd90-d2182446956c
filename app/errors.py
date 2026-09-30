"""Stable domain errors.

Every rejected request gets ``422 UNPROCESSABLE ENTITY`` with an envelope::

    {"error": {"code": "VALIDATION_FAILED", "message": ...,
               "issues": [{"code": ..., "path": ..., "message": ...}, ...]}}

Issues are sorted by (path, code), so clients can rely on deterministic
error details.
"""

from __future__ import annotations

from typing import List, Dict, Any


class AccountingError(Exception):
    def __init__(self, issues: List[Dict[str, Any]],
                 code: str = "VALIDATION_FAILED",
                 message: str = "核算请求校验失败"):
        self.issues = sorted(issues, key=lambda it: (it["path"], it["code"]))
        self.code = code
        self.message = message
        super().__init__(message)


class NotFoundError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class ConflictError(Exception):
    def __init__(self, code: str, message: str, details: Dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)
