"""Process-local authentication for the loopback-only desktop workbench."""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field


SESSION_COOKIE_NAME = "subtitle_local_session"
BOOTSTRAP_HEADER_NAME = "x-subtitle-bootstrap"
CSRF_HEADER_NAME = "x-subtitle-csrf"
LAUNCH_CHALLENGE_HEADER_NAME = "x-subtitle-launch-challenge"


def _new_secret() -> str:
    """Return at least 256 bits of URL-safe entropy without persisting it."""
    return secrets.token_urlsafe(32)


@dataclass
class LocalAccessState:
    """Hold one launcher session entirely in process memory.

    APP-AUTH-001: the bootstrap secret is single-use, the session cookie is
    HttpOnly, and the CSRF value is kept only in the workbench page's memory.
    """

    _session_token: str = field(default_factory=_new_secret)
    _bootstrap_token: str = field(default_factory=_new_secret)
    _csrf_token: str = field(default_factory=_new_secret)
    _launch_challenge: str = field(default_factory=_new_secret)
    _bootstrap_available: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def configure(
        self,
        *,
        session_token: str,
        bootstrap_token: str,
        csrf_token: str,
        launch_challenge: str,
    ) -> None:
        """Replace all secrets before a launcher starts accepting requests."""
        values = (session_token, bootstrap_token, csrf_token, launch_challenge)
        if any(len(value) < 32 for value in values):
            raise ValueError("Local access secrets must contain at least 32 characters")
        with self._lock:
            self._session_token = session_token
            self._bootstrap_token = bootstrap_token
            self._csrf_token = csrf_token
            self._launch_challenge = launch_challenge
            self._bootstrap_available = True

    def exchange_bootstrap(self, candidate: str) -> tuple[str, str] | None:
        """Consume the launcher bootstrap once and return cookie/CSRF values."""
        with self._lock:
            if not self._bootstrap_available or not secrets.compare_digest(
                str(candidate or ""), self._bootstrap_token
            ):
                return None
            self._bootstrap_available = False
            return self._session_token, self._csrf_token

    def valid_session(self, candidate: str) -> bool:
        return bool(candidate) and secrets.compare_digest(
            str(candidate), self._session_token
        )

    def valid_csrf(self, candidate: str) -> bool:
        return bool(candidate) and secrets.compare_digest(
            str(candidate), self._csrf_token
        )

    def valid_launch_challenge(self, candidate: str) -> bool:
        return bool(candidate) and secrets.compare_digest(
            str(candidate), self._launch_challenge
        )

    def csrf_for_session(self, candidate: str) -> str | None:
        return self._csrf_token if self.valid_session(candidate) else None


local_access = LocalAccessState()
