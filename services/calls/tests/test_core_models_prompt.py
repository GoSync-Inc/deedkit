"""W5-7a pin: the brief and tool types live in models.py, the prompt and the call clock in prompt.py, and core.py
re-exports the same objects. Every `from core import ...` keeps working, and the saved prompt, the clock line and the
agent-facing schemas stay byte-equal to the pre-split main (783de5b9)."""
import hashlib
import json
from datetime import datetime, timezone

import pytest

import core

MODEL_NAMES = ('MODEL', 'GROK_MODEL', 'GeminiVoice', 'GrokVoice', 'VOICES', 'GROK_VOICES', 'VoiceEngine', 'CustomVoiceId',
               'TimeZone', 'VoiceStyle', 'AgentBrief', 'BriefInput', 'CallVoice', 'ToolInput', 'Lookup', 'Answer',
               'Decision', 'Finish', 'EndConversation', 'Consult', 'Keys', 'BASE_TOOL_MODELS', 'TOOL_MODELS',
               'FLOW_TOOL_MODELS', 'KEYPAD_CHANNELS')
PROMPT_NAMES = ('GREETINGS', 'call_clock', 'build_prompt')
BASE = {'goal': 'Confirm the delivery window', 'counterparty': 'Shop "Ось"', 'facts': [{'id': 'm1', 'text': 'order 42'}],
        'success_fields': ['window'], 'language': 'ru', 'self_reference': 'masculine'}
# Case: (brief fields over BASE, learned instruction, sha256 of build_prompt at 783de5b9).
PROMPTS = {
    'ru-masculine-browser': ({}, '', '6207cc312c1503ce914c04562a1fc475050a9fe1392c66e85961038bca3340a2'),
    'ru-feminine-phone': ({'self_reference': 'feminine', 'channel': 'phone'}, '',
                          '748efbf1becbf983bf0810bc8917fb1a1cd665ade8f0d345a93a03919e46329a'),
    'ru-neutral-sip-social': ({'self_reference': 'neutral', 'channel': 'sip', 'conversation_mode': 'social'}, '',
                              '5db7c88ca1ea69a64aab91dcc91e7a985e0e7ba9305ec7c8b0dad2df4641d840'),
    'en-telegram-friendly': ({'language': 'en', 'channel': 'telegram', 'voice_style': 'friendly'}, '',
                             '5bfa8ae1f730d1491cfebbad829363b381b51383c943315c65696918ba0f5141'),
    'id-custom-voice-followup': ({'language': 'id', 'custom_voice_id': 'voice_abcd1234',
                                  'followup': {'owner_answer': 'yes', 'conditions': 'fee 5'}}, '',
                                 '0602b19bccdf321ac94252edea4d20d14173af3107e28fe769290fcdb0e15c40'),
    'ru-approval-instruction': ({'approval_fields': ['window']}, 'Keep turns short.',
                                '4290fc1926ad00f3df37f0986a99145e178e08eb54ad0c2e72f212eb205f803b'),
    'flows-ru': ({'dialog_engine': 'gemini_live_flows', 'approval_fields': [], 'conversation_mode': 'task'},
                 'Learned line.', '0fc47008682ad34634d23015741865087da83d4dc9914c57739fc7c51b5db345'),
}
AT = datetime(2026, 11, 10, 6, 30, tzinfo=timezone.utc)
# Case: (brief, call_clock(brief, AT) at 783de5b9).
CLOCKS = {
    'ru-no-zone': ({'language': 'ru'}, "The counterparty's local time is unknown: greet with 'Здравствуйте' and use no "
                                       'morning, afternoon or evening greeting.'),
    'id-makassar': ({'language': 'id', 'timezone': 'Asia/Makassar'},
                    'Counterparty local time at call start: Tuesday 2026-11-10 14:30 (Asia/Makassar). If you greet by '
                    "time of day, a time-of-day greeting must be 'Selamat siang'; never another one."),
    'ru-moscow': ({'language': 'ru', 'timezone': 'Europe/Moscow'},
                  'Counterparty local time at call start: Tuesday 2026-11-10 09:30 (Europe/Moscow). If you greet by '
                  "time of day, a time-of-day greeting must be 'Доброе утро'; never another one."),
    'en-new-york-night': ({'language': 'en', 'timezone': 'America/New_York'},
                          'Counterparty local time at call start: Tuesday 2026-11-10 01:30 (America/New_York). It is '
                          "night there: greet with 'Hello' and use no time-of-day greeting."),
    'en-tokyo': ({'language': 'en', 'timezone': 'Asia/Tokyo'},
                 'Counterparty local time at call start: Tuesday 2026-11-10 15:30 (Asia/Tokyo). If you greet by time '
                 "of day, a time-of-day greeting must be 'Good afternoon'; never another one."),
    'legacy-language-zone': ({'language': 'de', 'timezone': 'UTC'},
                             'Counterparty local time at call start: Tuesday 2026-11-10 06:30 (UTC). Use no '
                             'time-of-day greeting.'),
    'legacy-language-no-zone': ({'language': 'de'},
                                "The counterparty's local time is unknown: use no morning, afternoon or evening greeting."),
}
# (sha256 of the model's JSON schema with sort_keys at 783de5b9, model): what agents and the voice model are shown.
# Digest first: gitleaks reads a hex string right after a name like press_keys as an API key.
SCHEMAS = [
    ('a7df27b016ddd6377c137741cf0f639d66a7900417d4b5e8647f02162d2eec31', 'AgentBrief'),
    ('4b6e6cb184625d2e7b92f1a42ccd21e656315f25ce8ddb38ec099b9c55db14d7', 'BriefInput'),
    ('467ec6ddb241026f3d5a74c2deb27d66ec34102d237a182a25e63ba0ae3f9f8a', 'CallVoice'),
    ('54e23753486f78c7493c2f4a72a85b62027100d2a8cca7929245058e3165ce46', 'tool:lookup_context'),
    ('1b2089076ac0e7e4a23c38b73a3120c18c7b95546d2c679db17d7e221f7b375c', 'tool:record_answer'),
    ('7cd59e601e3ec9a69a0d63dd5f59a7db456d0879af79e5dbf5427b80f3f58dee', 'tool:request_owner_decision'),
    ('4904bbb6e561a4ba4484aa4df60822703274db0961557e1de0f5e4cb2d62d3a2', 'tool:consult_supervisor'),
    ('8866686221933da52a28ae4d02290f2c3fd2fb5caae26659654a8a5df7f389a9', 'tool:finish_task'),
    ('d31b05e600c6a0007b913159bc390e9b6fb8757c1846a2d26e93033b6b1a0ee2', 'tool:end_conversation'),
    ('af7c0e6f3b89f3b7c4d29ff93c59325cf9cf9c6958e8e33ba3491114c5fc8931', 'tool:press_keys'),
    ('8866686221933da52a28ae4d02290f2c3fd2fb5caae26659654a8a5df7f389a9', 'tool:propose_closing'),
]


# spec: ANYM-239
@pytest.mark.parametrize('name', MODEL_NAMES)
def test_core_reexports_the_models_object(name):
    import models
    assert getattr(core, name) is getattr(models, name)


# spec: ANYM-239
@pytest.mark.parametrize('name', PROMPT_NAMES)
def test_core_reexports_the_prompt_object(name):
    import prompt
    assert getattr(core, name) is getattr(prompt, name)


# spec: ANYM-239
@pytest.mark.parametrize('case', PROMPTS)
def test_the_saved_prompt_is_byte_equal_to_the_pre_split_main(case):
    import prompt
    extra, instruction, digest = PROMPTS[case]
    assert hashlib.sha256(prompt.build_prompt({**BASE, **extra}, instruction).encode()).hexdigest() == digest


# spec: ANYM-239
@pytest.mark.parametrize('case', CLOCKS)
def test_the_clock_line_is_byte_equal_to_the_pre_split_main(case):
    import prompt
    brief, line = CLOCKS[case]
    assert prompt.call_clock(brief, AT) == line


# spec: ANYM-239
@pytest.mark.parametrize(('digest', 'name'), SCHEMAS, ids=[name for _, name in SCHEMAS])
def test_the_agent_facing_schemas_are_byte_equal_to_the_pre_split_main(digest, name):
    import models
    model = (models.TOOL_MODELS | models.FLOW_TOOL_MODELS)[name[5:]] if name.startswith('tool:') else getattr(models, name)
    text = json.dumps(model.model_json_schema(), sort_keys=True, ensure_ascii=False)
    assert hashlib.sha256(text.encode()).hexdigest() == digest


# spec: ANYM-239
def test_a_zone_the_host_cannot_load_reads_as_unknown_in_the_prompt_module(monkeypatch):
    import prompt
    def missing(key):
        raise prompt.ZoneInfoNotFoundError(key)
    monkeypatch.setattr(prompt, 'ZoneInfo', missing)
    assert prompt.call_clock({'language': 'id', 'timezone': 'Asia/Makassar'}, AT).startswith(
        "The counterparty's local time is unknown: greet with 'Halo'")
