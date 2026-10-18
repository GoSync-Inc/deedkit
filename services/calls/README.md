# Voice tasks — a separate Pipecat service

Shared backlog and statuses: **[DeedKit BACKLOG](../../BACKLOG.md)**
(DK-01, DK-20, DK-21). This README describes the current contract and checks.

Experimental Grok Voice 3.0 for the browser and Twilio phone/SIP, a separate xAI key,
readiness diagnostics for agents and the order of live verification:
[GROK.md](GROK.md). Synthetic checks do not assess model quality in a conversation.

Added a browser mode **Flows + Gemini Live** with a real FlowManager, transitions through tool results and a check for new speech before hanging up. The supported scope and the verification scenarios are described in [LIVE_FLOWS.md](LIVE_FLOWS.md). Phone connections use the previous executor until separately verified.

Speed measurements of the ordinary chain with separate recognition, a text model and voice: [BENCHMARKS.md](BENCHMARKS.md).

A separate Pipecat service: task → selected AnyMsg facts → Gemini Live → consultant → deferred question to the owner → new follow-up. A server pilot is added: Telegram signaling through the existing session owner, Twilio for WhatsApp Business, phone and SIP, and a separate MCP with 11 tools, or 14 with `VOICE_EXTERNAL_MCP=1`. The Telegram connection is verified; live calls have not been made yet, Twilio credentials/Business sender need setup. The service starts independently; state is passed through `VOICE_DATA_DIR`. The owner connects the providers.

## Starting

From this folder:

```sh
uv sync --locked --python 3.13
cd frontend
npm ci
npm run build
cd ..
uv run python app.py
```

Open **http://127.0.0.1:13842/**. The browser will ask for the login `owner` and the password from `VOICE_DATA_DIR/owner.token`, created by the service on first start. The server listens only on loopback; for remote access use an SSH tunnel, do not publish the panel over plain HTTP. Python 3.13, Pipecat 1.12.0, Gemini `gemini-3.8-live`, SmallWebRTC. This is native audio: separate ASR/LLM/TTS services are not added. LiveKit is not connected.

The **"Подключить Gemini"** (Connect Gemini) button accepts a key from [Google AI Studio](https://aistudio.google.com/apikey). In a new process the key must be entered again; it is not carried over from Voice Lab. In an ordinary local run the key is kept only in memory and is removed after a restart or with the "Удалить ключ" (Delete key) button. A server installation with VOICE_PERSIST_CREDENTIALS=1 saves it in a private file; this is shown explicitly in the setup panel. Saving the key does not prove access to the model: the API is checked during a conversation. Audio and the selected context are sent to Gemini; raw audio is not recorded locally.

## First check

1. Leave the training task about order TEST-104 or fill in your own. Choose Russian, English or Indonesian, a voice and self-introduction forms.
2. If needed, expand "Добавить сообщения AnyMsg" (Add AnyMsg messages), run a targeted search and mark up to 8 messages. The voice agent cannot search the whole archive itself.
3. Press **"Подготовить поручение"** (Prepare the task), read the facts and the exact instructions. After changing settings a new snapshot is needed.
4. If needed, set the "Дополнительный контекст для консультанта" (Additional context for the consultant). It is received by a separate text Gemini Flash on `consult_supervisor`, not directly by the voice prompt. This is a separate API request on the same key.
5. Start a conversation and play the other party. Say delivery is 11–12, correct it to 12–13 and name a surcharge of 50000 IDR. Do not answer as the owner during the conversation: the assistant must save the request and end briefly with `needs_owner`, without promising a callback that is not connected.
6. After it ends, in "Вопросы, которые можно решить позже" (Questions that can be resolved later) answer "Отклонить" (Decline), then press "Подготовить продолжение в браузере" (Prepare a follow-up in the browser). In the new attempt the agent will pass on the refusal. Offer 25000 IDR or ask to reconsider: a new request is needed, the earlier answer does not extend to the new terms.
7. Rate each attempt and repeat in the other languages. The history keeps snapshots, text, tools, advice, decisions and follow-up links.

The hint with test answers in the browser is **not part** of the model's instructions. The p50/p95 delay is approximate: end of microphone audio → start of reply audio, one monotonic browser timer; the greeting and real speech overlap are excluded. Background sounds, pauses, the output device and a small number of measurements affect the result. Values without measurements are empty.

Check separately: whether the needed fields were collected; whether the correction was taken into account; whether an unconfirmed payment/booking was promised; whether the needed tools were executed; whether naturalness was kept; whether it can be interrupted; the delay. Transport success or a synthetic test is not counted as an assessment of Gemini in ru/en/id.

## AnyMsg context

In the working environment the local setting is in the ignored `.local/voice-service/anymsg.json`. For another installation, set `VOICE_ANYMSG_CONFIG` with JSON:

```json
{"base_url":"https://your-anymsg.example/prefix","token_file":"/private/path/anymsg.token"}
```

`base_url` is the root of the REST API without `/mcp`; the file contains a Bearer token without a prefix. The files must be accessible only to the owner. The service does not read a messenger session/profile and does not start collectors. Only `POST /v1/capabilities`, `/v1/search`, `/v1/get_context` are used. A separate AnyMsg principal with read-only rights is recommended.

Search is limited to 12 results. On preparation each selected anchor is read with `before_count=0`, `after_count=0`; neighboring messages, attachments and reviews are not included in the prompt. A deleted/unavailable source stops preparation. The snapshot stores the ID, revision, account, chat, date, conflict and a flag that the text was shortened. Archive data remains observations, not a check of the current state at the provider. A read error is not replaced by an empty successful response.

## Tools inside a conversation

| Tool | Action |
|---|---|
| `lookup_context(query)` | Read only the facts of the current snapshot; an empty query returns all selected facts |
| `record_answer(field, value, evidence)` | Save/correct an allowed field with a quote; for `approval_fields` it automatically creates a decision request and returns its state |
| `request_owner_decision(question, options)` | Create a request, including a reconsideration of the same terms after an answer; a repeated call while a decision is already pending returns the same round |
| `consult_supervisor(question)` | Brief advice from a separate Gemini Flash; the selected facts, additional notes and the current conversation, with no owner authority |
| `finish_task(summary)` | Record the result, show missing fields and pending decisions |
| `end_conversation(reason, summary, evidence)` | End as completed/needs_owner/refused/unavailable; a refusal/unavailability needs an exact quote. A refusal does not become a success |

Calls are bound by the backend to the current `call_id`. Provider tool IDs are processed idempotently; unknown tools and fields are forbidden. Only the local owner endpoint answers a decision request, including after the end. The control MCP does not record the owner's consent. The other party's speech and `approved=true` in tool arguments do not replace it. If the call is still active, the owner's answer is passed to Gemini's realtime text input; waiting for the answer during the call is not required. After a decision a new summary is needed; after a correction the previous summary is considered stale.

Policy v4 keeps the v3 rules: `approval_fields` is an explicit rule of the task, not an inference from a phrase about price. By default the UI requires a decision for `additional_cost`, including a message that there is no surcharge; for ordinary information gathering the list can be cleared. A request is tied to the exact snapshot of all saved values. Changing any value replaces the old round, keeps the earlier answer for audit and creates a new request. The old card cannot be used to answer. A duplicate of a pending request does not create a new round; after an answer is received, a reconsideration request creates a new one. Questions and answers do not turn into a right to perform external actions. Ending as `completed` is forbidden while a decision is pending; a refusal and unavailability may end a conversation with unresolved terms.

New in v4: ending as `needs_owner` saves the request without waiting for the owner. `GET /api/decisions` / MCP `get_owner_requests` give a shared queue. After a deferred answer `followup_ready` arises. `prepare_followup` creates one separate browser attempt per decision with the exact terms/hash, the owner answer and the parent call ID. Old conversations are not rewritten. A refusal by the other party forbids a follow-up; historical v1 decisions not tied to terms cannot be reused.

The `gemini-3.8-flash` consultant receives `supervisor_notes`, the selected snapshot and the current conversation. At most 6 requests per attempt, a wait of up to 6 seconds for one, input up to 100000 characters. `VOICE_SUPERVISOR_MODEL` allows changing the text model. Advice is not owner approval. A timeout/error/unknown refs give no permissions; late advice, when the terms change or on closing, gets `obsolete`. All requests/advice are saved in the journal.

Owner notifications, a permanent Personal OS controller and an external callback scheduler are not connected yet (DK-20 in the shared backlog). In the browser the queue is a test owner channel; the workflow is implemented in the states described above.

This guarantees a request after a marked field is saved. Understanding speech, calling `record_answer` and asking to reconsider the same terms still depend on Gemini and are checked by a live scenario. Text backend tests check states, not the model's behavior on audio.

No tool of this version calls, sends messages, pays, books or changes a calendar. The prompt does not guarantee the absence of mistaken verbal promises; they must be checked against the conversation. The collected result has `collected_needs_review`, not the status of a performed external action.

## Separate control MCP

The endpoint **http://127.0.0.1:13842/control/mcp** uses the official Python MCP SDK and a separate Bearer key from `.local/voice-service/data/control.token` (private, not shown in the UI or responses). The server endpoint is connected to Codex as a separate voice-calls through a private stdio proxy/SSH tunnel. The AnyMsg gateway remains separate. The archive key is not the control key.

Eleven tools are always present: `get_voice_capabilities`, `prepare_call(brief)`, `start_call(brief_id)`, `get_call_status(call_id)`, `stop_call(call_id)`, `get_call_result(call_id)`, `get_owner_requests(limit, offset)`, `prepare_followup(call_id, decision_id)`, `get_call_analysis(call_id)`, `get_voice_learning()`, `submit_call_analysis(call_id, analysis)`. With `VOICE_EXTERNAL_MCP=1`, three more are registered (14 in total; provider readiness is checked separately): `prepare_external_call(brief)`, `preflight_call(brief_id)` and `dial_call(brief_id, destination_source, owner_instruction, destination_evidence)` (control.py; `authorization_reference` is the earlier name of `owner_instruction`). Phone, WhatsApp and Telegram calls need no recipient list: the destination may be one the owner named (`owner_named`, the owner's words must contain it) or one the agent found for the owner's task (`agent_found` with evidence such as a URL). The call record and the owner panel show the owner's words and the destination's origin. Emergency numbers and short codes are refused with a message telling the owner to call them directly; invalid numbers are refused; premium-rate, shared-cost and UAN numbers cost 3 of the 10 daily call units (rolling 24 hours, counted from saved calls). Countries are limited in Twilio Console → Voice → Geo Permissions. SIP keeps its exact allowlist. `submit_call_analysis` saves the calling agent's analysis with the separate provenance `control_mcp_agent`, checks references to events and creates only draft cases/proposals. `prepare_call` accepts the same strict `BriefInput` schema as REST. `start_call` creates a browser check with the status `waiting_for_browser`; a person opens the page, checks the task and connects the microphone. A repeated start of one snapshot returns the same record; for a repeated check a new snapshot is created.

REST is described in the local `/docs`. The panel, the documentation and all owner APIs, including GET, require a separate owner credential: HTTP Basic with the login `owner` for the browser or `Authorization: Bearer <owner token>` for a client. `Host`, `Origin` and `X-Voice-Service` do not replace authentication. Mutating browser requests additionally require a same-origin `Origin` and `X-Voice-Service: 1`; MCP and `/v1/` keep using their own keys and rights. The hostnames of the panel/MCP are limited to loopback. Only separate provider routes are publicly accessible, with a Twilio signature and binding to an attempt. The local UI is intended for one trusted owner, not for a public multi-user deployment.

`owner.token` contains 32 random bytes in URL-safe base64, is stored with permissions 0600 and belongs to the process user. Do not pass it to agents, URLs, the frontend bundle or logs. To rotate, atomically replace the file with a new `secrets.token_urlsafe(32)` with the same owner and permissions: the old key stops working from the next request, no restart is needed. Symbolic/hard links, wrong permissions and corrupted files lead to access being denied; at startup the service stops with an error. Deleting the file while running also closes owner access. The browser caches the Basic credential: to end a local session close the private window, to revoke all sessions immediately rotate the file.

The Docker image uses `python healthcheck.py`: the check reads the owner credential inside the container and requests the local `/api/health`, without printing the key or the response. `/api/health` requires the owner credential and checks only the process itself, without the Telegram broker and AnyMsg: their state is still visible in `/api/config`. When updating an existing Compose, replace the old anonymous probe with `test: ["CMD", "python", "healthcheck.py"]`; the other intervals can be kept. A public health endpoint is not added. By default the image starts with `VOICE_EXTERNAL_MCP=0`: `dial_call` and other external calls through the control MCP are enabled only by an explicit setting. `control.token` is checked by the same rules as `owner.token` (no links, 0600, process owner), otherwise the service does not start.

Twilio HTTP callbacks accept only a POST of the form `application/x-www-form-urlencoded`: at most 64 KiB, 64 fields, 128 characters in a name and 4096 in a value. Repeated fields, ambiguous headers and incorrect encoding are rejected. All fields, including unknown ones, take part in the signature check. A missing signature is rejected before the body is read; for other requests a total read time of 5 seconds and at most 8 concurrently processed callbacks per process apply (413/408/429 when exceeded). The media WebSocket keeps a separate signature check, binding to an attempt and audio limits. The deployment is designed for one worker with a single SQLite database.

## Storage and MVP limits

The ignored `.local/voice-service/data/voice.sqlite3` contains tasks, conversation text, tools, decisions and ratings; directory permissions 0700, files 0600. The Gemini key does not get into the database. One active check; at most 10 minutes, Pipecat idle timeout 180 seconds. An unfinished conversation after a restart gets `interrupted`. There is no automatic retry. SQLite is designed for one process — do not start a second service with the same database.

The journal saves all received Gemini text fragments and finished utterances without the former truncation to 6000 characters/1000 events. Events are appended to a separate table, without rewriting the history on every word. Before stopping, the pipeline is closed, then completion and the analysis queue are recorded. Historical conversations have `legacy_finalized_turns`: fragments already lost cannot be restored. Answers may refer to received pieces of text even before the aggregated turn is finished. Without raw audio, recognition accuracy and the text actually heard are not checked.

## Analysis and the improvement loop

After each finished attempt a durable report is created: the exact snapshot/provider/language/voice/prompt, utterances and tools, completion, metrics and the owner's rating. Even a model error/restart keeps the attempt. Without text — `no_speech`; without a key — `waiting_key`. A new rating/metrics create a new version of the input; old analyses are kept. A restart restores the queue, API errors do not cause endless retries. Deleting the key cancels the current model job.

Offline journal checks and semantic analysis are separate. The semantic agent is Gemini `gemini-3.8-flash`, on the same key, a separate paid API request; the model can be set with `VOICE_ANALYZER_MODEL`. It checks refusal/completion, repeats, invented facts/deadlines/actions, corrections, role, tools and owner decisions. Each hypothesis must refer to existing event IDs. The conversation text and facts remain data, not instructions to the evaluator. Analyses by the controlling agent have a separate source. [Google: structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output?hl=en).

In the **"Разбор и улучшения"** (Analysis and improvements) panel, open a conversation from the history, look at the quotes, check the proposed case and its expected result. **"Принять кейс и проверить улучшение"** (Accept the case and check the improvement) saves the scenario and queues the related proposals for comparison. Accepted old cases stay in the set. Twelve fixed scenarios — refusal, a correction with the word "нет" (no), owner substitution and ending with a deferred decision × ru/en/id. Rejected/unchecked hypotheses do not become a reference.

Regressions send scripted **text** to a real Gemini Live with the current scoped tools and the chosen voice. The original prompt of the problem conversation is taken from the snapshot, with the same current backend tools; other checks compare the baseline and new experience guidelines. Both traces/quotes/answers/decisions/prompt hashes/ratings are saved. This is a check of text and tools, not of the whole historical runtime, the microphone, ASR, prosody or WebRTC delay. One run does not prove a lasting improvement; for audio a repeated browser acceptance is needed.

A candidate is allowed to the enable button only if at least one original problem case improved and all accepted cases/guardrails passed. If the criteria change, a new check is needed. Enabling affects only new snapshots; old ones are not rewritten. **"Вернуть базовый промпт"** (Restore the baseline prompt) does a rollback. Only draft cases/proposals change automatically; code, model weights, tools and rights are not edited. The current local runner has a limit of 30 accepted cases/8 utterances per case; larger sets require a batch runner. All text is stored, but semantic input over 180000 characters gets an explicit `input_too_large`, without a false claim of a full analysis.

The errors of the original two browser dialogs have already influenced policy v2: do not invent sending/repeated approval/a response deadline, do not tell the other party about internal interfaces, clarify a different currency. The service owner frame is excluded from the assistant transcript. Native closing was added as a separate tool. The quality of these fixes is checked by a new conversation; unit/fixture checks are not a successful live benchmark.

Implementation of storage, provenance and draft proposals: [learning.py](learning.py).

Live checks of the installed adapters (Twilio phone/SIP/WhatsApp Business and Telegram private-call signaling + raw audio bridge) are tracked as DK-21 in the shared backlog. LiveKit is not connected; the personal WhatsApp native route is not implemented. The presence of authorized correspondence does not prove access to calls. [Module runtime](../../docs/platform-runtime.md), [boundaries of the browser Flows](LIVE_FLOWS.md).

## Telegram audio diagnostics

By default the standard `Puck`, masculine gender, `voice_style: "neutral"` and `custom_voice_id: null` are used: the sound is generated by Gemini Live without a separate TTS stage. The neutral instruction sets an ordinary adult manner, without baby talk, exaggerated warmth or forced laughter.

`conversation_mode: "social"` enables free conversation. `record_answer` and `finish_task` save notes, but the collected fields do not allow ending the call. `end_conversation(reason="completed")` needs a fresh explicit farewell from the person and its exact quote; an earlier goodbye before a new story does not qualify. The check allows ordinary repeats of "давай/ага/пока" (okay/uh-huh/bye), keeps the words/punctuation of the quote and normalizes the aggregator's whitespace. A refusal to end returns an instruction to clarify once, not to repeat the farewell and to wait for new speech. This is an instruction to the model, not proof of its execution in audio. When ending with missing notes the result stays `incomplete`. In `task` mode the requirements for filling the task are kept. A complaint about voice quality/delay is not a refusal: for example, "Мне не пойдёт" (That doesn't work for me) needs clarification. The check of explicit refusals/farewells covers a limited set of ru/en/id phrases; an unusual wording may need clarification. The technical duration limit and the other party hanging up apply in both modes.

A replicated voice can be set explicitly through `custom_voice_id` (a Google profile of the same API project); `voice_style` chooses `neutral` or `friendly`. The identifier of a personal voice is stored only in private settings/tasks. Gemini Live keeps understanding incoming sound and executing scoped tools; its outgoing speech is replaced by streaming Gemini Flash-Lite TTS 3.8. Live's native audio is not passed to the other party in this mode.

`friendly` is passed specifically as `speech_metadata.style` in every synthesis request: a warm relaxed intonation, a calm tempo, natural pauses and appropriate expressiveness. This complements the dialogue instructions. Allowed pause/soft-laughter tags are kept, arbitrary markup is dropped; effects are not inserted automatically into every utterance. A bot with a replicated voice introduces itself as the owner's voice bot.

Pipecat aggregates text by sentences, plays PCM 24 kHz through the chosen transport, cancels an unfinished HTTP stream and the accumulated speech on interruption. Delivery of the last farewell phrase needs separate verification in a live call: the current completion guard does not confirm that it was played to the other party. `metrics.replicated_tts` saves the duration of requests, the time of the first PCM and the cancellation status, without keys or audio recordings. This is synthesis time, not the delay to the other party's speaker. A custom voice adds a separate synthesis stage; its delay should be assessed in a live call.

External Telegram calls save `metrics.telegram_audio`: the duration of received and sent PCM, of active input sound and a limited set of `response_gap_ms`. This is an approximate measurement by PCM energy on the worker: from the arrival of the last active input packet to the sending of the first active packet of the reply. A greeting without input speech, short noise and speech overlap are excluded; noise/echo may affect the estimates. Network delay to the phone's speaker is not included, `recipient_playback_verified` stays false. With no input PCM the response delay is not computed. Raw audio is not recorded.

In the pinned PyTgCalls 3.0 the input `types.Frame` contains bytes in `frame`; the `data` field belongs to the lower NTgCalls level. A regression checks that a real public SDK packet is passed into Pipecat and that a foreign peer/outgoing stream is excluded. The success of this check does not replace a repeated live conversation.

## Checks

```sh
uv run pytest -q
cd frontend
npm test
npm run build
```

Covered: scoped archive reads, snapshot, lifecycle/restart, CSRF/secrets, corrections/quotes, repeated tool IDs, pending decisions, Gemini settings/tool wiring, realtime owner updates, real local WebRTC audio/RTVI/cleanup, deferred decisions/follow-ups, supervisor advice/timeout/obsolete and the manifest through a real MCP client.
