"""Fetch scheduled-job passwords at runtime from OCI Vault; never persist them locally."""
from __future__ import annotations

import base64
import json
import os
import threading
import time


_CREDENTIAL_CACHE: dict[tuple[str, str], tuple[float, tuple[str, str]]] = {}
_CACHE_LOCK = threading.Lock()


def clear_credential_cache() -> None:
    """Discard process-memory credentials after a connection/Vault setting changes."""
    with _CACHE_LOCK:
        _CREDENTIAL_CACHE.clear()


def vault_credential(secret_ocid: str, configured_user: str) -> tuple[str, str]:
    """Return username/password from an OCI Vault secret JSON value.

    Expected secret plaintext: {"username":"archive_user", "password":"..."}.
    A plain secret value is accepted as the password when the username is configured separately.
    """
    if not secret_ocid:
        return configured_user, ""
    cache_key = (secret_ocid, configured_user)
    try:
        cache_seconds = max(0, int(os.environ.get("ERROR_ARCHIVER_VAULT_CACHE_SECONDS", "300")))
    except ValueError:
        cache_seconds = 300
    expires_at = time.monotonic() + cache_seconds
    with _CACHE_LOCK:
        cached = _CREDENTIAL_CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    try:
        import oci
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        bundle = oci.secrets.SecretsClient({}, signer=signer).get_secret_bundle(secret_ocid).data
        raw = base64.b64decode(bundle.secret_bundle_content.content).decode("utf-8")
    except Exception as exc:
        raise RuntimeError(f"Unable to retrieve OCI Vault credential secret using instance principal: {exc}") from exc
    try:
        value = json.loads(raw)
        credential = (str(value.get("username") or configured_user), str(value["password"]))
    except (json.JSONDecodeError, KeyError, TypeError):
        credential = (configured_user, raw)
    if expires_at > time.monotonic():
        with _CACHE_LOCK:
            _CREDENTIAL_CACHE[cache_key] = (expires_at, credential)
    return credential
