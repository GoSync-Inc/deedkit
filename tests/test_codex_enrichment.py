"""Subscription adapter security/format boundaries; no real account in unit tests."""
import json
import subprocess
from pathlib import Path

import pytest

from messaging_service.enrichment_providers import ModelError, infer, validate_profile


def profile(tmp_path, **overrides):
    home=tmp_path/'codex-home';home.mkdir(exist_ok=True,mode=0o700)
    auth=home/'auth.json';auth.write_text('{"auth_mode":"chatgpt","tokens":{}}');auth.chmod(0o600)
    return validate_profile({'provider':'codex_cli','model':'fixture-model','allow_cloud':True,
        'codex_home':str(home),'codex_binary':'/usr/bin/codex','isolated_runtime':True,**overrides},'document')


def fake_runner(monkeypatch, tmp_path, output=None, error=None):
    calls=[]
    def run(command, **kwargs):
        calls.append((command,kwargs))
        assert kwargs['shell'] is False
        assert kwargs['env']['CODEX_HOME']==str(tmp_path/'codex-home')
        assert 'OPENAI_API_KEY' not in kwargs['env'] and 'DATABASE_URL' not in kwargs['env']
        if 'login' in command:
            return subprocess.CompletedProcess(command,0,b'',b'Logged in using ChatGPT\n')
        if error:raise error
        dest=Path(command[command.index('--output-last-message')+1])
        dest.write_text(json.dumps(output or {'title':'Fixture','summary':'Short description',
            'document_type':'test_document','text':'Detailed analysis with source limitations.'}))
        kwargs['stdout'].write(b'{"type":"turn.completed"}\n')
        return subprocess.CompletedProcess(command,0)
    monkeypatch.setenv('OPENAI_API_KEY','ambient-secret')
    monkeypatch.setenv('DATABASE_URL','ambient-database')
    monkeypatch.setattr(subprocess,'run',run)
    return calls


def test_subscription_profile_needs_cloud_opt_in(tmp_path):
    with pytest.raises(ValueError,match='cloud_processing_not_enabled'):
        profile(tmp_path,allow_cloud=False)


@pytest.mark.parametrize('field,value',[('codex_home','relative'),('codex_binary','codex'),
    ('base_url','https://example.invalid'),('api_key_file','/secret')])
def test_subscription_profile_rejects_ambiguous_auth_and_executable(tmp_path,field,value):
    with pytest.raises(ValueError):profile(tmp_path,**{field:value})


def test_subscription_adapter_describes_document_without_tools(monkeypatch,tmp_path):
    p=profile(tmp_path);calls=fake_runner(monkeypatch,tmp_path)
    result=infer(p,'document',tmp_path/'unused-original','application/pdf','Synthetic source text')
    assert result['summary']=='Short description' and result['text'].startswith('Detailed analysis')
    assert result['metadata']['text_kind']=='model_analysis'
    assert result['metadata']['generated_by_model'] is True
    command,kwargs=calls[-1]
    assert '--ignore-user-config' in command and '--ignore-rules' in command and '--ephemeral' in command
    assert command[command.index('--sandbox')+1]=='read-only'
    assert 'features.shell_tool=false' in command and 'features.apps=false' in command
    assert 'features.plugins=false' in command and 'features.hooks=false' in command
    assert 'features.multi_agent=false' in command and 'web_search="disabled"' in command
    assert 'Synthetic source text' in kwargs['input'].decode()
    assert 'Synthetic source text' not in ' '.join(command)
    assert '--output-schema' in command and 'forced_login_method="chatgpt"' in command


def test_subscription_adapter_attaches_only_allowed_image(monkeypatch,tmp_path):
    p=profile(tmp_path);calls=fake_runner(monkeypatch,tmp_path)
    image=tmp_path/'image';image.write_bytes(b'fixture image')
    value=infer(p,'image',image,'image/png')
    assert '--image' in calls[-1][0] and value['metadata']['text_kind']=='visible_text'
    with pytest.raises(ModelError,match='model_image_format_unsupported'):
        infer(p,'image',image,'image/svg+xml')


def test_subscription_does_not_fall_back_to_api_auth(monkeypatch,tmp_path):
    p=profile(tmp_path);calls=[]
    def run(command,**kwargs):
        calls.append(command);return subprocess.CompletedProcess(command,0,b'',b'Logged in using an API key\n')
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(ModelError,match='codex_chatgpt_login_required'):
        infer(p,'document',tmp_path/'unused','application/pdf','text')
    assert len(calls)==1


def test_subscription_auth_cannot_be_symlink_or_world_readable(monkeypatch,tmp_path):
    p=profile(tmp_path);fake_runner(monkeypatch,tmp_path)
    auth=Path(p['codex_home'])/'auth.json';auth.chmod(0o644)
    with pytest.raises(ModelError,match='codex_private_auth_required'):
        infer(p,'document',tmp_path/'unused','application/pdf','text')
    auth.unlink();auth.symlink_to(tmp_path/'not-readable')
    with pytest.raises(ModelError,match='codex_private_auth_required'):
        infer(p,'document',tmp_path/'unused','application/pdf','text')


@pytest.mark.parametrize('output',[{'title':'only'}, {'title':'x','summary':'s','document_type':'t','text':'t','execute':'shell'},
    {'title':1,'summary':'s','document_type':'t','text':'t'}])
def test_subscription_schema_failures_do_not_publish(monkeypatch,tmp_path,output):
    p=profile(tmp_path);fake_runner(monkeypatch,tmp_path,output)
    with pytest.raises(ModelError,match='invalid_model_response'):
        infer(p,'document',tmp_path/'unused','application/pdf','text')


def test_subscription_timeout_redacts_input_and_credentials(monkeypatch,tmp_path):
    p=profile(tmp_path);fake_runner(monkeypatch,tmp_path,error=subprocess.TimeoutExpired('SENSITIVE',1))
    with pytest.raises(ModelError,match='model_timeout') as e:
        infer(p,'document',tmp_path/'unused','application/pdf','PRIVATE')
    assert 'PRIVATE' not in str(e.value) and 'SENSITIVE' not in str(e.value)


def test_subscription_empty_document_not_claimed_as_full_analysis(monkeypatch,tmp_path):
    p=profile(tmp_path);calls=fake_runner(monkeypatch,tmp_path)
    with pytest.raises(ModelError,match='document_text_required'):
        infer(p,'document',tmp_path/'unused','application/pdf','')
    assert calls==[]


def test_subscription_requires_isolated_runtime(tmp_path):
    with pytest.raises(ValueError,match='isolated_subscription_runtime_required'):
        profile(tmp_path,isolated_runtime=False)


@pytest.mark.parametrize('event,code',[
    ({'type':'item.completed','item':{'type':'command_execution'}},'codex_tool_use_rejected'),
    ({'type':'error','message':'PRIVATE: requires a newer version of Codex'},'codex_upgrade_required'),
    ({'type':'error','message':'PRIVATE: you have hit your usage limit'},'model_rate_limited'),
    ({'type':'turn.started'},'codex_run_incomplete')])
def test_subscription_failed_or_tool_using_runs_do_not_publish(monkeypatch,tmp_path,event,code):
    p=profile(tmp_path);original=fake_runner(monkeypatch,tmp_path)
    previous=subprocess.run
    def run(command,**kwargs):
        result=previous(command,**kwargs)
        if 'exec' in command:
            kwargs['stdout'].seek(0);kwargs['stdout'].truncate()
            kwargs['stdout'].write(json.dumps(event).encode()+b'\n')
        return result
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(ModelError,match=code) as error:
        infer(p,'document',tmp_path/'unused','application/pdf','text')
    assert 'PRIVATE' not in str(error.value)


def test_subscription_semantics_survive_worker_revalidation(monkeypatch,tmp_path):
    from messaging_service.enrichment_providers import normalize_result
    p=profile(tmp_path);fake_runner(monkeypatch,tmp_path)
    result=infer(p,'document',tmp_path/'unused','application/pdf','text')
    again=normalize_result(result)
    assert again['metadata']['text_kind']=='model_analysis'
    assert again['metadata']['prompt_version']=='codex-file-description-v1'


# spec: ANYM-311
def test_codex_example_stays_disabled_and_the_doc_lists_the_open_blockers():
    root=Path(__file__).resolve().parents[1]
    assert json.loads((root/'enrichment.codex.example.json').read_text())['enabled'] is False
    doc=' '.join((root/'CODEX_ENRICHMENT.md').read_text().split())
    assert '`enabled: false`' in doc
    for blocker in ('Global and per-account daily counters are shared','sums every profile and provider',
                    'fixed 30 minutes','Per-file retries of errors that do not pause'):
        assert blocker in doc,blocker
