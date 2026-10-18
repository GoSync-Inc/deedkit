"""Bounded `field_errors` shared by every messaging rejection: `{field, reason, expected?, allowed?}`.

Paths, reasons, hints and allowed values come from reviewed code or schemas, never from input values.
Same shape as the gateway's (ANYM-295); a path is `/`-separated identifier or index segments.
"""
import re

FIELD_ERROR_REASONS = frozenset(('required', 'unknown_field', 'type', 'enum', 'min_length',
    'max_length', 'pattern', 'minimum', 'maximum', 'min_items', 'max_items', 'unique',
    'format', 'blank', 'size'))
MAX_FIELD_ERRORS = 20
MAX_FIELD_PATH = 128
MAX_ALLOWED = 50
FIELD_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
SEGMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}|[0-9]{1,4}')


def field_error(field, reason, expected=None, allowed=None):
    if (reason not in FIELD_ERROR_REASONS or not isinstance(field, str) or len(field) > MAX_FIELD_PATH
            or not all(SEGMENT.fullmatch(part) for part in field.split('/'))):
        raise ValueError('invalid_field_error')
    return {'field': field, 'reason': reason, **({'expected': expected} if expected else {}),
            **({'allowed': list(allowed)[:MAX_ALLOWED]} if allowed else {})}


def one_per_field(errors):
    """First entry per field path, in the given order, at most MAX_FIELD_ERRORS."""
    seen = set()
    return [e for e in errors if not (e['field'] in seen or seen.add(e['field']))][:MAX_FIELD_ERRORS]


# Pydantic error type -> (reason, expected type name for `type`).
_REASONS = {'missing': 'required', 'extra_forbidden': 'unknown_field', 'literal_error': 'enum', 'enum': 'enum',
    'string_too_short': 'min_length', 'string_too_long': 'max_length', 'string_pattern_mismatch': 'pattern',
    'greater_than': 'minimum', 'greater_than_equal': 'minimum', 'less_than': 'maximum',
    'less_than_equal': 'maximum', 'too_short': 'min_items', 'too_long': 'max_items'}
_TYPES = {'string': 'string', 'int': 'integer', 'float': 'number', 'bool': 'boolean', 'list': 'array',
    'dict': 'object', 'model': 'object', 'mapping': 'object'}


def _resolve(schema, node):
    """Follow a local $ref and, for a nullable union, the non-null branch."""
    for _ in range(8):
        if not isinstance(node, dict): return {}
        ref = node.get('$ref')
        if isinstance(ref, str) and ref.startswith('#/'):
            node = schema
            for key in ref[2:].split('/'): node = node.get(key, {}) if isinstance(node, dict) else {}
        elif node.get('anyOf'): node = next((o for o in node['anyOf'] if isinstance(o, dict) and o.get('type') != 'null'), {})
        else: return node
    return {}


def _walk(schema, loc):
    """Follow an error location through the reviewed schema.

    Returns (path, rank, node, complete): the longest prefix of the location the schema declares (property names,
    literal map keys and array indexes only), the schema order of that path, its schema node, and whether the
    whole location was declared. A segment the schema does not declare is input and is never kept.
    """
    node = _resolve(schema, schema)
    path, rank = [], []
    for part in loc:
        if isinstance(part, bool): break
        if isinstance(part, int) and 'items' in node:
            path.append(str(part)); rank.append(part); node = _resolve(schema, node['items'])
        elif isinstance(part, str) and part in node.get('properties', {}):
            path.append(part); rank.append(list(node['properties']).index(part))
            node = _resolve(schema, node['properties'][part])
        elif isinstance(part, str) and part in node.get('propertyNames', {}).get('enum', ()):
            path.append(part); rank.append(node['propertyNames']['enum'].index(part))
            node = _resolve(schema, node.get('additionalProperties', {}))
        else: break
    return path, rank, node, len(path) == len(loc)


def _expected(reason, kind, node, ctx):
    if reason == 'type': return _TYPES.get(kind.split('_')[0])
    if reason == 'pattern' and isinstance(node.get('pattern'), str) and len(node['pattern']) <= 80: return 'matching ' + node['pattern']
    if reason in ('min_length', 'max_length'):
        low = node.get('minLength', ctx.get('min_length')); high = node.get('maxLength', ctx.get('max_length'))
        if low is not None and high is not None: return f'{low}–{high} characters'
        return f'at least {low} character{"" if low == 1 else "s"}' if low is not None else f'at most {high} characters'
    if reason in ('min_items', 'max_items'):
        return f'at least {ctx["min_length"]} items' if 'min_length' in ctx else f'at most {ctx["max_length"]} items'
    for key, text in (('ge', 'at least {}'), ('gt', 'more than {}'), ('le', 'at most {}'), ('lt', 'less than {}')):
        if reason in ('minimum', 'maximum') and isinstance(ctx.get(key), (int, float)): return text.format(ctx[key])
    return None


def pydantic_field_errors(model, errors):
    """Map `ValidationError.errors(include_input=False)` to field_errors in schema order, one per field."""
    schema = model.model_json_schema()
    found = []
    for error in errors:
        loc, kind = tuple(error.get('loc', ())), error['type']
        key_error = loc[-1:] == ('[key]',)
        path, rank, node, complete = _walk(schema, loc[:-1] if key_error else loc)
        reason = _REASONS.get(kind) or ('type' if kind.rsplit('_', 1)[-1] in ('type', 'parsing') else 'format')
        allowed = None
        if key_error:  # A map key outside the reviewed literal keys.
            reason, allowed = 'unknown_field', node.get('propertyNames', {}).get('enum')
        elif kind == 'extra_forbidden' and len(path) == len(loc) - 1:
            # A rejected unknown key is named only when it is a short identifier.
            if isinstance(loc[-1], str) and FIELD_NAME.fullmatch(loc[-1]): path.append(loc[-1]); rank.append(10**6)
        elif not complete:
            if 'propertyNames' in node: continue  # Under an unknown map key, which has its own entry.
            reason, kind = 'format', ''
        elif reason == 'enum':
            allowed = node.get('enum') or ([node['const']] if 'const' in node else None)
        allowed = [v for v in allowed or () if isinstance(v, (str, int, bool)) and len(str(v)) <= 64]
        text = '/'.join(path)
        if len(text) > MAX_FIELD_PATH: text, rank = '/'.join(path[:1]), rank[:1]
        found.append((rank or [10**6], text or 'arguments', None if not complete and not key_error else
            _expected(reason, kind, node, error.get('ctx') or {}), reason, allowed))
    return one_per_field(field_error(text, reason, expected, allowed)
        for _, text, expected, reason, allowed in sorted(found, key=lambda f: f[0]))
