"""Server-owned ephemeral credentials keyed by opaque browser session IDs."""
from __future__ import annotations

import secrets
import time


class ServerSessionStore:
    def __init__(self, ttl_seconds: int = 3600):
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, dict[str, object]] = {}

    def create(self, profile_name: str, username: str, password: str, profile: dict[str, object]) -> str:
        token = secrets.token_urlsafe(32)
        self._records[token] = {"profile_name": profile_name, "username": username, "password": password, "profile": dict(profile), "expires_at": time.monotonic() + self.ttl_seconds}
        return token

    def get(self, token: str | None) -> dict[str, object] | None:
        record = self._records.get(token or "")
        if not record or float(record["expires_at"]) < time.monotonic():
            self.delete(token)
            return None
        return record

    def delete(self, token: str | None) -> None:
        if token:
            self._records.pop(token, None)

    @staticmethod
    def public_context(record: dict[str, object] | None) -> dict[str, str]:
        if not record:
            return {"current_username": "", "current_profile_name": "", "connection_summary": ""}
        profile = record["profile"]
        if profile.get("mode") == "socket":
            summary = "MySQL Unix socket"
        else:
            summary = f"{profile.get('host', '127.0.0.1')}:{profile.get('port', 3306)}"
        return {"current_username": str(record["username"]), "current_profile_name": str(record["profile_name"]), "connection_summary": summary}
