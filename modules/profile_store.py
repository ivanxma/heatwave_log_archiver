"""Non-secret MySQL connection-profile storage."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SECRET_FIELDS = {"password", "ssh_password", "token", "private_key"}


def _clean(profile: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in profile.items() if key not in SECRET_FIELDS}


def ensure_profile_store(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_profiles(path, {"local-mysql": {"name": "local-mysql", "mode": "tcp", "host": "127.0.0.1", "port": 3306, "socket": "", "profile_management": True}})


def load_profiles(path: Path) -> dict[str, dict[str, object]]:
    ensure_profile_store(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): _clean(dict(value)) for name, value in payload.get("profiles", {}).items()}


def write_profiles(path: Path, profiles: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    payload = json.loads(path.read_text()) if path.exists() else {}
    payload['profiles'] = {name: _clean(value) for name, value in sorted(profiles.items())}
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def get_profile_by_name(profiles: dict[str, dict[str, object]], name: str) -> dict[str, object] | None:
    profile = profiles.get(name)
    return dict(profile) if profile else None


def save_profile_from_form(path: Path, form) -> str:
    name = str(form.get("profile_name", "")).strip()
    mode = str(form.get("mode", "tcp")).strip().lower()
    if not name or not all(c.isalnum() or c in "-_." for c in name):
        raise ValueError("Profile name may contain only letters, digits, dash, underscore, and dot.")
    if mode not in {"tcp", "socket"}:
        raise ValueError("Profile mode must be TCP or socket.")
    profile: dict[str, object] = {"name": name, "mode": mode, "profile_management": form.get("profile_management") == "on"}
    profile['control_schema'] = form.get('control_schema', '').strip()
    profile['user'] = form.get('control_user', '').strip()
    profile['secret_ocid'] = form.get('control_secret_ocid', '').strip()
    if profile['control_schema'] and not re.fullmatch(r'[A-Za-z0-9_$]+', profile['control_schema']):
        raise ValueError('Control schema must be a MySQL identifier.')
    if profile['control_schema'] and not profile['secret_ocid'].startswith('ocid1.vaultsecret.'):
        raise ValueError('An existing control schema requires a worker credential Secret OCID.')
    if mode == "socket":
        socket = str(form.get("socket", "")).strip()
        if not socket:
            raise ValueError("Socket path is required for a socket profile.")
        profile["socket"] = socket
        profile["host"] = ""
        profile["port"] = 3306
    else:
        host = str(form.get("host", "")).strip()
        try:
            port = int(form.get("port", 3306))
        except ValueError as exc:
            raise ValueError("Port must be numeric.") from exc
        if not host or not 1 <= port <= 65535:
            raise ValueError("TCP profile requires a host and port from 1 to 65535.")
        profile.update({"host": host, "port": port, "socket": ""})
    profiles = load_profiles(path)
    profiles[name] = profile
    write_profiles(path, profiles)
    return name


def activate_control_profile(path, name, profile):
    profiles = load_profiles(path)
    profiles[name] = profile
    write_profiles(path, profiles)
    payload = json.loads(path.read_text())
    payload['active_control_profile'] = name
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)
