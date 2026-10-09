#!/usr/bin/env python3
"""Bootstrap a Compute against a new or existing archive control database."""
import argparse
import os
from pathlib import Path

from modules import control_store
from modules.control_migration import import_existing, retire_existing
from modules.profile_store import activate_control_profile, load_profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--schema', default='archive_control')
    parser.add_argument('--user', required=True)
    parser.add_argument('--secret-ocid', required=True)
    parser.add_argument('--import-directory', type=Path)
    args = parser.parse_args()
    path = control_store.profile_path()
    profile = load_profiles(path)[args.profile]
    profile.update(control_schema=args.schema, user=args.user, secret_ocid=args.secret_ocid)
    user, password = control_store.vault_credential(args.secret_ocid, args.user)
    control_store.validate_credentials(profile, create=True)
    token = control_store.bind(profile, user, password)
    try:
        if args.import_directory:
            import_existing(args.import_directory)
        else:
            control_store.load_settings()
        activate_control_profile(path, args.profile, profile)
        if args.import_directory:
            retire_existing(args.import_directory)
    finally:
        control_store.unbind(token)
    print('Control database configured. This Compute uses the shared schema:', args.schema)


if __name__ == '__main__':
    main()
