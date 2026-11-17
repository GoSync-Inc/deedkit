"""One credential-bound GitHub tree read, with no redirect or anonymous retry."""
import json
from pathlib import Path
import urllib.error
import urllib.request

import pytest
import yaml

from scripts import tiden_ci


TOKEN = 'synthetic-ci-read-credential'
SHA, TREE = 'a' * 40, 'b' * 40
REPOSITORY = 'GoSync-Inc/deedkit'


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, limit):
        assert limit == 1_000_000
        return json.dumps({'sha': SHA, 'tree': {'sha': TREE}, 'message': TOKEN}).encode()


def authenticated_reader(monkeypatch, outcome=None):
    calls = []
    monkeypatch.setenv('TIDEN_GITHUB_READ_TOKEN', TOKEN)

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.full_url == f'https://api.github.com/repos/{REPOSITORY}/git/commits/{SHA}'
            assert request.get_header('Authorization') == 'Bearer ' + TOKEN
            assert timeout == 30
            if outcome:
                raise urllib.error.HTTPError(request.full_url, outcome, TOKEN, {}, None)
            return Response()

    def build(*handlers):
        assert len(handlers) == 1 and isinstance(handlers[0], urllib.request.HTTPRedirectHandler)
        # A redirect must never forward this credential, even to another GitHub URL.
        assert handlers[0].redirect_request(None, None, 302, '', {}, 'https://elsewhere.test/') is None
        return Opener()

    monkeypatch.setattr(tiden_ci.urllib.request, 'build_opener', build)
    monkeypatch.setattr(tiden_ci.urllib.request, 'urlopen',
                        lambda *_args, **_kwargs: pytest.fail('no anonymous fallback'))
    return calls


# spec: ANYM-322
def test_existing_read_credential_verifies_exact_tree_without_redirect_or_output(monkeypatch, capsys):
    calls = authenticated_reader(monkeypatch)
    result = tiden_ci.commit_tree(REPOSITORY, SHA)
    assert result == TREE and result.failure == '' and len(calls) == 1
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


# spec: ANYM-322
@pytest.mark.parametrize('status', [302, 401, 403])
def test_authenticated_read_failure_never_retries_or_uses_anonymous_fallback(monkeypatch, status):
    calls = authenticated_reader(monkeypatch, status)
    result = tiden_ci.commit_tree(REPOSITORY, SHA)
    assert result == '' and result.failure == f'http_{status}' and len(calls) == 1
    assert TOKEN not in str(result) + result.failure


# spec: ANYM-322
def test_malformed_credential_refuses_before_any_network_read(monkeypatch):
    monkeypatch.setenv('TIDEN_GITHUB_READ_TOKEN', TOKEN + '\r\nX-Injected: value')
    monkeypatch.setattr(tiden_ci.urllib.request, 'build_opener', lambda *_: pytest.fail('no credential send'))
    monkeypatch.setattr(tiden_ci.urllib.request, 'urlopen', lambda *_a, **_k: pytest.fail('no anonymous read'))
    result = tiden_ci.commit_tree(REPOSITORY, SHA)
    assert result == '' and result.failure == 'invalid_credential'


# spec: ANYM-322
def test_only_publisher_receives_existing_job_token_without_permission_expansion():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/checks.yml').read_text())
    assert workflow['permissions'] == {'contents': 'read'}
    steps = workflow['jobs']['tiden']['steps']
    wired = [step for step in steps if 'TIDEN_GITHUB_READ_TOKEN' in step.get('env', {})]
    assert len(wired) == 1 and wired[0]['id'] == 'publish'
    assert wired[0]['env']['TIDEN_GITHUB_READ_TOKEN'] == '${{ github.token }}'


# spec: ANYM-322
def test_main_release_validation_uses_its_existing_read_token_too():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text())
    assert workflow['permissions'] == {'contents': 'read'}
    wired = [step for job in workflow['jobs'].values() for step in job.get('steps', [])
             if 'TIDEN_GITHUB_READ_TOKEN' in step.get('env', {})]
    assert len(wired) == 1 and wired[0]['name'] == 'Record the main release in Tiden'
    assert wired[0]['env']['TIDEN_GITHUB_READ_TOKEN'] == '${{ github.token }}'
