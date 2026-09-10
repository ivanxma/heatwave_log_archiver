"""Fetch scheduled-job passwords at runtime from OCI Vault; never persist them locally."""
from __future__ import annotations

import base64
import json


def vault_credential(secret_ocid: str, configured_user: str) -> tuple[str, str]:
    """Return username/password from an OCI Vault secret JSON value.

    Expected secret plaintext: {"username":"archive_user", "password":"..."}.
    A plain secret value is accepted as the password when the username is configured separately.
    """
    if not secret_ocid:
        return configured_user, ""
    try:
        import oci
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        bundle = oci.secrets.SecretsClient({}, signer=signer).get_secret_bundle(secret_ocid).data
        raw = base64.b64decode(bundle.secret_bundle_content.content).decode("utf-8")
    except Exception as exc:
        raise RuntimeError(f"Unable to retrieve OCI Vault credential secret using instance principal: {exc}") from exc
    try:
        value = json.loads(raw)
        return str(value.get("username") or configured_user), str(value["password"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return configured_user, raw
