"""Validate settings restoration without accessing Vault or writing a database."""
import copy
import io
import json
import unittest
from unittest.mock import patch
from modules.settings_import import parse_settings, MAX_IMPORT_BYTES


class SettingsImportTests(unittest.TestCase):
    def setUp(self):
        self.settings = {'enabled': True, 'configured': True, 'schedule': '5min', 'source_connections': [{'name': 'source', 'host': 'source.example', 'secret_ocid': 'ocid1.vaultsecret.source'}], 'archive_connections': [{'name': 'archive', 'host': 'archive.example', 'secret_ocid': 'ocid1.vaultsecret.archive'}], 'source_tables': [{'name': 's', 'source': 'performance_schema.error_log', 'timestamp_column': 'LOGGED', 'connection': 'source'}], 'archive_tables': [{'name': 'a', 'archive_db': 'archivedb', 'archive_table': 'logs', 'connection': 'archive'}], 'source_mappings': [{'name': 'm', 'source_table': 's', 'archive_table_ref': 'a', 'enabled': 'true'}]}

    def parse(self, value):
        return parse_settings(io.BytesIO(json.dumps(value).encode()))

    def test_exported_mapping_settings_validate_without_vault(self):
        with patch('modules.config.vault_credential', side_effect=AssertionError('Unexpected Vault')):
            self.assertEqual(self.parse(self.settings), self.settings)

    def test_rejects_dangling_mapping_even_when_disabled(self):
        self.settings['source_mappings'][0].update(source_table='missing', enabled='false')
        with self.assertRaisesRegex(ValueError, 'existing source'):
            self.parse(self.settings)

    def test_rejects_credentials_worker_export_and_invalid_types(self):
        invalid = [[], {}, {'profiles': {}}, {'enabled': 'false'}, {'enabled': False, 'source_connections': {}}, {'enabled': False, 'archive_connections': [{'password': 'secret'}]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(value)

    def test_rejects_invalid_reference_and_vault_ocid(self):
        for field, value in [('connection', 'missing'), ('source', 'bad; DROP DATABASE x')]:
            candidate = copy.deepcopy(self.settings)
            candidate['source_tables'][0][field] = value
            with self.assertRaises(ValueError):
                self.parse(candidate)
        self.settings['source_connections'][0]['secret_ocid'] = 'ocid1.vault.example'
        with self.assertRaisesRegex(ValueError, 'Secret OCIDs'):
            self.parse(self.settings)

    def test_size_limit_and_invalid_json(self):
        for raw in (b'x' * (MAX_IMPORT_BYTES + 1), b'{broken', b'\xff'):
            with self.assertRaises(ValueError):
                parse_settings(io.BytesIO(raw))


if __name__ == '__main__':
    unittest.main()
