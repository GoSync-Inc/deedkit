"""Who may be called without recipient lists (ANYM-288).

The owner's task authorizes a call; the destination may be one the owner named or one the
agent found for that task. Telephone numbers are fenced locally by libphonenumber
classification and every external dial spends units of a durable daily cap. Telegram user
IDs are not phone numbers and are not classified; SIP keeps its exact allowlist.
"""
import re

import phonenumbers
from phonenumbers import PhoneNumberType, shortnumberinfo

from core import ServiceError

DAILY_UNITS=10
PREMIUM_UNITS=3
TELEPHONE={'phone','whatsapp'}
PREMIUM={PhoneNumberType.PREMIUM_RATE,PhoneNumberType.SHARED_COST,PhoneNumberType.UAN}
SEPARATORS=re.compile(r'[\s().-]')
CALL_YOURSELF='Emergency and short-code calls are not supported. Tell the owner to call {} themselves right now.'


def recipient_policy(channel):
    return 'exact_allowlist' if channel=='sip' else 'any_valid_destination'


def _emergency(digits,region=None):
    regions=[region] if region else phonenumbers.SUPPORTED_SHORT_REGIONS
    return any(shortnumberinfo.is_emergency_number(digits,r) for r in regions)


def _call_yourself(digits):
    code='emergency_number_unsupported' if _emergency(digits) else 'short_code_unsupported'
    return ServiceError(code,422,{'message':CALL_YOURSELF.format(digits)})


def unsupported(channel,destination):
    """Fail fast on a short telephone dial string before any other validation."""
    if channel not in TELEPHONE or not isinstance(destination,str):
        return
    digits=SEPARATORS.sub('',destination.strip())
    if re.fullmatch(r'\+?[0-9]{1,6}',digits):
        raise _call_yourself(digits.lstrip('+'))


def dial_units(channel,destination):
    """Units one call to this destination spends, or a refusal. No provider contact."""
    if channel not in TELEPHONE:
        return 1
    unsupported(channel,destination)
    try:
        number=phonenumbers.parse(destination,None)
    except phonenumbers.NumberParseException:
        raise ServiceError('destination_blocked',422,{'category':'invalid'}) from None
    national=str(number.national_number)
    if _emergency(national,phonenumbers.region_code_for_number(number)):
        raise _call_yourself(national)
    kind=phonenumbers.number_type(number)
    if not phonenumbers.is_valid_number(number) or kind==PhoneNumberType.UNKNOWN:
        raise ServiceError('destination_blocked',422,{'category':'invalid'})
    return PREMIUM_UNITS if kind in PREMIUM else 1


def _named_forms(channel,destination):
    """(international form, national forms) in which the owner may write this destination."""
    if channel not in TELEPHONE:
        return destination,set()
    try:
        number=phonenumbers.parse(destination,None)
        national=phonenumbers.format_number(number,phonenumbers.PhoneNumberFormat.NATIONAL)
        return destination.lstrip('+'),{SEPARATORS.sub('',national)}
    except phonenumbers.NumberParseException:
        return destination.lstrip('+'),set()


def _written(form,instruction,national):
    # Separators may sit between the digits, but the form must not be part of a longer
    # digit sequence: "+1 (202) 555-0199 (2 min)" names it, "...01992" does not.
    digits=r'[\s().-]*'.join(map(re.escape,form))
    for match in re.finditer(r'(?<![0-9])\+?'+digits+r'(?![0-9])',instruction):
        before=instruction[:match.start()].rstrip(' \t\n().-')
        # A national form right after an explicit country code belongs to another number:
        # "+91 202 555 0199" does not name +1 202 555 0199.
        if national and re.search(r'\+[0-9]{1,3}$',before):
            continue
        return True
    return False


def owner_named(instruction,channel,destination):
    """True when the owner's own words literally contain this destination."""
    if channel=='sip':
        return destination.casefold() in instruction.casefold()
    international,nationals=_named_forms(channel,destination)
    return _written(international,instruction,False) or any(_written(form,instruction,True) for form in nationals)


def bind_origin(brief,instruction,source,evidence):
    """Check and return the destination's origin before any call row exists."""
    evidence=(evidence or '').strip()
    if source=='owner_named':
        if not owner_named(instruction,brief['channel'],brief['destination']):
            raise ServiceError('destination_not_in_owner_instruction',422)
    elif source=='agent_found':
        if not 3<=len(evidence)<=300:
            raise ServiceError('destination_evidence_required',422)
    else:
        raise ServiceError('destination_source_required',422)
    return {'destination_source':source,'destination_evidence':evidence or None}
