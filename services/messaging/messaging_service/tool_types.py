from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Required, TypedDict

from pydantic import AfterValidator, ConfigDict, Field, StrictBool, StrictInt, StrictStr

WaitSeconds = Annotated[float, Field(strict=True,ge=0,le=25,allow_inf_nan=False)]
LiveWaitSeconds = Annotated[float, Field(strict=True,ge=0,le=20,allow_inf_nan=False)]
WakeRule = Literal['incoming_message','owner_mention','reply_to_owner','reply_to_confirmed_send','reply_to_own_send']
WakeRules = Annotated[list[WakeRule], Field(min_length=1,max_length=4)]
SendReference = Annotated[StrictStr, Field(min_length=1,max_length=128)]
SubscriptionReceipt = Annotated[StrictStr, Field(min_length=32,max_length=128)]
ReceiverReference = Annotated[StrictStr, Field(pattern=r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]
RequestReference = Annotated[StrictStr, Field(min_length=8,max_length=128,pattern=r'^[A-Za-z0-9_.:-]+$')]
FeedbackReference = Annotated[StrictStr, Field(min_length=1,max_length=256,pattern=r'^[^\x00-\x1f\x7f]+$')]
FeedbackReferences = dict[Literal['message_ids','chat_ids','subscription_ids'],Annotated[list[FeedbackReference],Field(max_length=32)]]
FeedbackDescription = Annotated[StrictStr,Field(min_length=1,max_length=4000,pattern=r'^[^\x00-\x08\x0b-\x1f\x7f]+$')]
# A tool name as tools/list shows it (feedback.py canonical_operation, ANYM-342). The name list is not repeated
# here: the gateway owns the catalog, so the schema carries the shape only.
FeedbackOperation = Annotated[StrictStr,Field(min_length=1,max_length=129,pattern=r'^[a-z][a-z0-9_]*$')]
FeedbackStatus = Literal['received','accepted','declined','duplicate','fixed','verify_requested','verified','reopened']
Detail = Literal['compact','full']
MessageDetail = Literal['compact','full','transcript']
ConversationView = Literal['archive','conversation']
QUERY_MODE_DESCRIPTION = 'Five modes, one line each:\nall (default): every word, any order.\nany: at least one of the words (OR), no syntax to learn; best for spelling variants and transliteration, e.g. `счёт счет invoice`.\nphrase: the words adjacent and in order.\nweb: `"exact phrase" OR other -excluded`; or/OR in any case is the operator, so the word `or` itself cannot be searched in web; for a literal `or`, a leading `-` or quotes as text use any or exact; for phrases and exclusion use web.\nexact: case-insensitive literal substring, including punctuation and identifiers.\nWith any and web prefer order=relevance, because asc lists the oldest hits first.\nLive search applies to all and phrase only; for a live search of several words run one live search per word.'
QueryMode = Annotated[Literal['all','any','phrase','web','exact'], Field(description=QUERY_MODE_DESCRIPTION)]
# ANYM-661: values are checked by search_files itself, so a bad one (also a wrong JSON type) is invalid_filter with
# field_errors naming the field, not invalid_parameters from the pydantic model; the published schema stays array|null.
LIST_OR_NULL = {'anyOf': [{'type': 'array', 'items': {'type': 'string'}}, {'type': 'null'}]}
MediaKindFilter = Annotated[Any, Field(json_schema_extra=LIST_OR_NULL, description=
    'Narrow to files of these media kinds: a list of voice, audio, image, video, document (a file is voice for a Telegram '
    'or WhatsApp voice note, and for audio/ogg, audio/opus or application/ogg without a provider label); provider labels '
    'shown in media_kind are accepted too: photo and sticker are image, animation is video, other is document. '
    'Values are OR-ed; an empty list is no filter. Each row returns its class as media_class. '
    'For voice notes across accounts use ["voice","audio"].')]
ReadyFilter = Annotated[Any, Field(json_schema_extra=LIST_OR_NULL, description=
    'Narrow to files whose derived content is ready, a list: ready=["transcript"] (voice notes with a transcript). '
    'Values text (extracted text or OCR), transcript (speech of an audio or voice file only), description '
    '(model description of a non-audio file or an understanding); OR-ed. ready=["none"] alone: files with none of the '
    'three yet, e.g. voice notes still without a transcript. Counts the current revision only; each page is a snapshot. '
    'A match for an empty page: repeat without ready and read the per-row availability fields.')]
SearchLanguage = Literal['auto','simple','russian','english','indonesian']
EventKind = Literal['message.created','message.edited','message.deleted','message.status','attachment.ready']
SubscriptionEventKind = Literal['message.created','message.edited','message.deleted',
    'message.status','attachment.ready','file.text.ready','file.transcript.ready',
    'file.description.ready','file.processing.failed','send.failed','source.health']
EventOrigin = Literal['live','backfill','reconcile','fixture']
MessageFormat = Literal['plain','markdown','html']
SubscriptionReference = Annotated[StrictStr, Field(min_length=1,max_length=256,
    pattern=r'^[^\x00-\x20\x7f]+$')]
SubscriptionReferences = Annotated[list[SubscriptionReference], Field(min_length=1,max_length=64)]
SubscriptionKinds = Annotated[list[SubscriptionEventKind], Field(min_length=1,max_length=11)]
SubscriptionOrigins = Annotated[list[EventOrigin], Field(min_length=1,max_length=4)]
SubscriptionLimit = Annotated[StrictInt, Field(ge=1,le=100)]
SubscriptionVersion = Annotated[StrictInt, Field(ge=1,le=9007199254740991)]
SubscriptionPosition = Annotated[StrictInt, Field(ge=0,le=9007199254740991)]
# Hint timing only (delivery_policy.py validates again): never changes matches, ACK or passive reads.
LocalClock = Annotated[StrictStr, Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]
class QuietWindow(TypedDict):
    __pydantic_config__ = ConfigDict(extra='forbid')
    days: Annotated[list[Literal['mon','tue','wed','thu','fri','sat','sun']], Field(min_length=1,max_length=7)]
    start: LocalClock
    end: LocalClock
class QuietHours(TypedDict):
    __pydantic_config__ = ConfigDict(extra='forbid')
    timezone: Annotated[StrictStr, Field(min_length=1,max_length=64)]
    windows: Annotated[list[QuietWindow], Field(min_length=1,max_length=4)]
class DeliveryPolicy(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra='forbid')
    debounce_s: Annotated[StrictInt, Field(ge=0,le=600)]
    max_wait_s: Annotated[StrictInt, Field(ge=1,le=3600)]
    batch_window_s: Annotated[StrictInt, Field(ge=0,le=86400)]
    batch_count: Annotated[StrictInt, Field(ge=1,le=1000)]
    quiet_hours: QuietHours
    quiet_bypass: Annotated[list[Literal['incoming_message','owner_mention','reply_to_owner',
        'reply_to_confirmed_send','silence']], Field(min_length=1,max_length=5)]
    expect_reply_within_s: Annotated[StrictInt, Field(ge=60,le=1209600)]
# ANYM-519: an agent's own webhook endpoint. delivery_endpoints.py checks URL, secret and headers again.
EndpointReference = ReceiverReference
EndpointUrl = Annotated[StrictStr, Field(min_length=1,max_length=2048)]
SigningSecret = Annotated[StrictStr, Field(pattern=r'^whsec_[A-Za-z0-9+/=]{32,96}$')]
EndpointHeaders = Annotated[dict[StrictStr,StrictStr], Field(max_length=4)]
EndpointLabel = Annotated[StrictStr, Field(max_length=128,pattern=r'^[^\x00-\x1f\x7f]*$')]
# ANYM-520: one page of an endpoint's attempt log.
AttemptLimit = Annotated[StrictInt, Field(ge=1,le=50)]
class SubscriptionSources(TypedDict, total=False):
    # ANYM-522: every chat the key can see now, optionally per account; channels and bots only when asked for.
    __pydantic_config__ = ConfigDict(extra='forbid')
    all_visible_chats: Required[Literal[True]]
    account_ids: SubscriptionReferences
    include_channels: StrictBool
    include_bots: StrictBool
class SubscriptionEndpoint(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra='forbid')
    endpoint_id: Required[EndpointReference]
    delivery_policy: DeliveryPolicy


def subscription_expiry(value):
    """Validate a zoned ISO timestamp without coercing or echoing input values."""
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:raise ValueError
    except ValueError:
        raise ValueError('invalid_subscription_expiry') from None
    return value


SubscriptionExpiry = Annotated[StrictStr, Field(min_length=20,max_length=64,
    pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$'),
    AfterValidator(subscription_expiry)]
