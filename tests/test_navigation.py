"""Navigation must stay independent of database and Vault availability."""
import json
import copy
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from modules.secret_provider import vault_credential


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings = Path(self.directory.name) / "settings.json"
        settings.write_text(json.dumps({
            "configured": True, "source_user": "source", "archive_user": "archive",
            "source_secret_ocid": "ocid1.vault.invalid", "archive_secret_ocid": "ocid1.vaultsecret.example",
        }))
        env = patch.dict(os.environ, {"ERROR_ARCHIVER_CONFIG_FILE": str(settings)})
        env.start()
        self.addCleanup(env.stop)
        self.settings = json.loads(settings.read_text())
        for name, value in [('load_settings', lambda: copy.deepcopy(self.settings)), ('save_settings', self.save), ('load_state', lambda name='job': {})]:
            mocked = patch('modules.control_store.' + name, side_effect=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        token = app.SERVER_SESSIONS.create("test", "test", "private-password", {"profile_management": True, "control_schema": "archive_control", "user": "worker", "secret_ocid": "ocid1.vaultsecret.control"})
        self.addCleanup(app.SERVER_SESSIONS.delete, token)
        app.SERVER_SESSIONS.get(token)["last_health_check"] = 0
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session.update(connection_id=token, session_scope="error-log-archiver", csrf_token="test-csrf")

    def save(self, settings, *, require_empty=False):
        if require_empty and self.settings:
            raise ValueError('Import requires an empty control schema')
        self.settings = copy.deepcopy(settings)

    def test_expired_health_result_does_not_block_menu_pages(self):
        with patch("app.test_mysql_connection", side_effect=AssertionError("Unexpected MySQL call")) as mysql, patch("modules.config.vault_credential", side_effect=AssertionError("Unexpected Vault call")) as vault:
            for url in ("/", "/configuration", "/archive-setup", "/configuration/source-connections/new"):
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn(b"private-password", response.data)
            mysql.assert_not_called()
            vault.assert_not_called()

    def test_form_action_still_checks_expired_health_result(self):
        with patch("app.test_mysql_connection", side_effect=RuntimeError("Database unavailable")) as mysql:
            response = self.client.post("/configuration", data={"csrf_token": "test-csrf"})
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/login"))
            mysql.assert_called_once()

    def test_archive_report_resolves_only_archive_credentials(self):
        with patch("modules.config.vault_credential", return_value=("archive", "password")) as vault, patch("app.list_partitions", return_value=[]):
            response = self.client.get("/?tab=partitions")
            self.assertEqual(response.status_code, 200)
            vault.assert_called_once_with("ocid1.vaultsecret.example", "archive")

    def test_vault_ocid_is_rejected_without_importing_oci(self):
        with patch.dict("sys.modules", {"oci": None}):
            with self.assertRaisesRegex(ValueError, "Secret OCID"):
                vault_credential("ocid1.vault.example", "source")

    def archive_form(self):
        return {"csrf_token": "test-csrf", "confirm_setup": "yes", "archive_host": "archive.example", "archive_port": "3306", "archive_user": "archive", "archive_socket": "", "archive_secret_ocid": "ocid1.vaultsecret.example", "archive_db": "archivedb", "archive_table": "log_archive"}

    def test_successful_setup_registers_and_displays_destination(self):
        with patch("modules.config.vault_credential", return_value=("archive", "private-password")), patch("app.ensure_schema") as ensure, patch("app.test_mysql_connection"):
            for _ in range(2):
                response = self.client.post("/archive-setup", data=self.archive_form())
                self.assertEqual(response.status_code, 302)
                self.assertIn("config_tab=archive-connections", response.location)
            self.assertEqual(ensure.call_count, 2)
        settings = app._settings()
        self.assertEqual(len(settings["archive_connections"]), 1)
        self.assertEqual(len(settings["archive_tables"]), 1)
        self.assertEqual(settings["archive_tables"][0]["connection"], settings["archive_connections"][0]["name"])
        with patch("modules.config.vault_credential", side_effect=AssertionError("Unexpected Vault call")):
            response = self.client.get("/archive-setup")
            self.assertNotIn(b"Archive setup is saved", response.data)
            self.assertIn(b'name="archive_host" value="archive.example"', response.data)
            self.assertIn(b'name="archive_port" value="3306"', response.data)
            self.assertIn(b'name="archive_db" value="archivedb"', response.data)
            self.assertIn(b'name="archive_table" value="log_archive"', response.data)
            response = self.client.get("/configuration?config_tab=archive-connections")
            self.assertIn(b'id="archive-connections-panel" >', response.data)
            self.assertIn(b"archive.example", response.data)
            self.assertNotIn(b"private-password", response.data)

    def test_failed_setup_keeps_previous_configuration(self):
        previous = app._settings()
        with patch("modules.config.vault_credential", return_value=("archive", "password")), patch("app.ensure_schema", side_effect=RuntimeError("Setup failed")), patch("app.test_mysql_connection"):
            response = self.client.post("/archive-setup", data=self.archive_form())
            self.assertEqual(response.status_code, 200)
        self.assertEqual(app._settings(), previous)

    def test_register_preserves_other_destinations(self):
        settings = {"archive_host": "archive.example", "archive_port": "3306", "archive_user": "archive", "archive_secret_ocid": "ocid1.vaultsecret.example", "archive_connections": [{"name": "archive-default", "host": "other.example"}], "archive_tables": [{"name": "archive-default-table", "connection": "archive-default", "archive_db": "other", "archive_table": "logs"}]}
        app.register_archive_destination(settings)
        app.register_archive_destination(settings)
        self.assertEqual(len(settings["archive_connections"]), 2)
        self.assertEqual(settings["archive_connections"][0]["host"], "other.example")
        self.assertEqual(settings["archive_tables"][1]["connection"], "archive-default-2")

    def test_initial_archive_setup_does_not_require_source_secret(self):
        self.settings = {}
        with patch('modules.config.vault_credential', return_value=('archive', 'password')) as vault, patch('app.ensure_schema'), patch('app.test_mysql_connection'):
            response = self.client.post('/initial-setup', data=self.archive_form())
            self.assertEqual(response.status_code, 302)
            self.assertFalse(self.settings['enabled'])
            self.assertNotIn('source_secret_ocid', self.settings)
            vault.assert_called_once_with('ocid1.vaultsecret.example', 'archive')

    def test_banner_uses_mysql_log_archiver(self):
        response = self.client.get('/configuration')
        self.assertIn(b'MySQL Log Archiver', response.data)
        self.assertNotIn(b'Error Log Archiver', response.data)
        self.assertNotIn(b'>EA<', response.data)

    def test_control_setup_uses_selected_schema(self):
        with patch('app.test_mysql_connection'), patch('modules.control_store.validate_credentials', return_value=True) as validate, patch('app.activate_control_profile') as activate:
            response = self.client.post('/control-setup', data={'csrf_token': 'test-csrf', 'control_schema': 'archive_control', 'control_user': 'worker', 'control_secret_ocid': 'ocid1.vaultsecret.example'})
            self.assertEqual(response.status_code, 302)
            self.assertTrue(validate.call_args.kwargs['create'])
            self.assertEqual(activate.call_args.args[2]['control_schema'], 'archive_control')

    def test_control_secret_test_does_not_save_or_initialize(self):
        with patch('app.test_mysql_connection'), patch('modules.control_store.validate_credentials', return_value=True) as validate, patch('app.activate_control_profile') as activate:
            response = self.client.post('/control-setup', data={'csrf_token': 'test-csrf', 'control_schema': 'archive_control', 'control_user': 'worker', 'control_secret_ocid': 'ocid1.vaultsecret.example', 'control_action': 'test'})
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Secret validated', response.data)
            self.assertEqual(validate.call_args.kwargs, {})
            activate.assert_not_called()

    def test_invalid_control_secret_does_not_activate(self):
        with patch('app.test_mysql_connection'), patch('modules.control_store.validate_credentials', side_effect=RuntimeError('Secret access denied')), patch('app.activate_control_profile') as activate:
            response = self.client.post('/control-setup', data={'csrf_token': 'test-csrf', 'control_schema': 'archive_control', 'control_user': 'worker', 'control_secret_ocid': 'ocid1.vaultsecret.example', 'control_action': 'test'})
            self.assertIn(b'Secret access denied', response.data)
            activate.assert_not_called()

    def test_exports_settings_and_worker_bootstrap_without_passwords(self):
        self.settings['source_connections'] = [{'name': 'source', 'password': 'must-not-export', 'secret_ocid': 'ocid1.vaultsecret.example'}]
        response = self.client.get('/job-settings.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['source_connections'][0]['name'], 'source')
        self.assertNotIn(b'must-not-export', response.data)
        response = self.client.get('/control-profile.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['active_control_profile'], 'test')
        self.assertEqual(response.json['profiles']['test']['secret_ocid'], 'ocid1.vaultsecret.control')
        self.assertNotIn(b'private-password', response.data)

    def test_exports_require_login(self):
        with app.app.test_client() as client:
            for url in ('/job-settings.json', '/control-profile.json'):
                self.assertEqual(client.get(url).status_code, 302)

    def test_override_route_requests_cancel_without_bypassing_lock(self):
        with patch('app.test_mysql_connection'), patch('modules.control_store.request_cancel') as cancel:
            response = self.client.post('/execution/override', data={'csrf_token': 'test-csrf', 'execution_id': 'current'})
            self.assertEqual(response.status_code, 302)
            cancel.assert_called_once_with('current')

    def test_import_restores_export_into_fresh_control_schema(self):
        exported = self.client.get('/job-settings.json').json
        exported.update(enabled=False, source_secret_ocid='ocid1.vaultsecret.source')
        self.settings = {}
        from contextlib import nullcontext
        with patch('app.test_mysql_connection'), patch('app.archive_execution_lock', return_value=nullcontext(True)), patch('modules.config.vault_credential', side_effect=AssertionError('Unexpected Vault')):
            response = self.client.post('/job-settings/import', data={'csrf_token': 'test-csrf', 'confirm_import': 'yes', 'settings_file': (io.BytesIO(json.dumps(exported).encode()), 'job-settings.json')}, follow_redirects=True)
            self.assertIn(b'Job settings imported', response.data)
            self.assertEqual(self.client.get('/job-settings.json').json, exported)

    def test_import_rejects_existing_configuration(self):
        before = copy.deepcopy(self.settings)
        from contextlib import nullcontext
        with patch('app.test_mysql_connection'), patch('app.archive_execution_lock', return_value=nullcontext(True)):
            response = self.client.post('/job-settings/import', data={'csrf_token': 'test-csrf', 'confirm_import': 'yes', 'settings_file': (io.BytesIO(b'{"enabled":false}'), 'job-settings.json')}, follow_redirects=True)
            self.assertIn(b'empty control schema', response.data)
        self.assertEqual(self.settings, before)


if __name__ == "__main__":
    unittest.main()
