"""Principal-scoped configuration diagnostics. No probes, calls or archive reads."""
from core import ServiceError
from outbound import TwilioCalls
import destination_policy as policy


def calling_readiness(principal, outbound, credentials, browser_enabled):
    channels = {}
    for channel in principal.channels:
        blockers = []
        if channel == 'browser':
            if not browser_enabled:
                blockers.append('use_local_browser_rehearsal')
            if 'calls:start' not in principal.scopes:
                blockers.append('calls:start_required')
            if not any(credentials.values()):
                blockers.append('model_key_required')
            channels[channel] = {'configuration_ready':not blockers, 'blockers':blockers,
                                 'live_call_verified':False}
            continue
        if 'calls:dial' not in principal.scopes:
            blockers.append('calls:dial_required')
        if not outbound.settings.enabled:
            blockers.append('calling_disabled')
        # Only SIP keeps a recipient list (ANYM-288).
        recipients = set(principal.destinations.get(channel, [])) & set(outbound.settings.allowed.get(channel, []))
        if channel == 'sip' and not recipients:
            blockers.append('recipient_not_allowed')
        remaining = outbound.daily_calls_remaining()
        if not remaining:
            blockers.append('daily_call_limit')
        supported = ['gemini', 'xai'] if channel in {'phone','sip'} else ['gemini']
        if not any(credentials.get(name) for name in supported):
            blockers.append('model_key_required')
        provider = outbound.providers.get('telegram' if channel == 'telegram' else 'twilio')
        if provider is None:
            blockers.append('provider_not_configured')
        elif isinstance(provider, TwilioCalls):
            try:
                provider.configuration({'channel':channel, 'destination':''})
            except ServiceError as error:
                blockers.append(error.code)
        channels[channel] = {'configuration_ready':not blockers, 'blockers':blockers,
            'recipient_policy':policy.recipient_policy(channel), 'daily_calls_remaining':remaining,
            **({'allowed_recipient_count':len(recipients)} if channel == 'sip' else {}), 'model_providers':supported,
            'provider_connection':'not_probed', 'preflight_required':True, 'live_call_verified':False}
    return {'model_credentials':dict(credentials), 'channels':channels,
            'external_action_authorized':False}
