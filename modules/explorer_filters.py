"""Parameter-bound field rules shared by archive rows, charts and downloads."""
import json
import math
from decimal import Decimal, InvalidOperation

OPERATORS = [('contains', 'Contains'), ('eq', 'Equals'), ('ne', 'Does not equal'), ('gt', 'Greater than / after'), ('gte', 'At least / on or after'), ('lt', 'Less than / before'), ('lte', 'At most / on or before'), ('is_null', 'Is missing / null'), ('not_null', 'Is present')]


def parse_rules(args):
    fields, operators, values = (args.getlist(key) for key in ('rule_field', 'rule_op', 'rule_value'))
    if not len(fields) == len(operators) == len(values) or len(fields) > 20:
        raise ValueError('Provide up to 20 complete filter rules.')
    rules = []
    for field, operator, value in zip(fields, operators, values):
        if not field and not value:
            continue
        if not field or operator not in dict(OPERATORS) or len(field) > 2048 or len(value) > 1000:
            raise ValueError('Choose a valid field and operator for every filter rule.')
        if field == '__source_connection__' and value in ('', '[]'):
            continue
        rules.append({'field': field, 'op': operator, 'value': value})
    return rules


def typed_value(value):
    try:
        result = json.loads(value)
    except ValueError:
        result = value
    if not isinstance(result, (str, int, float, bool)) or (isinstance(result, float) and not math.isfinite(result)):
        raise ValueError('Use a text, number or boolean rule value; use Is missing / null for null values.')
    return result


def compile_filters(rules, mode, sources, columns, source_profiles=()):
    # Lazy import avoids a cycle with the page module's common column-path validator.
    from .log_explorer import sort_expression
    if mode not in ('all', 'any'):
        raise ValueError('Choose All rules or Any rule.')
    clauses, params = [], []
    if sources:
        if 'source_server_uuid' not in columns:
            raise ValueError('This archive has no source UUID metadata yet. Run the archiver to upgrade its table.')
        known = list(dict.fromkeys(value for value in sources if value != '__unknown__'))
        alternatives = []
        if known:
            import uuid
            for value in known:
                if str(uuid.UUID(value)) != value:
                    raise ValueError('Select a valid source server UUID.')
            alternatives.append('`source_server_uuid` IN (' + ','.join('%s' for _ in known) + ')')
            params.extend(known)
        if '__unknown__' in sources:
            alternatives.append("(`source_server_uuid` IS NULL OR `source_server_uuid` = '')")
        clauses.append('(' + ' OR '.join(alternatives) + ')')
    rule_clauses, rule_params = [], []
    for rule in rules:
        if rule['field'] == '__source_connection__':
            names = json.loads(rule['value'])
            if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names) or rule['op'] not in ('eq', 'ne'):
                raise ValueError('Source connection rules require selected connections and Equals / Does not equal.')
            selected = []
            for name in names:
                if name == '__unknown__':
                    selected.append(name)
                    continue
                profile = next((item for item in source_profiles if item.get('name') == name), None)
                if not profile:
                    raise ValueError('Select a registered source connection for this rule.')
                observed = profile.get('server_uuids') or ([profile['server_uuid']] if profile.get('server_uuid') else [])
                if not observed:
                    raise ValueError('The selected source has not registered its UUID yet.')
                selected.extend(observed)
            clause, bound = compile_filters([], 'all', selected, columns)
            clause = clause.removeprefix(' WHERE ')
            rule_clauses.append(f'NOT ({clause})' if rule['op'] == 'ne' else clause)
            rule_params.extend(bound)
            continue
        expression, path_params = sort_expression(rule['field'], columns)
        path = json.loads(rule['field'])
        is_json = columns[path[0]] == 'json' or len(path) > 1
        if path == ['source_fingerprint']:
            expression = 'LOWER(HEX(`source_fingerprint`))'
        operator, value = rule['op'], rule['value']
        if operator in ('is_null', 'not_null'):
            missing = f'({expression} IS NULL OR JSON_TYPE({expression}) = %s)' if is_json else f'{expression} IS NULL'
            rule_clauses.append(f'NOT ({missing})' if operator == 'not_null' else missing)
            rule_params.extend(path_params + path_params + ('NULL',) if is_json else path_params)
        elif operator == 'contains':
            if is_json:
                expression = f'JSON_UNQUOTE({expression})'
            term = '%' + value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            rule_clauses.append(f"CAST({expression} AS CHAR) LIKE %s ESCAPE '!' ")
            rule_params.extend(path_params + (term,))
        else:
            symbol = {'eq': '=', 'ne': '<>', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}[operator]
            if is_json:
                parsed = typed_value(value)
                if operator in ('gt', 'gte', 'lt', 'lte'):
                    if isinstance(parsed, bool):
                        raise ValueError('Ordering rules require text/date or numeric values.')
                    type_test = f"JSON_TYPE({expression}) = 'STRING'" if isinstance(parsed, str) else f"JSON_TYPE({expression}) IN ('INTEGER','DOUBLE','DECIMAL')"
                    rule_clauses.append(f'({type_test} AND {expression} {symbol} CAST(%s AS JSON))')
                    rule_params.extend(path_params)
                else:
                    rule_clauses.append(f'{expression} {symbol} CAST(%s AS JSON)')
                rule_params.extend(path_params + (json.dumps(parsed, ensure_ascii=False),))
            else:
                if any(columns[path[0]].startswith(prefix) for prefix in ('int', 'bigint', 'tinyint', 'smallint', 'mediumint', 'decimal', 'float', 'double')):
                    try:
                        number = Decimal(value)
                        if not number.is_finite():
                            raise InvalidOperation
                        value = number
                    except InvalidOperation:
                        raise ValueError('This field requires a numeric rule value.') from None
                rule_clauses.append(f'{expression} {symbol} %s')
                rule_params.extend(path_params + (value,))
    if rule_clauses:
        clauses.append('(' + (' AND ' if mode == 'all' else ' OR ').join(rule_clauses) + ')')
        params.extend(rule_params)
    return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), tuple(params)
