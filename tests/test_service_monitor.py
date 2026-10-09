"""Local monitoring is bounded, read-only and restricted to archiver units."""
import json
import subprocess
import unittest
from unittest.mock import patch
from modules import service_monitor as monitor


class MonitorTests(unittest.TestCase):
    def result(self, stdout='', stderr='', code=0):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_status_includes_worker_success_and_timer_waiting(self):
        output = '\n\n'.join('Id='+unit+'\nLoadState=loaded\nActiveState='+state+'\nSubState='+sub+'\nResult=success' for unit,state,sub in [('error-log-archiver.service','inactive','dead'),('error-log-archiver.timer','active','waiting'),('error-log-archiver-web.service','active','running')])
        with patch.object(monitor, '_run', return_value=self.result(output)) as run:
            rows = monitor.service_status()
        self.assertEqual(rows[0]['Result'], 'success')
        self.assertEqual(rows[1]['SubState'], 'waiting')
        self.assertEqual(run.call_args.args[0], 'systemctl')
        self.assertEqual(run.call_args.args[1][-3:], [v[1] for v in monitor.UNITS.values()])

    def test_journal_parses_systemd_events_redacts_and_orders_newest_first(self):
        rows = [dict(_SYSTEMD_UNIT='error-log-archiver.service', __REALTIME_TIMESTAMP='1000000', MESSAGE='password="sensitive" <script>known-password</script>', PRIORITY='3'), dict(UNIT='error-log-archiver.service', _SYSTEMD_UNIT='init.scope', __REALTIME_TIMESTAMP='2000000', MESSAGE='Started worker'), dict(_SYSTEMD_UNIT='unrelated.service', __REALTIME_TIMESTAMP='3000000', MESSAGE='secret')]
        output = '\n'.join(json.dumps(row) for row in rows)+'\ninvalid json'
        with patch.object(monitor, '_run', return_value=self.result(output)) as run:
            logs = monitor.journal_entries('worker', secrets=['known-password'])
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]['message'], 'Started worker')
        self.assertEqual(logs[1]['priority'], 'Error')
        self.assertNotIn('sensitive', logs[1]['message'])
        self.assertNotIn('known-password', logs[1]['message'])
        self.assertIn('--lines=100', run.call_args.args[1])
        self.assertEqual(run.call_args.args[1][-2:], ['--unit','error-log-archiver.service'])

    def test_untrusted_options_never_execute(self):
        with patch.object(monitor, '_run') as run:
            for unit,window,limit in [('ssh.service','24hours',100),('all','--boot',100),('all','24hours',10000)]:
                with self.assertRaises(ValueError):
                    monitor.journal_entries(unit,window,limit)
            run.assert_not_called()

    def test_missing_commands_timeouts_and_journal_permissions_are_explained(self):
        with patch.object(monitor.shutil,'which',return_value=None):
            with self.assertRaisesRegex(monitor.MonitorError,'Linux with systemd'):
                monitor.service_status()
        with patch.object(monitor.shutil,'which',return_value='/bin/systemctl'), patch.object(monitor.subprocess,'run',side_effect=subprocess.TimeoutExpired('systemctl',5)) as run:
            with self.assertRaisesRegex(monitor.MonitorError,'timed out'):
                monitor.service_status()
            self.assertNotIn('shell',run.call_args.kwargs)
            self.assertEqual(run.call_args.kwargs['timeout'],5)
        with patch.object(monitor,'_run',return_value=self.result(stderr='Permission denied',code=1)):
            with self.assertRaisesRegex(monitor.MonitorError,'systemd-journal'):
                monitor.journal_entries()
