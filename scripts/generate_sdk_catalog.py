"""Generate SDK tool metadata from the gateway catalog, or verify committed output."""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def generated(root):
    catalog = json.loads((root / 'services/gateway/src/agent_gateway/catalog.json').read_text())
    rows = sorted([{'name': row['name'], 'read_only': row.get('annotations', {}).get('readOnlyHint') is True}
                   for row in catalog] + [{'name': 'platform_capabilities', 'read_only': True}],
                  key=lambda row: row['name'])
    declarations = root / 'packages/deedkit/lib/index.d.ts'
    union = 'export type ToolName =\n' + '\n'.join('  | ' + "'" + row["name"] + "'" for row in rows) + ';'
    types, count = re.subn(r'export type ToolName =[^;]+;', lambda _: union, declarations.read_text(), count=1)
    if count != 1:
        raise ValueError('ToolName declaration not found')
    return {root / 'packages/deedkit/lib/tools.json': '[\n' + ',\n'.join('  ' + json.dumps(row) for row in rows) + '\n]\n', declarations: types}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    changed = []
    for path, content in generated(ROOT).items():
        if path.read_text() != content:
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.write_text(content)
    if args.check and changed:
        parser.exit(1, 'SDK catalog drift. Run python3 scripts/generate_sdk_catalog.py\n')


if __name__ == '__main__':
    main()
