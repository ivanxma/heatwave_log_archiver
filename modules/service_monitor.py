"""Read-only local systemd status and bounded, redacted archiver journal access."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess

UNITS = {
    'worker': ('Archive worker', 'error-log-archiver.service'),
    'timer': ('Schedule timer', 'error-log-archiver.timer'),
    'web': ('Web console', 'error-log-archiver-web.service'),
}
WINDOWS = {'15min': '15 minutes ago', '1hour': '1 hour ago', '24hours': '24 hours ago', '7days': '7 days ago'}
PRIORITIES = {'0':'Emergency', '1':'Alert', '2':'Critical', '3':'Error', '4':'Warning', '5':'Notice', '6':'Info', '7':'Debug'}
PROPERTIES = ('Id', 'Description', 'LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'Result', 'ExecMainStatus', 'MainPID', 'ActiveEnterTimestamp', 'InactiveEnterTimestamp', 'LastTriggerUSec')


class MonitorError(RuntimeError):
    pass


def redact(value, secrets=()):
    text = str(value)
    for secret in sorted({str(item) for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, '[redacted]')
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[private key redacted]', text, flags=re.S)
    text = re.sub(r'(?i)(\bBearer\s+)\S+', r'\1[redacted]', text)
    text = re.sub(r'''(?ix)(["']?(?:password|passwd|access_token|refresh_token|authorization|private_key)["']?\s*[:=]\s*)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,;}]+)''', r'\1[redacted]', text)
    return text


def _run(binary, arguments):
    executable = shutil.which(binary)
    if not executable:
        raise MonitorError('Service monitoring requires Linux with systemd; this command is unavailable on this host.')
    try:
        return subprocess.run([executable, *arguments], capture_output=True, text=True, timeout=5, env={'PATH':os.defpath, 'LC_ALL':'C', 'SYSTEMD_COLORS':'0'})
    except subprocess.TimeoutExpired:
        raise MonitorError('The service query timed out. Refresh to try again.') from None
    except OSError:
        raise MonitorError('The service query could not run on this host.') from None


def service_status():
    result = _run('systemctl', ['show', '--no-pager', '--property=' + ','.join(PROPERTIES), *(unit for _, unit in UNITS.values())])
    if result.returncode and not result.stdout.strip():
        raise MonitorError('Unable to read systemd service status on this host.')
    found = {}
    for block in result.stdout.strip().split('\n\n'):
        fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if fields.get('Id') in {unit for _, unit in UNITS.values()}:
            found[fields['Id']] = fields
    if not found:
        raise MonitorError('Systemd did not return archiver service status on this host.')
    return [{'key':key, 'label':label, 'unit':unit, **found.get(unit, {'LoadState':'not-found','ActiveState':'unknown','SubState':'unknown','UnitFileState':'unknown'})} for key, (label, unit) in UNITS.items()]


def journal_entries(unit='all', window='24hours', limit=100, secrets=()):
    if unit not in {'all', *UNITS} or window not in WINDOWS or limit not in {50,100,250,500}:
        raise ValueError('Select an archiver service, time window and valid entry limit.')
    units = [value[1] for value in UNITS.values()] if unit == 'all' else [UNITS[unit][1]]
    args = ['--no-pager', '--quiet', '--output=json', '--lines=' + str(limit), '--since=' + WINDOWS[window]]
    for name in units:
        args.extend(['--unit', name])
    result = _run('journalctl', args)
    if result.returncode or 'not seeing messages' in result.stderr.lower() or 'permission' in result.stderr.lower():
        raise MonitorError('Journal access is unavailable. Install the updated web service unit with its systemd-journal supplementary group, then restart the web service.')
    if len(result.stdout) > 2 * 1024 * 1024:
        raise MonitorError('Journal output is too large. Select fewer entries or a shorter time window.')
    entries = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                continue
            source = next((item.get(field) for field in ('UNIT', 'OBJECT_SYSTEMD_UNIT', '_SYSTEMD_UNIT') if item.get(field) in units), '')
            if source not in units:
                continue
            timestamp = datetime.fromtimestamp(int(item['__REALTIME_TIMESTAMP']) / 1_000_000, timezone.utc).isoformat(timespec='seconds')
            message = item.get('MESSAGE', '')
            if isinstance(message, list):
                message = bytes(message).decode('utf-8', errors='replace') if all(isinstance(part, int) and 0 <= part <= 255 for part in message) else str(message)
            entries.append({'time':timestamp, 'unit':source, 'priority':PRIORITIES.get(str(item.get('PRIORITY','6')), 'Info'), 'message':redact(message, secrets)[:8000]})
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            continue
    return list(reversed(entries[-limit:]))
