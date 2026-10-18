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


def _names(node, found):
    """Every property and literal-key name the reviewed schema declares; the only names a path may carry."""
    if isinstance(node, dict):
        found.update(node.get('properties', {}))
        found.update(v for v in node.get('propertyNames', {}).get('enum', ()) if isinstance(v, str))
        for value in node.values():
            _names(value, found)
    elif isinstance(node, list):
        for value in node:
            _names(value, found)
    return found


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


def _node(schema, path):
    """The reviewed schema node at a path, or {} when the path leaves the schema."""
    node = _resolve(schema, schema)
    for part in path:
        node = node.get('items', {}) if part.isdigit() else node.get('properties', {}).get(part, node.get('additionalProperties', {}))
        node = _resolve(schema, node)
    return node


def _expected(reason, kind, node, ctx):
    if reason == 'type': return _TYPES.get(kind.split('_')[0])
    if reason == 'pattern' and len(node.get('pattern', '')) <= 80: return 'matching ' + node['pattern']
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
    names = _names(schema, set())
    order = {name: index for index, name in enumerate(schema.get('properties', {}))}
    found = []
    for error in errors:
        path = []
        for part in error.get('loc', ()):
            if isinstance(part, int) and not isinstance(part, bool): path.append(str(part))
            elif isinstance(part, str) and part in names: path.append(part)
            else: break
        # A rejected unknown key is named only when it is a short identifier.
        if error['type'] == 'extra_forbidden' and len(error.get('loc', ())) == len(path) + 1:
            part = error['loc'][-1]
            if isinstance(part, str) and FIELD_NAME.fullmatch(part): path.append(part)
        kind = error['type']
        reason = _REASONS.get(kind) or ('type' if kind.rsplit('_', 1)[-1] in ('type', 'parsing') else 'format')
        node = _node(schema, path)
        allowed = None
        if error.get('loc', ())[-1:] == ('[key]',):  # A map key outside the reviewed literal keys.
            reason, allowed = 'unknown_field', node.get('propertyNames', {}).get('enum')
        elif reason == 'enum':
            allowed = node.get('enum') or ([node['const']] if 'const' in node else None)
        allowed = [v for v in allowed or () if isinstance(v, (str, int, bool)) and len(str(v)) <= 64]
        text = '/'.join(path)
        if not text or len(text) > MAX_FIELD_PATH: text = None
        found.append((order.get(path[0], len(order)) if path else len(order), text,
            _expected(reason, kind, node, error.get('ctx') or {}), reason, allowed))
    result = []
    for _, text, expected, reason, allowed in sorted(found, key=lambda f: f[0]):
        result.append(field_error(text or 'arguments', reason, expected, allowed))
    return one_per_field(result)
