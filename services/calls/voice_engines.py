"""Per-call voice engine choice (ANYM-584). One table and one selector for every agent call surface.

An agent may name the engine of one call; the voice inside it stays the owner's call_voice (ANYM-359).
A call's audio goes only to the selected engine's vendor: never a fallback to another one."""
from core import GROK_MODEL, GROK_VOICES, MODEL, VOICES, ServiceError, VoiceEngine

__all__=['ENGINES','VoiceEngine','brief_fields','capabilities','choose']

# Fixed order: auto takes the owner's call_voice engine first, then the first fitting engine here.
ENGINES={
    'gemini_live':{'vendor':'google','dialog_engine':'gemini_live','key':'gemini','model':MODEL,'voices':VOICES,
                   'languages':['ru','en','id'],'tool_calls':True,'success_fields':True},
    'grok_live':{'vendor':'xai','dialog_engine':'grok_realtime','key':'xai','model':GROK_MODEL,'voices':GROK_VOICES,
                 'languages':['ru','en','id'],'tool_calls':True,'success_fields':True},
}
BY_DIALOG_ENGINE={e['dialog_engine']:name for name,e in ENGINES.items()}


def capabilities(credentials,provider):
    """Read-only listing for calls_capabilities: key presence only, never a key."""
    return {'engines':[{'name':name,'dialog_engine':e['dialog_engine'],'vendor':e['vendor'],
                        'available':bool(credentials.get(e['key'])),'model':e['model'],
                        'voices':list(e['voices']),'languages':list(e['languages']),'tool_calls':e['tool_calls'],
                        'success_fields':e['success_fields']} for name,e in ENGINES.items()],
            'omitted_uses':BY_DIALOG_ENGINE.get(provider),'voice':'owner_call_voice',
            # Whatever engine speaks, the in-call supervisor and the after-call analysis send the call record to Gemini
            # when its key is set: the brief with its prompt, facts, transcript, answers, decisions and tool calls.
            'transcript_analysis_vendor':'google',
            'transcript_analysis_input':['brief','facts','transcript','answers','decisions','tool_calls']}


def unfit(name,language,credentials):
    """Why an engine cannot speak this brief, or None. Every agent brief needs the task tools and success_fields.
    language None: no brief yet (calls readiness, ANYM-595); the language is checked when the brief is prepared."""
    e=ENGINES[name]
    if not credentials.get(e['key']):return 'key_not_configured'
    if language is not None and language not in e['languages']:return 'language_unsupported'
    if not e['tool_calls']:return 'tool_calls_unsupported'
    if not e['success_fields']:return 'success_fields_unsupported'
    return None


def auto_order(provider):
    """auto's order: the owner's call_voice engine first, then ENGINES order; unknown owner (unreadable setting): ENGINES order."""
    owner=BY_DIALOG_ENGINE.get(provider)
    return [*([owner] if owner else []),*(n for n in ENGINES if n!=owner)]


def choose(requested,language,provider,credentials):
    """The engine of one agent call: {requested, selected, reason, skipped}, or a refusal before anything is saved.
    None keeps today's behaviour: the owner's call_voice engine, unchecked here (preflight reports a missing key)."""
    owner=BY_DIALOG_ENGINE.get(provider)
    if requested is None:
        return {'requested':None,'selected':BY_DIALOG_ENGINE[provider],'reason':'owner_call_voice','skipped':{}}
    if requested!='auto':
        reason=unfit(requested,language,credentials)
        if reason:
            raise ServiceError('voice_engine_unavailable',503,{'voice_engine':requested,'reason':reason})
        return {'requested':requested,'selected':requested,'reason':'requested','skipped':{}}
    skipped={}
    for name in auto_order(provider):
        reason=unfit(name,language,credentials)
        if reason is None:
            return {'requested':'auto','selected':name,'reason':'auto_owner_engine' if name==owner else 'auto_first_fitting',
                    'skipped':skipped}
        skipped[name]=reason
    raise ServiceError('no_voice_engine_available',503,{'skipped':skipped})


def brief_fields(call_voice,choice):
    """The owner's voice fields for the selected engine. Grok has no replicated Gemini voice, so it gets none."""
    fields={**call_voice.brief_fields(),'dialog_engine':ENGINES[choice['selected']]['dialog_engine']}
    if choice['selected']=='grok_live':fields['custom_voice_id']=None
    return fields
