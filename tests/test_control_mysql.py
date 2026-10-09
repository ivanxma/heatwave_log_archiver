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

    def test_ui_import_into_fresh_database_roundtrips_and_rejects_overwrite(self):
        import app
        import io
        token = app.SERVER_SESSIONS.create('compute-control-test', self.user, self.password, {**self.profile, 'profile_management': True})
        try:
            snapshot = {'enabled': False, 'configured': True, 'source_connections': [{'name': 'source', 'host': 'source.example', 'secret_ocid': 'ocid1.vaultsecret.source'}], 'archive_connections': [{'name': 'archive', 'host': 'archive.example', 'secret_ocid': 'ocid1.vaultsecret.archive'}], 'source_tables': [{'name': 's', 'connection': 'source', 'source': 'performance_schema.error_log', 'timestamp_column': 'LOGGED'}], 'archive_tables': [{'name': 'a', 'connection': 'archive', 'archive_db': 'archivedb', 'archive_table': 'logs'}], 'source_mappings': [{'name': 'm', 'source_table': 's', 'archive_table_ref': 'a', 'enabled': 'true'}]}
            with app.app.test_client() as client:
                with client.session_transaction() as session:
                    session.update(connection_id=token, session_scope='error-log-archiver', csrf_token='test-csrf')
                for expected in (b'Job settings imported', b'empty control schema'):
                    response = client.post('/job-settings/import', data={'csrf_token': 'test-csrf', 'confirm_import': 'yes', 'settings_file': (io.BytesIO(json.dumps(snapshot).encode()), 'job-settings.json')}, follow_redirects=True)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(expected, response.data)
                    self.assertEqual(client.get('/job-settings.json').json, snapshot)
        finally:
            app.SERVER_SESSIONS.delete(token)

    def test_log_explorer_real_json_sort_search_and_time_intervals(self):
        from unittest.mock import patch
        import app
        with control_store.connection() as conn:
            cur = conn.cursor()
            cur.execute(f"CREATE TABLE {ident(self.schema)}.explore_logs (id INT PRIMARY KEY, event_time DATETIME, payload JSON, source_server_uuid CHAR(36))")
            cur.executemany(f"INSERT INTO {ident(self.schema)}.explore_logs VALUES (%s,%s,%s,%s)", [(1, '2026-10-08 10:00:00', '{"code":2,"message":"first"}', '11111111-1111-1111-1111-111111111111'), (2, '2026-10-09 11:00:00', '{"code":10,"message":"second"}', '22222222-2222-2222-2222-222222222222'), (3, '2026-10-09 12:00:00', '{"code":3,"message":"third"}', None)])
            conn.commit()
            cur.close()
        settings = {'source_connections': [{'name': 'cluster', 'server_uuid':'22222222-2222-2222-2222-222222222222', 'server_uuids':['11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222']}], 'archive_connections': [{'name': 'a'}], 'archive_tables': [{'name': 't', 'connection': 'a', 'archive_db': self.schema, 'archive_table': 'explore_logs'}]}
        token = app.SERVER_SESSIONS.create('explore-test', self.user, self.password, self.profile)
        try:
            with patch('modules.log_explorer.connect', side_effect=lambda _: control_store.connection()), patch('modules.log_explorer.control_store.load_settings', return_value=settings), app.app.test_client() as client:
                with client.session_transaction() as session:
                    session.update(connection_id=token, session_scope='error-log-archiver')
                response = client.get('/log-explore', query_string={'connection': 'a', 'table': 't', 'sort': '["payload", "code"]', 'direction': 'asc', 'download': 'csv'})
                self.assertEqual(response.status_code, 200)
                import csv, io
                rows = list(csv.DictReader(io.StringIO(response.data.decode())))
                self.assertEqual([row['payload.code'] for row in rows], ['2', '3', '10'])
                response = client.get('/log-explore', query_string={'connection':'a','table':'t','rule_field':'["payload","message"]','rule_op':'eq','rule_value':'second','download':'csv'})
                self.assertEqual(len(list(csv.DictReader(io.StringIO(response.data.decode())))), 1)
                response = client.get('/log-explore', query_string=[('connection','a'),('table','t'),('rule_field','__source_connection__'),('rule_op','eq'),('rule_value','["cluster"]'),('rule_field','["payload","code"]'),('rule_op','gte'),('rule_value','3'),('download','csv')])
                rows = list(csv.DictReader(io.StringIO(response.data.decode())))
                self.assertEqual([row['id'] for row in rows], ['2'])
                response = client.get('/log-explore', query_string=[('connection','a'),('table','t'),('rule_field','__source_connection__'),('rule_op','eq'),('rule_value','["__unknown__"]'),('download','csv')])
                self.assertEqual(len(list(csv.DictReader(io.StringIO(response.data.decode())))), 1)
                response = client.get('/log-explore', query_string=[('connection','a'),('table','t'),('rule_mode','any'),('rule_field','["payload","code"]'),('rule_op','eq'),('rule_value','2'),('rule_field','["payload","code"]'),('rule_op','eq'),('rule_value','10'),('download','csv')])
                self.assertEqual(len(list(csv.DictReader(io.StringIO(response.data.decode())))), 2)
                response = client.get('/log-explore', query_string=[('connection','a'),('table','t'),('view','chart'),('interval','day'),('start','2026-10-08'),('end','2026-10-10'),('rule_field','__source_connection__'),('rule_op','eq'),('rule_value','["cluster"]'),('rule_field','["payload","message"]'),('rule_op','contains'),('rule_value','second'),('download','csv')])
                rows = list(csv.DictReader(io.StringIO(response.data.decode())))
                self.assertEqual(sum(int(row['Records']) for row in rows), 1)
                response = client.get('/log-explore', query_string=[('connection','a'),('table','t'),('rule_field','__source_connection__'),('rule_op','eq'),('rule_value','["cluster"]'),('download','csv')])
                self.assertEqual(len(list(csv.DictReader(io.StringIO(response.data.decode())))), 2)
                response = client.get('/log-explore', query_string=[('connection','a'),('table','t'),('rule_mode','all'),('rule_field','__source_connection__'),('rule_op','eq'),('rule_value','["cluster"]'),('rule_field','["payload","code"]'),('rule_op','gte'),('rule_value','3'),('download','csv')])
                self.assertEqual(len(list(csv.DictReader(io.StringIO(response.data.decode())))), 1)
                response = client.get('/log-explore', query_string=[('connection','a'),('table','t'),('rule_field','__source_connection__'),('rule_op','eq'),('rule_value','[]'),('download','csv')])
                self.assertEqual(len(list(csv.DictReader(io.StringIO(response.data.decode())))), 3)
                for interval in ('hour', 'day', 'week', 'month'):
                    response = client.get(f'/log-explore?connection=a&table=t&view=chart&interval={interval}&start=2026-10-08&end=2026-10-10&download=csv')
                    self.assertEqual(response.status_code, 200)
                    rows = list(csv.DictReader(io.StringIO(response.data.decode())))
                    self.assertEqual(sum(int(row['Records']) for row in rows), 3)
        finally:
            app.SERVER_SESSIONS.delete(token)

    def test_archive_uuid_migration_preserves_fingerprints_and_registers_source(self):
        from contextlib import contextmanager
        from dataclasses import replace
        from datetime import datetime, timezone, timedelta
        from hashlib import sha256
        from unittest.mock import patch
        from modules.config import ArchiveConfig
        from modules.archive_service import archive_error_log
        from modules.source_identity import read_server_uuid
        target = f"{ident(self.schema)}.uuid_archive"
        with control_store.connection() as conn:
            cur = conn.cursor()
            cur.execute(f"CREATE TABLE {target} (event_time DATETIME(6) NOT NULL, log_type VARCHAR(255) NOT NULL, payload JSON NOT NULL, archived_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP(6), source_fingerprint BINARY(32) NOT NULL, PRIMARY KEY(event_time,source_fingerprint)) PARTITION BY RANGE COLUMNS(event_time) (PARTITION p_bootstrap VALUES LESS THAN ('2000-01-01'))")
            cur.execute(f"INSERT INTO {target}(event_time,log_type,payload,source_fingerprint) VALUES ('1999-01-01','legacy','{{}}',UNHEX(%s))", ('ab'*32,))
            cur.execute(f"CREATE TABLE {ident(self.schema)}.uuid_source (logged DATETIME(6), message VARCHAR(50))")
            when = datetime.now(timezone.utc).replace(tzinfo=None)
            cur.execute(f"INSERT INTO {ident(self.schema)}.uuid_source VALUES (%s, 'hello')", (when,))
            conn.commit()
            cur.close()
        source_profile = {'name': 'test-source', 'host': self.profile.get('host', ''), 'port': self.profile.get('port', 3306), 'socket': self.profile.get('socket', '') if self.profile.get('mode') == 'socket' else ''}
        control_store.save_settings({'source_connections': [source_profile]})
        config = ArchiveConfig.from_env(False, False, settings={'archive_db': self.schema, 'archive_table': 'uuid_archive'})
        config = replace(config, source_host=source_profile['host'], source_port=int(source_profile['port']), source_socket=source_profile['socket'], source_connection_name='test-source', source_user=self.user, source_password=self.password, archive_host=source_profile['host'], archive_port=int(source_profile['port']), archive_socket=source_profile['socket'], archive_user=self.user, archive_password=self.password, log_types=(), custom_sources=({'name':'sample','source':self.schema+'.uuid_source','timestamp_column':'logged','cursor_key':'custom:sample'},))
        @contextmanager
        def fresh_source(_):
            conn = _connection(config.source_host, config.source_port, self.user, self.password, config.source_socket)
            try:
                yield conn
            finally:
                conn.close()
        with fresh_source(config) as conn:
            actual_uuid = read_server_uuid(conn)
        with control_store.execution_lock() as acquired:
            self.assertTrue(acquired)
            copied, cursors = archive_error_log(config)
            self.assertEqual(copied, 1)
            self.assertEqual(archive_error_log(config)[0], 0)
        self.assertEqual(control_store.load_settings()['source_connections'][0]['server_uuid'], actual_uuid)
        with control_store.connection() as conn:
            cur = conn.cursor(dictionary=True)
            cur.execute(f"SELECT source_server_uuid,source_connection,source_table,HEX(source_fingerprint) AS fingerprint FROM {target} ORDER BY event_time")
            rows=cur.fetchall()
            cur.close()
        self.assertIsNone(rows[0]['source_server_uuid'])
        self.assertEqual(rows[0]['fingerprint'].lower(), 'ab'*32)
        self.assertEqual(rows[1]['source_server_uuid'], actual_uuid)
        self.assertEqual(rows[1]['source_connection'], 'test-source')
        self.assertEqual(rows[1]['source_table'], self.schema+'.uuid_source')
        payload = json.dumps({'message':'hello'}, default=str, sort_keys=True)
        self.assertEqual(rows[1]['fingerprint'].lower(), sha256(f'custom:sample|{when}|{payload}'.encode()).hexdigest())

        # A cluster failover changes the observed instance without rejecting or
        # replacing the logical connection's existing checkpoint namespace.
        next_uuid = '33333333-3333-3333-3333-333333333333'
        with control_store.connection() as conn:
            cur = conn.cursor()
            cur.execute(f"INSERT INTO {ident(self.schema)}.uuid_source VALUES (%s, 'after failover')", (when + timedelta(seconds=1),))
            conn.commit()
            cur.close()
        with control_store.execution_lock(), patch('modules.archive_service.read_source_identity', return_value=(next_uuid, 'second-instance')):
            self.assertEqual(archive_error_log(config)[0], 1)
        profile = control_store.load_settings()['source_connections'][0]
        self.assertEqual(profile['server_uuids'], [actual_uuid, next_uuid])
        self.assertEqual(profile['server_uuid'], next_uuid)
        self.assertEqual(profile['server_hostnames'][next_uuid], 'second-instance')
        with control_store.connection() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT source_server_uuid,source_hostname FROM {target} ORDER BY event_time DESC LIMIT 1")
            self.assertEqual(cur.fetchone(), (next_uuid, 'second-instance'))
            cur.close()



if __name__ == '__main__':
    unittest.main()
