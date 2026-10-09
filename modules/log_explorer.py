"""Read-only, bounded archive browsing and timestamp aggregation."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager

from flask import Blueprint, Response, flash, request, url_for
from . import control_store
from .mysql_util import _cached_connection, ident
from .secret_provider import vault_credential
from .explorer_filters import OPERATORS, parse_rules, compile_filters


def flatten(row, json_columns):
    result = {}
    def visit(path, value):
        if isinstance(value, dict) and value:
            for key, child in value.items():
                visit(path + (str(key),), child)
        elif isinstance(value, list) and value:
            for index, child in enumerate(value):
                visit(path + (index,), child)
        else:
            result[path] = value.hex() if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value)
    for key, value in row.items():
        if key in json_columns and isinstance(value, (str, bytes)):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
        visit((key,), value)
    return result


def column_label(path):
    return str(path[0]) + ''.join(f'[{part}]' if isinstance(part, int) else '.' + part for part in path[1:])


def sort_expression(encoded, columns):
    path = json.loads(encoded)
    if not isinstance(path, list) or not path or path[0] not in columns or len(path) > 32:
        raise ValueError('Invalid sort column.')
    root = ident(path[0])
    if len(path) == 1:
        return root, ()
    if columns[path[0]] != 'json' and path[0] != 'payload':
        raise ValueError('Invalid JSON sort column.')
    suffix = ''
    for part in path[1:]:
        if isinstance(part, int) and not isinstance(part, bool) and part >= 0:
            suffix += f'[{part}]'
        elif isinstance(part, str) and len(part) <= 256:
            suffix += '.' + json.dumps(part, ensure_ascii=False)
        else:
            raise ValueError('Invalid JSON sort path.')
    return f'JSON_EXTRACT({root}, %s)', ('$' + suffix,)


@contextmanager
def connect(connection):
    user, password = vault_credential(connection.get('secret_ocid', ''), connection.get('user', ''))
    with _cached_connection(connection.get('host', ''), int(connection.get('port', 3306)), user, password, connection.get('socket', ''), role='explorer') as db:
        yield db


INTERVALS = {
    'hour': "DATE_FORMAT(`event_time`, '%Y-%m-%d %H:00:00')",
    'day': 'DATE(`event_time`)',
    'week': 'DATE_SUB(DATE(`event_time`), INTERVAL WEEKDAY(`event_time`) DAY)',
    'month': "DATE_FORMAT(`event_time`, '%Y-%m-01')",
}


def fill_buckets(buckets, first, last, interval):
    """Show gaps at their actual position on the time axis."""
    counts = {item['time']: item['count'] for item in buckets}
    current = datetime.combine(first, datetime.min.time())
    finish = datetime.combine(last + timedelta(days=1), datetime.min.time())
    if interval == 'week':
        current -= timedelta(days=current.weekday())
    elif interval == 'month':
        current = current.replace(day=1)
    result = []
    while current < finish:
        key = str(current) if interval == 'hour' else current.date().isoformat()
        result.append({'time': key, 'count': counts.get(key, 0)})
        if len(result) > 1000:
            raise ValueError('More than 1,000 time buckets. Select a shorter range or a larger interval.')
        if interval == 'month':
            current = current.replace(year=current.year + (current.month == 12), month=current.month % 12 + 1)
        else:
            current += timedelta(hours=1) if interval == 'hour' else timedelta(days=7 if interval == 'week' else 1)
    return result


def register(app, login_required, render_dashboard):
    bp = Blueprint('log_explore', __name__)

    @bp.get('/log-explore')
    @login_required
    def explore():
        settings = control_store.load_settings()
        connections = settings.get('archive_connections', [])
        selected_connection = request.args.get('connection', '')
        selected_table = request.args.get('table', '')
        tables = [table for table in settings.get('archive_tables', []) if table.get('connection') == selected_connection]
        rule_mode = request.args.get('rule_mode', 'all')
        rules, field_options = [], [('__source_connection__', 'Source connection (multi-select)')]
        source_profiles = [{key: item.get(key, '') for key in ('name', 'host', 'port', 'user', 'socket', 'server_uuid', 'server_uuids', 'server_hostname', 'server_hostnames')} for item in settings.get('source_connections', [])]
        view = 'chart' if request.args.get('view') == 'chart' else 'table'
        interval = request.args.get('interval', 'day')
        time_column = request.args.get('time_column', 'event_time')
        start = request.args.get('start', (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat())
        end = request.args.get('end', datetime.now(timezone.utc).date().isoformat())
        try:
            page = min(100000, max(1, int(request.args.get('page', 1))))
            size = int(request.args.get('size', 50))
            if size not in (25, 50, 100, 250):
                size = 50
        except ValueError:
            page, size = 1, 50
        rows, headers, buckets = [], [], []
        columns = {}
        more = False
        sort = request.args.get('sort', '')
        direction = 'asc' if request.args.get('direction') == 'asc' else 'desc'
        def link(**changes):
            args = request.args.to_dict(flat=False)
            args.pop('download', None)
            args.pop('q', None)
            args.update(connection=selected_connection, table=selected_table, size=size, view=view)
            args.update(changes)
            return url_for('log_explore.explore', **args)
        try:
            rules = parse_rules(request.args)
            if selected_table:
                connection = next((item for item in connections if item['name'] == selected_connection), None)
                table = next((item for item in tables if item['name'] == selected_table), None)
                if connection is None or table is None:
                    raise ValueError('Select a configured archive connection and table.')
                target = ident(table['archive_db']) + '.' + ident(table['archive_table'])
                with connect(connection) as db:
                    cursor = db.cursor(dictionary=True, buffered=True)
                    try:
                        cursor.execute(f'SHOW COLUMNS FROM {target}')
                        columns = {item['Field']: item['Type'].lower() for item in cursor.fetchall()}
                        json_columns = {key for key, typ in columns.items() if typ == 'json' or key == 'payload'}
                        fields = dict.fromkeys((name,) for name in columns)
                        if json_columns:
                            sampled = ', '.join(ident(name) for name in json_columns)
                            time_order = ' ORDER BY `event_time` DESC' if 'event_time' in columns else ''
                            cursor.execute(f'SELECT {sampled} FROM {target}{time_order} LIMIT 100')
                            for sample in cursor.fetchall():
                                fields.update(dict.fromkeys(flatten(sample, json_columns)))
                        for rule in rules:
                            if rule['field'] == '__source_connection__':
                                continue
                            sort_expression(rule['field'], columns)
                            fields[tuple(json.loads(rule['field']))] = None
                        field_options = [('__source_connection__', 'Source connection (multi-select)'), *[(json.dumps(path, ensure_ascii=False), column_label(path)) for path in fields]]
                        where, params = compile_filters(rules, rule_mode, [], columns, source_profiles)
                        if view == 'chart':
                            if time_column not in {'event_time', 'archived_at'}:
                                raise ValueError('Select event_time or archived_at for the X-axis.')
                            if time_column not in columns:
                                raise ValueError(f'Chart view requires an {time_column} column.')
                            axis = ident(time_column)
                            if interval not in INTERVALS:
                                raise ValueError('Select hour, day, week or month.')
                            first, last = date.fromisoformat(start), date.fromisoformat(end)
                            if not 0 <= (last - first).days <= 366:
                                raise ValueError('Select a date range of up to 367 days.')
                            where += (' AND ' if where else ' WHERE ') + f'{axis} >= %s AND {axis} < %s'
                            bucket_expression = INTERVALS[interval].replace('`event_time`', axis)
                            cursor.execute(f'SELECT {bucket_expression} AS bucket, COUNT(*) AS records FROM {target}{where} GROUP BY bucket ORDER BY bucket', params + (first, last + timedelta(days=1)))
                            buckets = [{'time': str(item['bucket']), 'count': item['records']} for item in cursor.fetchall()]
                            buckets = fill_buckets(buckets, first, last, interval)
                        else:
                            order, order_params = sort_expression(sort, columns) if sort else (ident('event_time') if 'event_time' in columns else ident(next(iter(columns))), ())
                            tie_columns = ['event_time', 'source_fingerprint'] if 'source_fingerprint' in columns else ['id']
                            tie = ''.join(', ' + ident(name) + ' ' + direction for name in tie_columns if name in columns and order != ident(name))
                            cursor.execute(f'SELECT * FROM {target}{where} ORDER BY {order} {direction}{tie} LIMIT %s OFFSET %s', params + order_params + (size + 1, (page - 1) * size))
                            raw = cursor.fetchall()
                            more = len(raw) > size
                            json_columns = {key for key, typ in columns.items() if typ == 'json' or key == 'payload'}
                            rows = [flatten(row, json_columns) for row in raw[:size]]
                            paths = dict.fromkeys(path for row in rows for path in row)
                            headers = [(path, column_label(path), json.dumps(path, ensure_ascii=False)) for path in paths]
                        if request.args.get('download') == 'csv':
                            output = io.StringIO()
                            writer = csv.writer(output)
                            def safe(value):
                                value = '' if value is None else str(value)
                                return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value
                            if view == 'chart':
                                writer.writerow([time_column + ' (UTC)', 'Records'])
                                writer.writerows((item['time'], item['count']) for item in buckets)
                            else:
                                writer.writerow([label for _, label, _ in headers])
                                writer.writerows([safe(row.get(path)) for path, _, _ in headers] for row in rows)
                            return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=archive-log-page.csv', 'Cache-Control': 'no-store'})
                    finally:
                        cursor.close()
        except Exception as exc:
            rows, headers, buckets, more = [], [], [], False
            flash(f'Log Explore: {exc}', 'error')
        def rule_sources(rule):
            if rule.get('field') != '__source_connection__':
                return []
            try:
                value = json.loads(rule.get('value', '[]'))
                return value if isinstance(value, list) else []
            except ValueError:
                return []
        return render_dashboard('log_explore.html', active_menu='log_explore', connections=connections, tables=tables, selected_connection=selected_connection, selected_table=selected_table, source_profiles=source_profiles, rules=rules, rule_sources=rule_sources, rule_mode=rule_mode, field_options=field_options, operators=OPERATORS, source_metadata_available='source_server_uuid' in columns, view=view, interval=interval, time_column=time_column, start=start, end=end, page=page, size=size, rows=rows, headers=headers, more=more, buckets=buckets, link=link, sort=sort, direction=direction)

    app.register_blueprint(bp)
