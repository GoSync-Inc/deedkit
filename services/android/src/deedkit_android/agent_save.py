"""A successful local-agent run becomes a saved scenario (ANYM-973): the compiler writes, the model only repairs.

`compile_log` turns the run's action log into scenario commands deterministically. `Saver` writes the draft through the
library's draft action under the calling principal, test-runs that exact version in the run's own session (stage=draft, the
guard applies) and publishes it through the library's publish gate only after a succeeded test of that version."""

import json
import re
import time
import uuid
from xml.etree import ElementTree

import yaml

from .agent_screen import cap_bytes
from .app_maps import compile_scenario
from .config import Refused
from .guard import parse_bounds
from .secrets import REFERENCE

TEST_TIMEOUT_S = 60
TEST_DEADLINE_S = 80
MAX_REPAIRS = 3
ELEMENT_WAIT_MS = 5000
PERCENT = re.compile(r'\s*(\d+(?:\.\d+)?)(%?)\s*,\s*(\d+(?:\.\d+)?)(%?)\s*')


class SaveError(Exception):
    """The log cannot become a scenario: `code` says why and `step` names the action (its number in the log)."""

    def __init__(self, code, step=None):
        super().__init__(code)
        self.code, self.step = code, step


def resolve_point(xml, point):
    """The selector of the element a point tap hit, when it names exactly one node on that screen, else None.

    The smallest node holding the point is tried first (resource-id, then exact text), then its parent."""
    root = ElementTree.fromstring(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    nodes = [(node, parse_bounds(node.get('bounds'))) for node in root.iter('node')]
    nodes = [(node, box) for node, box in nodes if box]
    width, height = max((box[2] for _, box in nodes), default=0), max((box[3] for _, box in nodes), default=0)
    found = PERCENT.fullmatch(point)
    if not found or not nodes:
        return None
    x = float(found[1]) * (width / 100 if found[2] else 1)
    y = float(found[3]) * (height / 100 if found[4] else 1)
    holding = sorted(((box[2] - box[0]) * (box[3] - box[1]), index, node) for index, (node, box) in enumerate(nodes)
                     if box[0] <= x <= box[2] and box[1] <= y <= box[3])
    if not holding:
        return None
    chain = [holding[0][2]]
    parent = parents.get(holding[0][2])
    if parent is not None and parent.tag == 'node':
        chain.append(parent)
    for node in chain:
        for field, key in (('resource-id', 'resourceId'), ('text', 'textExact')):
            value = node.get(field)
            if value and sum(other.get(field) == value for other, _ in nodes) == 1:
                return {key: value}
    return None


def substitute(text, params, names):
    """A typed literal equal to a saved parameter's value becomes `${NAME}`; a secret reference stays as it is."""
    if REFERENCE.fullmatch(text):
        return text
    for name in names:
        value = str(params.get(name, ''))
        if value and value in text:
            text = text.replace(value, '${' + name + '}')
    return text


def compile_log(actions, params, names, outputs, data):
    """Scenario commands from the action log of a succeeded run; raises SaveError."""
    commands, reads = [], []
    for entry in actions:
        if entry.get('verdict') != 'allowed':
            continue
        tool, args, step = entry['tool'], entry.get('arguments', {}), entry['n']
        if tool in ('screen', 'read_list', 'finish') or tool.startswith('flow_'):
            continue
        if tool == 'open_app':
            commands.append({'launchApp': {'appId': args['package'], 'stopApp': False}})
        elif tool == 'tap':
            selector = args.get('selector') or entry.get('resolved')
            if not selector:
                raise SaveError('unstable_point_tap', step)
            commands += [{'extendedWaitUntil': {'visible': dict(selector), 'timeout': ELEMENT_WAIT_MS}}, {'tapOn': dict(selector)}]
        elif tool == 'type':
            commands.append({'inputText': substitute(args['text'], params, names)})
        elif tool == 'key':
            commands.append({'pressKey': args['key']})
        elif tool == 'swipe':
            options = {'duration': args.get('duration_ms', 400)}
            options.update({'start': args['start'], 'end': args['end']} if args.get('start') else {'direction': args['direction']})
            commands.append({'swipe': options})
        elif tool == 'wait':
            if args.get('settled'):
                commands.append({'waitForAnimationToEnd': {'timeout': args.get('timeout_ms', 5000)}})
            else:
                wanted = args.get('visible') is not None
                commands.append({'extendedWaitUntil': {'visible' if wanted else 'notVisible': args.get('visible') or args['gone'],
                                                       'timeout': args.get('timeout_ms', 5000)}})
        elif tool == 'read_text':
            reads.append((len(commands), entry))
            commands.append(None)
        else:
            raise SaveError('unsupported_step', step)
    keyed = {}
    for name in outputs:
        value = str(data.get(name, ''))
        candidates = [(position, entry) for position, entry in reads if value and value in str(entry.get('read', ''))]
        if not candidates:
            raise SaveError('output_not_read:' + name)
        exact = [item for item in candidates if str(item[1].get('read', '')).strip() == value]
        position, entry = (exact or candidates)[-1]
        selector = entry.get('arguments', {}).get('selector')
        if not selector:
            raise SaveError('unsupported_read', entry['n'])
        keyed[position] = {'copyTextFrom': {**selector, 'key': name}}
    commands = [keyed.get(index, command) for index, command in enumerate(commands)]
    return [command for command in commands if command is not None]


class Saver:
    """The save state of one run that asked for `save_as`."""

    def __init__(self, run):
        self.run, self.name = run, run.request.save_as
        self.names = list(run.request.param_names)
        self.version = self.test_id = self.error = None
        self.repairs = self.tests = 0
        self.published = False
        self.last = None

    @property
    def library(self):
        return self.run.agents.store.library

    def known(self):
        return self.run.declared

    def view(self):
        found = {'name': self.name, 'version': self.version, 'published': self.published, 'test_request_id': self.test_id,
                 'repairs': self.repairs}
        return {**found, **({'error': self.error} if self.error else {})}

    def write_draft(self, source):
        """The draft under the calling principal; `base_version` rules of the library are kept."""
        owner, library = self.run.owner, self.library
        if not isinstance(yaml.safe_load(source), list):
            raise Refused('scenario_requires_draft', 422)
        commands, _ = compile_scenario(library, owner, source, {}, stage='draft', validate_only=True, secrets=self.known())
        self.run.check_commands(commands)  # the run's apps and declared secrets only, for a repair as for the log
        head = library.head(owner, 'flow', self.name)
        base = (head['draft'] or head['published']) if head else None
        written = library.manage(owner, action='draft', name=self.name, source=source, base_version=base,
                                 message='local-agent run ' + self.run.id)
        self.version = written['version']

    def draft_from_log(self, data):
        commands = compile_log(self.run.actions, self.run.params, self.names, self.run.request.outputs, data)
        source = yaml.safe_dump(commands, allow_unicode=True, sort_keys=False)
        if len(source.encode()) > 32768:
            raise SaveError('invalid_flow_source')
        self.write_draft(source)

    async def test(self):
        """Run the current draft once in this run's session; returns the receipt. A guard stop is raised, never published."""
        run, devices = self.run, self.run.agents.devices
        self.tests += 1
        self.test_id = request_id = str(uuid.uuid4())
        commands, metadata = compile_scenario(self.library, run.owner, None, dict(run.params), stage='draft',
                                              root={'name': self.name, 'version': self.version}, secrets=self.known())
        identity = {'name': self.name, 'version': self.version, 'stage': 'draft', 'parameters': dict(run.params)}
        receipt = await devices.execute(run.owner, run.device, run.request.session_id, request_id, None, TEST_TIMEOUT_S, 0,
                                        flow=commands, identity=identity, metadata={'library': metadata}, held=True)
        deadline = time.monotonic() + TEST_DEADLINE_S
        while receipt.get('state') in (None, 'running') and time.monotonic() < deadline:
            receipt = await devices.receipt(run.owner, request_id, 5)
        self.last = receipt
        return receipt

    def publish(self):
        """Only after a succeeded test of this exact version: the library's own gate decides."""
        owner = self.run.owner
        head = self.library.head(owner, 'flow', self.name)
        self.library.manage(owner, action='publish', name=self.name, version=self.version,
                            base_version=head['published'] if head else None, test_request_id=self.test_id,
                            message='local-agent run ' + self.run.id)
        self.published = True

    @staticmethod
    def failure(receipt):
        """What the engine needs to repair a draft: the failing step and its observation."""
        keys = ('state', 'error', 'failed_command_index', 'failed_command_path', 'failed_command', 'details', 'hint',
                'diagnostic', 'observation')
        return cap_bytes(json.dumps({key: receipt[key] for key in keys if key in receipt}, ensure_ascii=False, default=str), 3000)


async def settle(saver, receipt):
    """After a test: publish and finish when it passed; ends the run at a guard stop; else the failure for repair."""
    if receipt.get('state') == 'succeeded':
        try:
            saver.publish()
        except Refused as error:
            saver.error = error.code
            return None, 'draft saved but not published: ' + error.code
        return True, 'the test passed and the scenario is published'
    if receipt.get('state') == 'stopped' and receipt.get('guard'):
        saver.error = 'test_stopped_by_guard'
        return False, receipt['guard']
    saver.error = 'test_failed'
    return None, Saver.failure(receipt)
