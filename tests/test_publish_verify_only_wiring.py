"""ANYM-142: in both publish workflows a main-ref run is allowed only as verify_only, which checks the publisher
identity and cannot upload; a verify_only run never reaches a publishing job.

Static contract of the actual workflow files. Each job's `if:` is evaluated by a small strict evaluator (only the
operators and names these workflows use; any status function such as always() or an unknown name fails the test),
and `needs` is followed, so an equivalent rewrite of a condition passes and a removed or weakened gate fails.
Nothing runs Actions, OIDC, a registry, the network or credentials.
"""
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRIES = {'publish-npm.yml': 'npm', 'publish-pypi.yml': 'pypi'}
REFS = ('refs/heads/main', 'refs/tags/v1.2.3', 'refs/heads/feature')
UPLOAD = re.compile(r'\bnpm\s+publish\b|\btwine\b|\bupload\b|gh-action-pypi-publish|\bpublish\b', re.I)


def workflow(name):
    # reads: .github/workflows/publish-npm.yml
    # reads: .github/workflows/publish-pypi.yml
    return yaml.safe_load((ROOT / '.github' / 'workflows' / name).read_text())


TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>&&|\|\||==|!=|!|\(|\))|(?P<name>[A-Za-z_][A-Za-z0-9_.-]*))")


def condition(text, context):
    """Evaluate a GitHub `if:` expression over `context`; refuse anything but the forms these workflows can use."""
    if text is None:
        return True
    text = str(text).strip()
    if text.startswith('${{') and text.endswith('}}'):
        text = text[3:-2]
    tokens, at = [], 0
    while at < len(text.rstrip()):
        match = TOKEN.match(text, at)
        assert match, f'unsupported expression {text!r}'
        tokens.append((match.lastgroup, match.group(match.lastgroup)))
        at = match.end()
    position = [0]

    def peek():
        return tokens[position[0]] if position[0] < len(tokens) else (None, None)

    def take():
        position[0] += 1
        return tokens[position[0] - 1]

    def primary():
        kind, value = take()
        if value == '(':
            result = either()
            assert take()[1] == ')', f'unbalanced {text!r}'
            return result
        if value == '!':
            return not truthy(primary())
        if kind == 'str':
            return value[1:-1].replace("''", "'")
        if kind == 'name' and value in ('true', 'false'):
            return value == 'true'
        assert kind == 'name' and value in context and peek()[1] != '(', f'unsupported name or function in {text!r}'
        return context[value]

    def comparison():
        left = primary()
        while peek()[1] in ('==', '!='):
            operator, right = take()[1], primary()
            left = (left == right) if operator == '==' else (left != right)
        return left

    def both():
        left = comparison()
        while peek()[1] == '&&':
            take()
            right = comparison()
            left = right if truthy(left) else left
        return left

    def either():
        left = both()
        while peek()[1] == '||':
            take()
            right = both()
            left = left if truthy(left) else right
        return left

    def truthy(value):
        return bool(value)

    result = either()
    assert position[0] == len(tokens), f'unsupported expression {text!r}'
    return truthy(result)


def running(jobs, verify_only, ref):
    """The jobs a dispatch would run: its own `if:` holds and every job it needs runs (a skipped need skips it)."""
    context = {'inputs.verify_only': verify_only, 'github.ref': ref}
    result = {}

    def runs(name, seen=()):
        if name not in result:
            assert name not in seen, f'cyclic needs at {name}'
            job = jobs[name]
            needs = job.get('needs', [])
            needs = [needs] if isinstance(needs, str) else needs
            result[name] = condition(job.get('if'), context) and all(runs(n, (*seen, name)) for n in needs)
        return result[name]
    return {name for name in jobs if runs(name)}


def code(step):
    """A step's action and shell commands, without shell comment lines (which only explain)."""
    commands = [line for line in str(step.get('run', '')).splitlines() if not line.lstrip().startswith('#')]
    return str(step.get('uses', '')) + ' ' + '\n'.join(commands)


def uploads(job):
    """Whether a job can upload a package: a publishing step, or a reusable workflow it calls."""
    if 'uses' in job:
        return 'checks.yml' not in job['uses']  # only the reusable checks workflow is known not to upload
    return any(UPLOAD.search(code(step)) for step in job.get('steps', []))


# spec: ANYM-142
@pytest.mark.parametrize('name', sorted(REGISTRIES))
def test_verify_only_is_a_boolean_input_that_defaults_to_publishing_nothing_extra(name):
    dispatch = workflow(name)[True]['workflow_dispatch']['inputs']  # YAML reads the `on` key as True
    assert dispatch['verify_only']['type'] == 'boolean' and dispatch['verify_only']['default'] is False


# spec: ANYM-142
@pytest.mark.parametrize('name', sorted(REGISTRIES))
def test_a_main_ref_verify_only_run_verifies_trust_and_reaches_no_upload(name):
    jobs = workflow(name)['jobs']
    publishing = {job for job, body in jobs.items() if uploads(body)}
    verifying = {job for job, body in jobs.items() if any('verify_publisher_trust.py' in str(step.get('run', ''))
                                                          for step in body.get('steps', []))}
    assert publishing and len(verifying) == 1
    run = running(jobs, True, 'refs/heads/main')
    assert run == verifying, run  # the trust check alone: no tag check, release checks or upload
    (verify,) = verifying
    steps = jobs[verify]['steps']
    assert not uploads(jobs[verify]) and not any('download-artifact' in str(s.get('uses', '')) for s in steps)
    assert [s['run'].split() for s in steps if 'run' in s] == [['python3', 'scripts/verify_publisher_trust.py', REGISTRIES[name]]]
    assert jobs[verify]['permissions'].get('id-token') == 'write'  # the OIDC exchange needs it, nothing else


# spec: ANYM-142
@pytest.mark.parametrize('ref', REFS[1:])
@pytest.mark.parametrize('name', sorted(REGISTRIES))
def test_verify_only_off_main_runs_nothing(name, ref):
    assert running(workflow(name)['jobs'], True, ref) == set()


# spec: ANYM-142
@pytest.mark.parametrize('ref', REFS)
@pytest.mark.parametrize('name', sorted(REGISTRIES))
def test_a_publishing_run_never_runs_the_trust_check_and_publishes_only_after_the_tag_gate(name, ref):
    jobs = workflow(name)['jobs']
    run = running(jobs, False, ref)
    assert not any('verify_publisher_trust.py' in str(s.get('run', '')) for job in run for s in jobs[job].get('steps', []))
    # Every upload depends on the tag job, whose first check refuses any ref but refs/tags/v<version> (main included).
    gate = next(job for job, body in jobs.items() if any('refs/tags/v$VERSION' in str(s.get('run', '')) for s in body.get('steps', [])))
    step = next(s for s in jobs[gate]['steps'] if 'refs/tags/v$VERSION' in str(s.get('run', '')))
    assert step['env']['REF'] == '${{ github.ref }}' and 'test "$REF" = "refs/tags/v$VERSION"' in step['run']

    def depends(job):
        needs = jobs[job].get('needs', [])
        needs = [needs] if isinstance(needs, str) else needs
        return gate in needs or any(depends(n) for n in needs)
    assert all(depends(job) for job in jobs if uploads(jobs[job]))
