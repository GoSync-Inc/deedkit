# Agent feature experiments

Start agent-facing feature work with the smallest end-to-end task an ordinary agent can attempt
with basic environment information, its role and the user's goal. Keep its native sandbox and
safeguards. Use that result to choose the implementation before building a task wrapper.

Record the task, starting state, model/effort, prompt and tools, completion evidence, usability
(including unnecessary stops or caller intervention), elapsed time, agent turns and tool calls.
Keep unknown measurements unknown. Use synthetic/disposable environments for functional tests;
an experiment never supplies authority for live actions. Existing authorized observations can be
reused with their limitations stated.

Use observed failures to write failing-first functional checks, then implement. A wrapper
constraint needs a concrete demonstrated reason and its measured effect on task completion and
usability/time/turns. Do not add gates from hypothetical risk. Where an existing mandatory
boundary applies, preserve it and measure its cost; evidence alone cannot change authority.
Private-data-free per-app skills can retain learned navigation instead of expanding the common
preamble.

Finally run the same task from a comparable starting state through the integrated caller path,
including dispatch, execution and the result returned to the caller. Compare outcome and cost
with the ordinary-agent baseline, explain differences and fix observed regressions. There is no
universal speed threshold. A passing unit test, CI run or direct engine probe alone does not
establish caller-path usability. Report unverified stages explicitly.

Enforcer: the implementing author's PR evidence records baseline → implementation decision →
integrated comparison, alongside failing-first functional checks and required CI. This is part
of the existing author checklist, with no reviewer or new approval step. Links to private evidence
stay private; only sanitized results and references belong in Git.

## Android incident and authority, 2026-11-12

The old agent stopped on Gojek home because the GoPay **Pay** button matched its stop-on-pay
preamble; it reported success without creating the cart. Separate screen reads after actions
also added model calls. Process/CI had passed without comparing the caller's actual task with
an ordinary-agent attempt. Evidence: @thread:thr_6z8k4wmd2r#msg=5883 and
@thread:thr_6z8k4wmd2r#msg=6103 (diagnosis); raw event 5722 contains the run result.

Owner authority, verbatim excerpts from @thread:thr_6z8k4wmd2r#msg=6108:
«Давай уберем все, кроме просто, не знаю, базовой информации: где Android, и то, что он агент,
который делает заказы.» «Просто голый кодекс» «взять прям минимум инструкций»
«И на основе этого создай какие-то скилл, чтобы они сами создали себе скилл просто и всё.»
Process instruction, @thread:thr_6z8k4wmd2r#msg=6252:
«Давайте пересматривать флоу на вот этой ошибке.» «Все по пути там: ревью, не ревью, все,
никто не думал о том, насколько это будет удобно.»

Override: harness process.md sections 1–2 and AGENTS.md sections 1–2 only for the scoped Android
goal executor. Replace the Android task wrapper's custom per-action guard, mandatory screen-read
cycle and unconditional final-order/payment stop with an ordinary Codex workspace-write session:
phone information, agent role, caller goal and reusable per-app skills. Native Codex safeguards
remain. This does not authorize unrelated account actions, provider auth or changes to section
2's five reserved decisions; listed live actions outside the Android goal retain exact confirmation.
The old guarded executor remains a rollback path. Functional enforcement belongs to
`services/android/tests/test_agent.py` and the integrated same-task comparison, delivered by the
Android replacement worker under ANYM-993. No reviews remain under owner 11-11.

## Reported experiment

The predecessor's experiment reached checkout, read the total, emptied the cart and returned
home, without ordering. All four runs reported the same cart total, Rp141400:

| Model | Effort | Seconds | Commands |
| --- | --- | ---: | ---: |
| gpt-6.1-terra | medium | 120 | 17 |
| gpt-6.1-terra | high | 127 | 20 |
| gpt-6.1-sol | high | 150 | 35 |
| gpt-6.1-sol | medium | 169 | 40 |

Report: @thread:thr_6z8k4wmd2r#msg=6374. Raw event 6275 contains the results and event 6220
the experiment script, accessible with `bb thread log thr_6z8k4wmd2r --all --json`.
Raw evidence stays on `macmini-tests:~/bare-codex-exp-20261112/` (`summary.txt`, per-model JSONL).
The predecessor reported wrapper runs of 149–357 seconds for comparable carts (raw events 6314
and 5677). These are a historical experiment report, not independently reverified
benchmarks or a controlled paired comparison. Turns were not reported; commands are not turns.
They motivate the Terra medium default and the wrapper replacement; they do not prove the final
integrated caller path or authorize placing an order.
