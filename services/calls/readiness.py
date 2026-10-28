"""Principal-scoped configuration diagnostics. No probes, calls or archive reads."""
from core import ServiceError
from outbound import TwilioCalls
import destination_policy as policy
import voice_engines


def calling_readiness(principal, outbound, credentials, browser_enabled, provider='gemini_live'):
    # The owner's call_voice provider speaks on every agent call (ANYM-359); None: the setting is unreadable.
    key = {'gemini_live':'gemini', 'grok_realtime':'xai'}.get(provider)
    channels = {}
    for channel in principal.channels:
        blockers = []
        if channel == 'browser':
            if not browser_enabled:
                blockers.append('use_local_browser_rehearsal')
            if 'calls:start' not in principal.scopes:
                blockers.append('calls:start_required')
            if key is None:
                blockers.append('call_voice_invalid')
            elif not credentials.get(key):
                blockers.append('model_key_required')
            channels[channel] = {'configuration_ready':not blockers, 'blockers':blockers,
                                 'model_providers':['gemini', 'xai'], 'live_call_verified':False,
                                 **per_engine(blockers, credentials, provider)}
            continue
        if 'calls:dial' not in principal.scopes:
            blockers.append('calls:dial_required')
        if not outbound.settings.enabled:
            blockers.append('calling_disabled')
        # Only SIP keeps a recipient list (ANYM-288).
        recipients = set(principal.destinations.get(channel, [])) & set(outbound.settings.allowed.get(channel, []))
        if channel == 'sip' and not recipients:
            blockers.append('recipient_not_allowed')
        # An exhausted cap is not a configuration fault: preflight and dial report daily_call_limit.
        remaining = outbound.daily_calls_remaining()
        supported = ['gemini', 'xai']
        if key is None:
            blockers.append('call_voice_invalid')
        elif not credentials.get(key):
            blockers.append('model_key_required')
        connection = outbound.providers.get('telegram' if channel == 'telegram' else 'twilio')
        if connection is None:
            blockers.append('provider_not_configured')
        elif isinstance(connection, TwilioCalls):
            try:
                connection.configuration({'channel':channel, 'destination':''})
            except ServiceError as error:
                blockers.append(error.code)
        channels[channel] = {'configuration_ready':not blockers, 'blockers':blockers,
            'recipient_policy':policy.recipient_policy(channel), 'daily_calls_remaining':remaining,
            **({'allowed_recipient_count':len(recipients)} if channel == 'sip' else {}), 'model_providers':supported,
            'provider_connection':'not_probed', 'preflight_required':True, 'live_call_verified':False,
            **per_engine(blockers, credentials, provider)}
    return {'model_credentials':dict(credentials), 'channels':channels,
            'external_action_authorized':False}


def per_engine(blockers, credentials, provider):
    """Readiness for an explicit voice_engine and for auto (ANYM-595): the channel's engine-independent blockers plus
    each engine's own reason; the owner's-engine blockers above judge only an omitted voice_engine."""
    common = [b for b in blockers if b not in ('model_key_required', 'call_voice_invalid')]
    reasons = {name:voice_engines.unfit(name, None, credentials) for name in voice_engines.ENGINES}
    engines = {name:{'ready':not common and reason is None, 'blockers':[*common, *([reason] if reason else [])]}
               for name,reason in reasons.items()}
    selects = next((name for name in voice_engines.auto_order(provider) if reasons[name] is None), None)
    return {'voice_engines':engines, 'ready_engines':[name for name,e in engines.items() if e['ready']],
            'auto':{'ready':bool(selects) and engines[selects]['ready'], 'selects':selects}}
