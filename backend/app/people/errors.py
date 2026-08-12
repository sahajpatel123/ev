"""Shared domain errors for the AGENT 7 ROSTER people subsystem."""

from __future__ import annotations


class FaceError(Exception):
    """Domain error with an HTTP-ish status and stable error code."""

    def __init__(self, message: str, *, status: int = 400, code: str = "face_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
