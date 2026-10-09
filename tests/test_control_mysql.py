"""Opt-in tests against a disposable schema on a supplied control MySQL server."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

from modules import control_store
from modules.control_migration import import_existing
from modules.mysql_util import _connection, ident


@unittest.skipUnless(os.environ.get('ERROR_ARCHIVER_CONTROL_INTEGRATION') == '1', 'requires a MySQL control test connection')
class ControlMySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original, user, password = control_store.target()
        cls.schema = 'archiver_test_' + uuid.uuid4().hex[:16]
        cls.profile = {**original, 'control_schema': cls.schema}
        cls.user, cls.password = user, password
        cls.token = control_store.bind(cls.profile, user, password)
        try:
            control_store.initialize()
        except Exception:
            control_store.unbind(cls.token)
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            assert cls.schema.startswith('archiver_test_')
            conn = _connection(cls.profile.get('host', '127.0.0.1'), int(cls.profile.get('port', 3306)), cls.user, cls.password, cls.profile.get('socket', '') if cls.profile.get('mode') == 'socket' else '')
            try:
                cursor = conn.cursor()
                cursor.execute(f'DROP DATABASE {ident(cls.schema)}')
                cursor.close()
            finally:
                conn.close()
        finally:
            control_store.unbind(cls.token)

    def setUp(self):
        control_store.save_settings({})
        with control_store.connection() as conn:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {control_store.table('control_state')}")
            conn.commit()
            cur.close()

    def test_roundtrip_and_password_exclusion(self):
        settings = {'enabled': False, 'batch_size': 2, 'archive_connections': [{'name': 'a', 'host': 'archive.example', 'secret_ocid': 'ocid1.vaultsecret.example', 'password': 'must-not-persist'}]}
        control_store.save_settings(settings)
        expected = copy.deepcopy(settings)
        expected['archive_connections'][0].pop('password')
        self.assertEqual(control_store.load_settings(), expected)

    def test_concurrent_state_updates_do_not_lose_events(self):
        def add(i):
            # Separate processes model separate Compute apps, avoiding the
            # single-process connection cache's serialization.
            program = "import json,sys; from modules import control_store as s; p=json.load(sys.stdin); t=s.bind(p['profile'],p['user'],p['password']); s.update_state('test',lambda state: {'events':[*state.get('events',[]),p['i']]})"
            data = json.dumps({'profile': self.profile, 'user': self.user, 'password': self.password, 'i': i})
            result = subprocess.run([sys.executable, '-c', program], input=data, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(copy_context().run, add, i) for i in range(12)]
            for future in futures:
                future.result()
        self.assertEqual(sorted(control_store.load_state('test')['events']), list(range(12)))

    def test_second_connection_excludes_lock_and_crash_releases_it(self):
        with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
            with control_store.execution_lock() as acquired:
                self.assertTrue(acquired)
                with control_store.execution_lock() as other:
                    self.assertFalse(other)
                program = "import json,sys; from modules import control_store as s; p=json.load(sys.stdin); t=s.bind(p['profile'],p['user'],p['password']); assert s.load_settings()=={'shared':True};\nwith s.execution_lock() as acquired: assert not acquired"
                control_store.save_settings({'shared': True})
                result = subprocess.run([sys.executable, '-c', program], input=json.dumps({'profile': self.profile, 'user': self.user, 'password': self.password}), text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                raise RuntimeError('simulated crash')
        with control_store.execution_lock() as acquired:
            self.assertTrue(acquired)

    def test_override_cancels_owner_and_then_lock_can_be_acquired(self):
        with control_store.execution_lock() as acquired:
            self.assertTrue(acquired)
            state = control_store.load_state('execution')
            control_store.request_cancel(state['id'])
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                control_store.check_cancelled()
        with control_store.execution_lock() as acquired:
            self.assertTrue(acquired)
            control_store.check_cancelled()

    def test_legacy_import_verified_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = {'configured': True, 'source_connections': [], 'archive_connections': [{'name': 'archive', 'host': 'archive.example'}]}
            state = {'source_cursors': {'custom:e1': '2026-10-09 09:00:00'}, 'history': []}
            for name, value in [('settings.json', settings), ('job-state.json', state), ('worker-state.json', {'last_success': '2026-10-09T09:00:00+00:00'})]:
                Path(directory, name).write_text(json.dumps(value))
            import_existing(directory)
            self.assertEqual(control_store.load_settings()['archive_connections'], settings['archive_connections'])
            self.assertEqual(control_store.load_state(), state)
            with self.assertRaisesRegex(ValueError, 'already contains'):
                import_existing(directory)

    def test_control_credential_validation_checks_real_mysql(self):
        self.assertTrue(control_store.validate_credentials(self.profile))
        self.assertEqual(control_store.load_state('execution'), {})

    def test_setup_ui_validates_secret_and_exports_shared_settings(self):
        import app
        profile = {**self.profile, 'profile_management': True}
        token = app.SERVER_SESSIONS.create('compute-control-test', self.user, self.password, profile)
        try:
            control_store.save_settings({'enabled': False, 'batch_size': 2, 'archive_connections': [{'name': 'a', 'host': 'archive.example'}]})
            with app.app.test_client() as client:
                with client.session_transaction() as session:
                    session.update(connection_id=token, session_scope='error-log-archiver', csrf_token='test-csrf')
                response = client.post('/control-setup', data={'csrf_token': 'test-csrf', 'control_schema': self.schema, 'control_user': self.user, 'control_secret_ocid': self.profile['secret_ocid'], 'control_action': 'test'})
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'Secret validated', response.data)
                response = client.get('/job-settings.json')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['batch_size'], 2)
                response = client.get('/control-profile.json')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['profiles']['compute-control-test']['control_schema'], self.schema)
                self.assertNotIn('password', response.json['profiles']['compute-control-test'])
        finally:
            app.SERVER_SESSIONS.delete(token)


if __name__ == '__main__':
    unittest.main()
