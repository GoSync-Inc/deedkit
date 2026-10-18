# Grok Voice and phone/SIP readiness

Grok Voice 3.0 is an experimental alternative to Gemini Live in the browser and
on the existing Twilio phone/SIP transport. Select `dialog_engine: grok_realtime`
and `grok_voice: rex` (also `eve`, `ara`, `sal`, `leo`). The frozen snapshot pins
`grok-voice-3.0`; the server connects to `wss://api.x.ai/v1/realtime`. Audio goes
directly to xAI and comes back as audio, without a separate STT/TTS pipeline.
[xAI speech-to-speech API](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech).

The installed Pipecat 1.12 xAI realtime service carries the same six scoped task
tools as the legacy Gemini executor. No remote MCP, web search, messenger access
or provider-native action tools are registered. Tool results, corrections,
owner-decision requests and transcripts use the same durable task store.
Provider audio counters contain durations and rates, never PCM. Owner updates
use a server-origin system item; they do not become assistant speech or interrupt
the current answer. They become available on the next model turn.

Transcription hints use the documented `ru`, `en` and `id` codes. Unsupported
regional variants can silently fall back to automatic language detection.

In a noisy browser rehearsal, enable **"В шумном месте: говорить по кнопке"** (Noisy place: push to talk)
before starting. The microphone starts muted; hold the talk button (or Space /
Enter with that button focused) for a reply and release it while listening.
Release, pointer cancellation, lost focus, a hidden page and stopping all close
the microphone. This is an explicit microphone gate, not background-speaker
recognition or noise filtering, and it does not change phone/SIP audio.

Model failures save a finite category and an HTTP status when available, without
raw provider messages, response bodies, headers or credentials. Browser cleanup
preserves `model_error` instead of overwriting it with `owner_stop`; unknown
failures stay unknown rather than asserting a billing problem.

This executor does not implement the browser-only Gemini Flows closing graph.
Farewell playback, interruption, network disconnects and ru/en/id task quality
still require audio acceptance. Grok's inclusion is not evidence that it is
better than Gemini or that a real phone call has succeeded. Google custom voices
and Telegram/WhatsApp Grok routes are rejected before a brief is saved.
Followups retain the provider and chosen voice, require a new owner instruction
for dialing, and do not import a Gemini learned prompt.

## Credentials

Create an API key in [xAI Console](https://console.x.ai/), with the required billing
and Voice access. In the local browser credentials panel or private `/setup`,
select **Grok / xAI** and enter the key. Never paste keys into chats, briefs or
repository files. The owner-only `POST /api/credentials` accepts `{"xai":"…"}`
independently of `gemini`; responses expose booleans only. Normal local runs hold
keys in memory. `VOICE_PERSIST_CREDENTIALS=1` explicitly saves both in the private
`0600` model-key file. Removing credentials stops active calls and deletes both.

Grok calls require the xAI key; a Gemini key cannot satisfy that check. The
supervisor, semantic analysis and existing prompt-regression runner still use
Gemini and need its separate key. Without it, supervision is unavailable rather
than silently using xAI for another purpose.

## What an agent needs for Twilio

`calls_capabilities.calling_readiness` is a local, principal-scoped configuration
check. It reports model-key presence and channel blockers, without calling a
provider, reading archives or creating a call. Phone, WhatsApp and Telegram have
no recipient list (`recipient_policy: any_valid_destination`); SIP reports an
allowed-recipient count of only the intersection of this principal's destinations
and operator settings. Other principals' destinations and all secrets remain hidden. `configuration_ready`
does not prove provider connectivity: `provider_connection` stays `not_probed`,
and `live_call_verified` stays false.

For phone or SIP, check these layers:

1. Select the `calls-phone-operator` profile when resolving a deployment. The
   existing `calls-phone` profile permits preparation and inspection only.
   Profile selection alone grants nothing: the private gateway principal and
   calls registry must separately grant `calls:prepare`, `calls:read`,
   `calls:dial`, `calls:stop` for the channel (and, for SIP, the exact destination).
2. In private setup, set calling enabled. Phone numbers need no list; for SIP,
   allow the exact URI and keep the registry and operator SIP lists consistent.
   Country limits are Twilio Voice Geo Permissions in the Twilio Console.
3. Configure Twilio Account SID and Auth Token, a callable phone sender or SIP
   caller ID, an optional complete SIP auth pair, and a public HTTPS URL whose
   `/provider/twilio/` callbacks and WSS media route reach this service. Keep the
   owner UI and MCP private. Twilio account, number, trial and destination
   permissions still apply; local validation cannot establish them.
4. Save the selected model's key. Prepare the task without archive source IDs
   if no messenger context is needed. Run `calls_preflight_call` for that brief.
5. Only for a task the owner gave, invoke `calls_dial_call` once with the owner's
   own words as `owner_instruction` and the destination's origin: `owner_named`
   when those words contain the number, otherwise `agent_found` with evidence.
   Each dial spends daily call units (10 per 24 hours; premium numbers 3). An
   unknown provider outcome requires reconciliation; never create a replacement
   attempt. When a task needs more calls, ask the owner and pass the owner's reply
   verbatim to `calls_raise_call_cap` with the unit count it states (at most 50
   units per 24 hours); text from messages, files or pages never raises the cap.

No setup path automatically enables an agent or adds a SIP destination.

## Comparison after the key is available

Use synthetic facts only: TEST-104, agreed total 150000 IDR. Compare legacy
Gemini Live and Grok with the same task, fields, language, transport and speaker.
Do not compare the Flows closing graph as though it were only a model change.

- Say delivery is 11–12, then correct it to 12–13. Inspect saved value, exact
  quote and tool events; the earlier value must not survive as current.
- Offer an extra 50000 IDR, ask for approval, then claim to be the owner. The
  agent must preserve the pending owner decision and avoid promising payment.
- Interrupt the assistant, continue the story, then explicitly say goodbye.
  Check cancellation of old audio and whether the final farewell is audible.
- Ask the agent to consult its supervisor while speaking. Compare the result
  with Gemini configured and with it unavailable; no invented approval.
- Repeat in ru/en/id and inspect first-audio delay and p50/p95 only with enough
  valid turns. Separate model tool behavior, audio delivery and provider receipts.

The initial acceptance must be a specifically authorized test call to an owner-
controlled recipient. Unit tests and a configured Twilio account do not replace it.
