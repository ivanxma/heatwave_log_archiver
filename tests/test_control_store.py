"""Control bootstrap, isolation, lock lifecycle, and cancellation checks."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from modules import control_store
from modules.profile_store import activate_control_profile, write_profiles
from modules.control_migration import retire_existing


class ControlStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.json'
        self.profile = {'host': 'control.example', 'port': 3306, 'mode': 'tcp', 'control_schema': 'archive_control', 'user': 'worker', 'secret_ocid': 'ocid1.vaultsecret.example'}
        env = patch.dict(os.environ, ERROR_ARCHIVER_PROFILE_STORE=str(self.path))
        env.start()
        self.addCleanup(env.stop)
        self.token = control_store.bind(self.profile, 'login-user', 'login-password')
        self.addCleanup(control_store.unbind, self.token)

    def test_web_uses_session_credentials_without_vault(self):
        with patch('modules.control_store.vault_credential', side_effect=AssertionError('Unexpected Vault')):
            profile, user, password = control_store.target()
            self.assertEqual(user, 'login-user')
            self.assertEqual(password, 'login-password')
            self.assertEqual(profile['control_schema'], 'archive_control')

    def test_worker_reads_active_bootstrap_profile(self):
        activate_control_profile(self.path, 'control', self.profile)
        token = control_store._TARGET.set(None)
        try:
            with patch('modules.control_store.vault_credential', return_value=('worker', 'secret-password')) as vault:
                self.assertEqual(control_store.target()[1:], ('worker', 'secret-password'))
                vault.assert_called_once_with('ocid1.vaultsecret.example', 'worker')
        finally:
            control_store._TARGET.reset(token)

    def test_profile_updates_preserve_active_bootstrap(self):
        activate_control_profile(self.path, 'control', self.profile)
        write_profiles(self.path, {'control': self.profile, 'other': {'host': 'other'}})
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload['active_control_profile'], 'control')
        self.assertNotIn('password', self.path.read_text())

    def test_control_schema_identifier_is_escaped(self):
        token = control_store.bind({**self.profile, 'control_schema': 'bad; DROP DATABASE mysql'}, 'user', 'password')
        try:
            with self.assertRaises(ValueError):
                control_store.table('control_settings')
        finally:
            control_store.unbind(token)

    def test_lock_closes_connection_after_failure(self):
        conn = Mock()
        conn.cursor.return_value.fetchone.side_effect = [(1,), (321,)]
        with patch('modules.mysql_util._connection', return_value=conn), patch('modules.control_store.update_state', side_effect=lambda name, fn: fn({})):
            with self.assertRaisesRegex(RuntimeError, 'crashed'):
                with control_store.execution_lock() as acquired:
                    self.assertTrue(acquired)
                    raise RuntimeError('crashed')
        conn.close.assert_called_once()
        self.assertIsNone(control_store._EXECUTION.get())

    def test_busy_lock_cannot_be_bypassed(self):
        conn = Mock()
        conn.cursor.return_value.fetchone.return_value = (0,)
        with patch('modules.mysql_util._connection', return_value=conn), patch('modules.control_store.update_state') as update:
            with control_store.execution_lock() as acquired:
                self.assertFalse(acquired)
            update.assert_not_called()
        conn.close.assert_called_once()

    def test_cancel_only_matches_current_execution(self):
        token = control_store._EXECUTION.set('current')
        try:
            with patch('modules.control_store.load_state', return_value={'id': 'previous', 'cancel_requested': True}):
                with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                    control_store.check_cancelled()
            with patch('modules.control_store.load_state', return_value={'id': 'current', 'cancel_requested': True}):
                with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                    control_store.check_cancelled()
        finally:
            control_store._EXECUTION.reset(token)

    def test_override_rejects_stale_execution_id(self):
        with patch('modules.control_store.update_state', side_effect=lambda name, fn: fn({'id': 'new'})):
            with self.assertRaisesRegex(ValueError, 'changed'):
                control_store.request_cancel('old')

    def test_retirement_keeps_bootstrap_and_protected_backup(self):
        directory = Path(self.temp.name)
        for name in ('profiles.json', 'settings.json', 'job-state.json', 'worker-state.json', 'settings.before-destination.json'):
            (directory / name).write_text('{}')
        retire_existing(directory)
        self.assertEqual([p.name for p in directory.glob('*.json')], ['profiles.json'])
        self.assertEqual((directory / 'legacy-control-backup.tar.gz').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
