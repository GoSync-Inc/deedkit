"""Subscription and delivery tools: subscriptions, event pages and ACK, webhook endpoints and wake targets (moved
from app.py, W5-2b).

Signatures and docstrings are the published tool schemas and descriptions; the canonical contract snapshot
(tests/platform/fixtures/messaging-contracts.json) pins them byte for byte.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field, StrictBool, StrictStr

from ..tool_types import (AttemptLimit, DeliveryPolicy, EndpointHeaders, EndpointLabel, EndpointReference, EndpointUrl,
    ExcludedChats, ExclusionsRevision, ReceiverReference, RequestReference, SendReference, SigningSecret,
    SubscriptionEndpoint, SubscriptionExpiry, SubscriptionFeedback, SubscriptionKinds, SubscriptionLimit, SubscriptionOrigins,
    SubscriptionPageDetail, SubscriptionPageInclude, SubscriptionPosition, SubscriptionReceipt, SubscriptionReference,
    SubscriptionReferences, SubscriptionSources, SubscriptionVersion, WaitSeconds, WakeConfirmationCode, WakeRules,
    WakeTargetBudget, WakeTargetLabel, WakeTargetReference, WakeTargetSubscriptions, WakeThread)


def register_subscription_tools(mcp, call, async_call, read, upload_write):
    @mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=False,
        destructiveHint=False,idempotentHint=False,openWorldHint=False))
    def create_subscription(ctx: Context,chat_ids: SubscriptionReferences | None=None,
                            sources: SubscriptionSources | None=None,
                            sender_ids: SubscriptionReferences | None=None,
                            kinds: SubscriptionKinds | None=None,
                            direction: Literal['','incoming','outgoing']='incoming',
                            origins: SubscriptionOrigins | None=None,
                            label: Annotated[StrictStr,Field(max_length=128,
                                pattern=r'^[^\x00-\x1f\x7f]*$')]='',
                            expires_at: SubscriptionExpiry | None=None,
        wake_rules: WakeRules | None=None, reply_to_send: SendReference | None=None,
        delivery: SubscriptionEndpoint | None=None, feedback: SubscriptionFeedback | None=None) -> dict[str,Any]:
        """Persist selection of authorized archive chats; no send or provider request.

        Requires read + explicit subscription-write. The subscription belongs to the
        originating credential; another key for the same principal cannot adopt it.
        Exactly one of chat_ids (1–64 archive IDs from list_chats/resolve_reference, frozen)
        or sources={all_visible_chats: true, account_ids?, include_channels?, include_bots?, chat_types?}:
        every chat the key can see at the moment of each read, count and hint, so a scope
        change neither invalidates it nor needs a new one; channels and bot senders stay out
        unless included. Dynamic sources need the operator's switch (dynamic_sources_disabled).
        chat_types (1-4 distinct of private, group, channel, unknown) keeps only those chats before
        any match, hint or budget; new chats of those types still join. An untyped chat is unknown,
        and WhatsApp chats are untyped today: [private] leaves out every WhatsApp chat, [private,
        unknown] keeps Telegram private chats and all WhatsApp chats. Naming channel lets channels in;
        include_channels without channel is invalid_filter. It needs a second operator switch
        (source_filters_disabled).
        exclude_chats (up to 40 {account, chat} pairs the key can see, 6,000 bytes) leaves those chats
        out before any match, hint or budget and hides their stored references; replace it later with
        update_subscription(action='set_exclusions'). It is a preference, not authorization (scope
        applies first), and needs a third operator switch (source_exclusions_disabled).
        Recommended wake_rules for them: [reply_to_owner, owner_mention, reply_to_own_send];
        reply_to_own_send (dynamic only) catches an exact native reply to any confirmed send of
        this key. Optional sender_ids (explicit chats only) are exact account-scoped IDs from search_people.
        Starts at the committed event boundary; default message.created/incoming/live.
        contact.birthday (named in kinds only) wakes once per contact and year, from 09:00 owner time the day
        before, with the chat ID and date only; it covers the owner's active Telegram contacts only
        (list_birthdays) and DeedKit adds nothing to any calendar.
        Historical events require explicit backfill/reconcile origins; a message the live stream lost and a
        reconcile pass found within 6 hours of sending has origin live_recovered and matches live. File readiness
        is independent of message arrival. Optional expiry is a future zoned ISO time
        within 366 days. Label is only a correlation hint, not authority to wake a task.
        Delivery uses separate fetch/ACK and configured signed hints; no provider read-state mutation.
        Optional delivery={endpoint_id, delivery_policy?} binds your own endpoint from register_delivery_endpoint;
        it needs delivery-write and the operator's delivery switch.
        feedback=true (alone or beside either) wakes you when your own feedback is fixed, verify_requested,
        declined or duplicate; {statuses} picks others (SUBSCRIPTIONS.md).
        A timeout can leave creation uncertain: inspect list_subscriptions before retrying.
        """
        return call(ctx,'create_subscription',chat_ids=chat_ids,sources=sources,sender_ids=sender_ids,
            kinds=kinds,direction=direction,origins=origins,label=label,expires_at=expires_at,wake_rules=wake_rules,reply_to_send=reply_to_send,
            delivery=delivery,feedback=feedback)

    @mcp.tool(structured_output=True,annotations=read)
    def list_subscriptions(ctx: Context,limit: SubscriptionLimit=50) -> dict[str,Any]:
        """List this exact credential's persistent subscriptions; no other key's definitions.

        At most 100 definitions. Access-changed entries mask source metadata and need
        recreation after current scope is checked. No matching work or delivery ACK.
        """
        return call(ctx,'list_subscriptions',limit=limit)

    @mcp.tool(structured_output=True,annotations=read)
    def get_subscription(ctx: Context,subscription_id: SubscriptionReference) -> dict[str,Any]:
        """Read your definition, lifecycle/version and archive matcher boundary.

        matcher_caught_up covers committed archive events, not live-provider freshness,
        complete history, downloaded originals or completed file processing. Use chat
        get_sync_status and file tools for those independent states. No ACK or push.
        delivery.lag: unfinished push hints, the oldest one's age and seconds to the next attempt.
        positions: acked_through, fetched_through (highest retained receipt), open_receipts,
        oldest_open_receipt_expires_at, expired_unacked; the server cannot know your local cursor.
        activity.days: the last 7 UTC days, oldest first, per platform: scanned, matched, left_out_<reason>
        (first rejecting rule), match_budget_blocked, matched_failure. Numbers only, current scope version only;
        matched counts references kept, not model runs; no cost figure (compare equal periods).
        """
        return call(ctx,'get_subscription',subscription_id=subscription_id)

    @mcp.tool(structured_output=True,annotations=ToolAnnotations(readOnlyHint=False,
        destructiveHint=False,idempotentHint=False,openWorldHint=False))
    def update_subscription(ctx: Context,subscription_id: SubscriptionReference,
                            version: SubscriptionVersion,
                            action: Literal['pause','resume','close','set_exclusions'],
                            resume_policy: Literal['catch_up','skip']='catch_up',
                            exclude_chats: ExcludedChats=None,
                            exclusions_revision: ExclusionsRevision=None) -> dict[str,Any]:
        """Version-checked pause/resume/close of your own subscription, without a provider action.

        Requires read + explicit subscription-write. Read the current version first.
        Resume catch_up keeps the prior matching boundary; skip intentionally advances
        to the current committed boundary. Inspect any reported gap. Closed definitions
        cannot be resumed. set_exclusions replaces the exclude_chats list of an active or
        paused all-visible-chats definition (an empty list clears it); it needs version and
        sources.exclusions_revision (0 before the first list), keeps version and queued hints,
        and returns source_exclusions_disabled until the operator enables it. No other
        selection edit and no credential transfer is supported.
        After an uncertain result, read the definition before retrying its old version.
        """
        return call(ctx,'update_subscription',subscription_id=subscription_id,version=version,
            action=action,resume_policy=resume_policy,exclude_chats=exclude_chats,
            exclusions_revision=exclusions_revision)

    @mcp.tool(structured_output=True,annotations=read)
    def get_subscription_events(ctx: Context,subscription_id: SubscriptionReference,
                                after: SubscriptionPosition=0,
                                limit: SubscriptionLimit=50,detail: SubscriptionPageDetail=None,
                                include: SubscriptionPageInclude=None) -> dict[str,Any]:
        """Inspect bounded durable matches after an event ID; no consumer ACK or push.

        Save the returned next boundary locally to page; no server progress is advanced.
        Message/original/text/transcript/description readiness are separate event kinds.
        Exact event revision and artifact references remain distinct from current content.
        An empty page with a lagging/paused matcher or a gap does not prove no new data.
        Matching and live provider freshness are separate; get_sync_status checks chats.
        Each item has sent_at (provider), observed_at (journal commit), matched_at and delivered_at (null until a hint
        covering it was delivered).
        detail=compact (pollers): same items, cursors and receipt without the definition, delivery and metadata blocks.
        """
        return call(ctx,'get_subscription_events',subscription_id=subscription_id,after=after,limit=limit,detail=detail,
            include=include)

    @mcp.tool(structured_output=True,annotations=read)
    def fetch_subscription_events(ctx: Context,subscription_id: SubscriptionReference,limit: SubscriptionLimit=50,
                                  detail: SubscriptionPageDetail=None,include: SubscriptionPageInclude=None) -> dict[str,Any]:
        """Fetch an issued event page. Does not advance ACK or mark provider read. With explicit subscription-ack, a non-empty page gets a short-lived receipt (5 minutes). Safe to retry; a retry may evict the oldest receipt beyond 128, which then fails closed when ACKed. detail=compact (pollers) drops repeated blocks; same items, cursors and receipt. positions: acked, fetched and open/expired receipts; after invalid_receipt, fetch again (events may repeat) and compare positions."""
        return call(ctx,'fetch_subscription_events',subscription_id=subscription_id,limit=limit,detail=detail,include=include)

    @mcp.tool(structured_output=True,annotations=upload_write)
    def ack_subscription_events(ctx: Context,subscription_id: SubscriptionReference,receipt: SubscriptionReceipt,version: SubscriptionVersion) -> dict[str,Any]:
        """ACK an issued page using its receipt and ack_version. Exact retries are idempotent; never ACKs provider messages."""
        return call(ctx,'ack_subscription_events',subscription_id=subscription_id,receipt=receipt,version=version)

    @mcp.tool(structured_output=True,annotations=read)
    async def wait_for_subscription_events(ctx: Context,subscription_id: SubscriptionReference,timeout: WaitSeconds=25,limit: SubscriptionLimit=50,
                                           detail: SubscriptionPageDetail=None,include: SubscriptionPageInclude=None) -> dict[str,Any]:
        """Wait at most 25 seconds for an authorized event page. No ACK, provider polling or guarantee that a host runs a turn. With explicit subscription-ack, a non-empty page gets a short-lived receipt (5 minutes). Safe to retry; a retry may evict the oldest receipt beyond 128, which then fails closed when ACKed. detail=compact (pollers) drops repeated blocks; same items, cursors and receipt."""
        return await async_call(ctx,'wait_for_subscription_events',subscription_id=subscription_id,timeout=timeout,limit=limit,
            detail=detail,include=include)

    @mcp.tool(structured_output=True,annotations=upload_write)
    def configure_subscription_delivery(ctx: Context,subscription_id: SubscriptionReference,version: SubscriptionVersion,receiver_id: ReceiverReference | None=None,delivery_policy: DeliveryPolicy | None=None,endpoint_id: EndpointReference | None=None) -> dict[str,Any]:
        """Select an operator-registered receiver ID or your own endpoint_id (register_delivery_endpoint, needs delivery-write) for signed hints, not both; null disables. Optional delivery_policy times hints only (debounce or batch, quiet hours, expected-reply silence timer). Never accepts URLs or grants access."""
        # An omitted policy or endpoint keeps the schema-37 payload, so existing audit records and callers are unchanged.
        return call(ctx,'configure_subscription_delivery',subscription_id=subscription_id,version=version,receiver_id=receiver_id,delivery_policy=delivery_policy,endpoint_id=endpoint_id)

    @mcp.tool(structured_output=True,annotations=upload_write)
    def test_subscription(ctx: Context,subscription_id: SubscriptionReference,request_id: RequestReference) -> dict[str,Any]:
        """Queue a rate-bounded synthetic hint to an existing receiver. No fake archive event, message send or event ACK."""
        return call(ctx,'test_subscription',subscription_id=subscription_id,request_id=request_id)


def register_delivery_tools(mcp, call, async_call, read, upload_write):
    @mcp.tool(structured_output=True,annotations=upload_write)
    async def register_delivery_endpoint(ctx: Context,request_id: RequestReference,url: EndpointUrl,signing_secret: SigningSecret,label: EndpointLabel='',headers: EndpointHeaders | None=None) -> dict[str,Any]:
        """Register your own HTTPS webhook endpoint for subscription hints, then send it one signed test delivery.

        Requires read + explicit delivery-write, and the operator's delivery switch (else delivery_disabled).
        The operator sets the host policy, never an agent: url must be https on port 443 at a public host the
        policy allows (else unsafe_endpoint or delivery_policy_unset). signing_secret is your own whsec_<base64>
        (24-64 bytes) for Standard Webhooks signatures; optional headers (at most 4) go with every delivery.
        Secrets and header values are write-only: no response, list, log or error returns them.
        The endpoint belongs to this exact key. Waits up to 10 s for the test; test_status pending names the
        test_delivery_endpoint call. A failed test still creates the endpoint. Repeat the same request_id and
        arguments to recover a lost response.
        """
        return await async_call(ctx,'register_delivery_endpoint',request_id=request_id,url=url,signing_secret=signing_secret,label=label,headers=headers)

    @mcp.tool(structured_output=True,annotations=read)
    def list_delivery_endpoints(ctx: Context) -> dict[str,Any]:
        """List this exact key's webhook endpoints with state, health, last test and fingerprints; never secrets or header values."""
        return call(ctx,'list_delivery_endpoints')

    @mcp.tool(structured_output=True,annotations=upload_write)
    async def update_delivery_endpoint(ctx: Context,endpoint_id: EndpointReference,version: SubscriptionVersion,
                                 action: Literal['rotate_secret','set_headers','pause','resume'],
                                 new_secret: SigningSecret | None=None,headers: EndpointHeaders | None=None) -> dict[str,Any]:
        """Version-checked change of your own endpoint: rotate_secret (new_secret), set_headers (headers; {} clears), pause or resume.

        Requires read + explicit delivery-write. During a rotation deliveries carry both signatures for 24 hours,
        then the new secret alone. Resume works from agent_paused or auto_paused: it sends one signed test, waits
        up to 10 s, and re-arms the endpoint only once the test is delivered (else endpoint_test_failed, or the test
        pending with the same resume call to repeat); each bound subscription with unACKed events then gets one
        hint at once. Only the operator lifts operator_disabled. Write-only values are never returned.
        """
        return await async_call(ctx,'update_delivery_endpoint',endpoint_id=endpoint_id,version=version,action=action,new_secret=new_secret,headers=headers)

    @mcp.tool(structured_output=True,annotations=read)
    def list_delivery_attempts(ctx: Context,endpoint_id: EndpointReference,limit: AttemptLimit=50,before: ReceiverReference | None=None) -> dict[str,Any]:
        """List your own endpoint's latest delivery attempts, newest first: time, hint and subscription IDs, attempt number, outcome (delivered, retry, dead_letter, cancelled, paused), HTTP status or error code, latency_ms, hint_created_at and hint_admitted_at (null once the hint was pruned).

        Requires read + explicit delivery-write. Kept 7 days; no response body, header value or URL. Pass the
        previous page's next_before as before for older attempts.
        """
        return call(ctx,'list_delivery_attempts',endpoint_id=endpoint_id,limit=limit,before=before)

    @mcp.tool(structured_output=True,annotations=upload_write)
    def delete_delivery_endpoint(ctx: Context,endpoint_id: EndpointReference,version: SubscriptionVersion) -> dict[str,Any]:
        """Delete your own endpoint; queued hints and tests are cancelled and bound subscriptions become pull-only. Requires read + explicit delivery-write."""
        return call(ctx,'delete_delivery_endpoint',endpoint_id=endpoint_id,version=version)

    @mcp.tool(structured_output=True,annotations=upload_write)
    async def test_delivery_endpoint(ctx: Context,endpoint_id: EndpointReference,request_id: RequestReference) -> dict[str,Any]:
        """Send one signed test delivery to your own endpoint and wait up to 10 s for http_status and latency_ms.

        Requires read + explicit delivery-write; at most one test per minute per key. The same request_id
        reads back the same test; use a new one after fixing the receiver. No archive event or message send.
        """
        return await async_call(ctx,'test_delivery_endpoint',endpoint_id=endpoint_id,request_id=request_id)

    # ANYM-848: an agent's own wake targets; a host waker (packages/wake-relay) reads them, DeedKit never contacts the chat.
    @mcp.tool(structured_output=True,annotations=upload_write)
    def register_wake_target(ctx: Context,request_id: RequestReference,kind: Literal['codex_thread'],thread: WakeThread,
                             subscription_ids: WakeTargetSubscriptions,label: WakeTargetLabel='',
                             hourly_budget: WakeTargetBudget | None=None,rebind: StrictBool=False) -> dict[str,Any]:
        """Register one of your own chats to be woken when any of your own listed active subscriptions has new events.

        Requires read + explicit delivery-write and the operator's delivery switch (else delivery_disabled). A Codex
        agent passes its own CODEX_THREAD_ID as thread. The host waker on the owner's machine reads your targets, sends
        the chat one challenge with a code, and wakes it for events only after that chat calls confirm_wake_target
        with the code. A subscription wakes at most one chat: subscription_already_bound names your target that holds
        it; rebind true moves it here. hourly_budget (default 12) caps wakes per hour on the waker. Repeat the same
        request_id and arguments to recover a lost response; DeedKit never sends anything to the chat.
        """
        return call(ctx,'register_wake_target',request_id=request_id,kind=kind,thread=thread,
                    subscription_ids=subscription_ids,label=label,hourly_budget=hourly_budget,rebind=rebind)

    @mcp.tool(structured_output=True,annotations=read)
    def list_wake_targets(ctx: Context) -> dict[str,Any]:
        """List this exact key's wake targets, newest first: thread, label, state, hourly_budget, confirmation and the IDs of their still-active subscriptions (bound counts them). Requires read + explicit delivery-write."""
        return call(ctx,'list_wake_targets')

    @mcp.tool(structured_output=True,annotations=upload_write)
    def update_wake_target(ctx: Context,wake_target_id: WakeTargetReference,version: SubscriptionVersion,
                           action: Literal['pause','resume','set_thread','set_subscriptions'],thread: WakeThread | None=None,
                           subscription_ids: WakeTargetSubscriptions | None=None,rebind: StrictBool=False) -> dict[str,Any]:
        """Version-checked change of your own wake target: pause, resume, set_thread (thread; needs a new confirmation) or set_subscriptions (subscription_ids, rebind).

        Requires read + explicit delivery-write. A stale version gives wake_target_conflict with the current version.
        Only the owner lifts operator_disabled: then every action except delete gives invalid_wake_target_state with
        disabled_reason and the command to ask for.
        """
        return call(ctx,'update_wake_target',wake_target_id=wake_target_id,version=version,action=action,thread=thread,
                    subscription_ids=subscription_ids,rebind=rebind)

    @mcp.tool(structured_output=True,annotations=upload_write)
    def delete_wake_target(ctx: Context,wake_target_id: WakeTargetReference,version: SubscriptionVersion) -> dict[str,Any]:
        """Delete your own wake target; its subscriptions are unbound and the chat is no longer woken. Requires read + explicit delivery-write."""
        return call(ctx,'delete_wake_target',wake_target_id=wake_target_id,version=version)

    @mcp.tool(structured_output=True,annotations=upload_write)
    def confirm_wake_target(ctx: Context,wake_target_id: WakeTargetReference,code: WakeConfirmationCode) -> dict[str,Any]:
        """Return the code of the host waker's challenge that this chat received, so the waker starts waking it.

        Call it only from the chat that got the challenge and only for a target this chat registered. The latest code
        replaces an earlier one. Requires read + explicit delivery-write.
        """
        return call(ctx,'confirm_wake_target',wake_target_id=wake_target_id,code=code)
