"""Call brief, voice and voice-tool types: what agents and the voice model are shown (split from core.py, W5-7a)."""
import re
from functools import cache
from typing import Annotated,Literal,get_args
from zoneinfo import available_timezones

from pydantic import AfterValidator,BaseModel,ConfigDict,Field,PrivateAttr,field_validator,model_validator

MODEL="gemini-3.8-live"
GROK_MODEL="grok-voice-3.0"


GeminiVoice=Literal["Kore","Puck","Charon","Fenrir","Aoede","Leda","Orus","Zephyr"]
GrokVoice=Literal['eve','ara','rex','sal','leo']
VOICES,GROK_VOICES=list(get_args(GeminiVoice)),list(get_args(GrokVoice))
# An agent's per-call engine (ANYM-584); its table and selector are voice_engines.py.
VoiceEngine=Literal['auto','gemini_live','grok_live']
CustomVoiceId=Annotated[str|None,Field(pattern=r'^voice_[A-Za-z0-9_]{4,100}$')]


@cache
def _zones():
    # Area/Location keys and UTC only: localtime, posixrules and Factory are host files, not places.
    # Etc/GMT+N means UTC-N (POSIX sign), so an agent's "GMT+7" would land 14 hours off.
    return frozenset(z for z in available_timezones() if '/' in z and not z.startswith('Etc/GMT') or z=='UTC')


def _zone(value):
    if value not in _zones():raise ValueError('timezone must be an IANA zone such as Asia/Makassar')
    return value


TimeZone=Annotated[str|None,Field(min_length=1,max_length=64),AfterValidator(lambda v:v if v is None else _zone(v))]
VoiceStyle=Literal['neutral','friendly']


# What an agent may set; the voice is the owner's call_voice (ANYM-359). No docstrings: they enter the schema.
class AgentBrief(BaseModel):
    model_config=ConfigDict(extra="forbid")
    goal:str=Field(min_length=3,max_length=1500)
    counterparty:str=Field(min_length=1,max_length=200)
    language:Literal["ru","en","id"]="ru"
    owner_notes:str=Field(default="",max_length=4000)
    supervisor_notes:str=Field(default="",max_length=4000)
    source_message_ids:list[str]=Field(default_factory=list,max_length=8)
    success_fields:list[str]=Field(min_length=1,max_length=8)
    approval_fields:list[str]=Field(default_factory=list,max_length=8)
    conversation_mode:Literal['task','social']='task'
    self_reference:Literal["feminine","masculine","neutral"]="masculine"
    max_duration_secs:int=Field(default=600,ge=30,le=600)
    timezone:TimeZone=None
    voice_engine:VoiceEngine|None=None


# Owner rehearsals choose their own engine and voice.
class BriefInput(AgentBrief):
    voice_engine:None=None
    _voice_engine:dict|None=PrivateAttr(default=None)
    voice:GeminiVoice="Puck"
    custom_voice_id:CustomVoiceId=None
    voice_style:VoiceStyle='neutral'
    dialog_engine:Literal['gemini_live','gemini_live_flows','grok_realtime']='gemini_live'
    grok_voice:GrokVoice='rex'


# The owner's one voice for every agent-prepared call. Unset means today's Gemini defaults.
class CallVoice(BaseModel):
    model_config=ConfigDict(extra="forbid")
    provider:Literal['gemini_live','grok_realtime']='gemini_live'
    gemini_voice:GeminiVoice='Puck'
    grok_voice:GrokVoice='rex'
    gemini_custom_voice_id:CustomVoiceId=None
    voice_style:VoiceStyle='neutral'

    @model_validator(mode='after')
    def native_grok(self):
        if self.provider=='grok_realtime' and self.gemini_custom_voice_id:
            raise ValueError('grok_native_voice_required')
        return self

    def brief_fields(self):
        return {'dialog_engine':self.provider,'voice':self.gemini_voice,'grok_voice':self.grok_voice,
                'custom_voice_id':self.gemini_custom_voice_id,'voice_style':self.voice_style}

    def summary(self):
        """For agents: no custom voice ID; a replicated voice is not reported as the prebuilt one."""
        grok,custom=self.provider=='grok_realtime',bool(self.gemini_custom_voice_id)
        return {'provider':self.provider,'model':GROK_MODEL if grok else MODEL,
                'voice':self.grok_voice if grok else 'replicated' if custom else self.gemini_voice,'custom_voice':custom}


class ToolInput(BaseModel):
    model_config=ConfigDict(extra="forbid")


class Lookup(ToolInput):
    query:str=Field(default="",max_length=200)


class Answer(ToolInput):
    field:str=Field(max_length=64)
    value:str=Field(min_length=1,max_length=1000)
    evidence:str=Field(min_length=1,max_length=1000)


class Decision(ToolInput):
    question:str=Field(min_length=3,max_length=1000)
    options:list[str]=Field(min_length=2,max_length=4)


class Finish(ToolInput):
    summary:str=Field(min_length=1,max_length=2000)


class EndConversation(ToolInput):
    reason:Literal['completed','needs_owner','refused','unavailable']
    summary:str=Field(min_length=1,max_length=2000)
    evidence:str=Field(default='',max_length=1000)


class Consult(ToolInput):
    question:str=Field(min_length=3,max_length=1000)


class Keys(ToolInput):
    # A menu choice only (ANYM-428): no code, PIN or number fits in three keys.
    keys:str=Field(min_length=1,max_length=3)
    menu_quote:str=Field(min_length=1,max_length=500)

    @field_validator('keys')
    @classmethod
    def keypad(cls,value):
        if not re.fullmatch(r'[0-9*#]{1,3}',value): raise ValueError('invalid_keys')
        return value


BASE_TOOL_MODELS={"lookup_context":Lookup,"record_answer":Answer,"request_owner_decision":Decision,"consult_supervisor":Consult,
                  "finish_task":Finish,"end_conversation":EndConversation}
TOOL_MODELS={**BASE_TOOL_MODELS,"press_keys":Keys}
# Flows keeps its fixed tool set (ANYM-375): no keypad.
FLOW_TOOL_MODELS={**BASE_TOOL_MODELS,'propose_closing':Finish}
# In-band tones reach automated menus over Twilio media streams; the browser rehearsal plays them to the owner.
KEYPAD_CHANNELS={'phone','sip','browser'}
