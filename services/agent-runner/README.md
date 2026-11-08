# Agent runner (ANYM-819)

Runs one Claude Code or Codex task per disposable container inside the Mac mini's agent VM, within hard
limits. It reports each task's state, outcome, progress and log, and stops a task on request. This is S1 of the
MCP agents epic (ANYM-818). The gateway module is S2 (ANYM-820), the host, VM, logins and enable are S3
(ANYM-821), and the clean clone and publication to the fork are S4 (ANYM-822).

## API (tailnet 8448 with TLS, production's tailnet address only; loopback for the local CLI)

| Route | Key | |
|---|---|---|
| `POST /v1/tasks` | run | `engine`, `instruction`, `owner_instruction`, `request_id`, `max_minutes`, `continue_task_id` |
| `GET /v1/tasks/{id}` | read | state, outcome, summary, progress, publication, `next_action` |
| `GET /v1/tasks?request_id=` | read | the task of that request |
| `GET /v1/tasks/{id}/log?cursor=&raw=` | read | pages of at most 64 KiB, opaque cursor |
| `POST /v1/tasks/{id}/stop` | run | queued/starting: stopped without a run; running: `docker stop -t 20`, then kill |
| `GET /v1/capabilities` | read | per engine: configured, login state, caps, counts; paused, host gate, host check |

The principal comes from the caller key: the private config maps each key's SHA-256 to one principal and its
operations. A task is visible only to its own principal's keys. Refusals: 401 unknown or revoked key, 403 read
key, 404 another principal's task, 409 `request_id_conflict` / `engine_not_configured`, 422 invalid fields,
429 `runner_busy` (`retry_after_s`) / `daily_cap_reached` (`next_slot_at`), 503 `runner_paused` /
`host_check_failed`.

## Limits

- One running task per engine (two in total), two queued per engine, at most `daily_starts` (default 10, at
  most 20) admissions per rolling 24 hours. A start that ends `login_required` does not count.
- A queued task launches only while the host gate reads `ok`. After 60 minutes waiting for the host, it ends
  `failed` / `host_busy` without having run.
- Container: `--user` non-root, `--cap-drop ALL`, `no-new-privileges`, read-only root, `/tmp` tmpfs 256 MiB,
  `--memory 2500m` (no swap), `--pids-limit 512`, `--cpus 2`. It gets its own workspace volume (at most 3 GiB)
  and `/out` volume (at most 16 MiB), and the engine's login only. It never gets a host path, the Docker
  socket, runner config or a GitHub credential. Environment names are allowlisted in
  `src/deedkit_agent_runner/docker.py`.
- The prompt is `preamble.md` (versioned; each task records its SHA-256) followed by the instruction, on stdin.
- Logs are at most 5 MiB per task, then a marker line. A single output line is cut at 16 KiB and the rest of
  it dropped, so a container that never prints a newline cannot grow the runner's memory.

## Kill switch

`agent-runner stop-all --config <file>` creates the `pause` file in the state directory, then stops every
queued, starting and running task. `agent-runner resume --config <file>` removes the file. The launch takes the
runner's lock (`launch.lock` in the state directory, `flock`) for two steps: it re-checks in one SQLite
transaction that the task is still `starting`, not cancelled, and that no pause file exists, and then it starts
the container. `stop` and `stop-all` take the same lock. A stop therefore lands either before the start, and
no engine runs, or after it, and the running container is stopped.

A stopped or timed-out task keeps its engine's slot until its container is confirmed gone. The scheduler
retries the stop and removal on every tick, so a second task of that engine never runs beside a container
that is still stopping.

## Storage and retention

The state directory holds SQLite task rows, logs, patches (S4) and the audit file. The task container is
removed at its terminal state. Its volumes are removed once publication settles. Instruction, owner
instruction, summary, log and patch go 14 days after the terminal state. Dedup rows and the audit (hashes,
never the texts) go after 365 days. Containers run with `--log-driver none`: Docker keeps no copy of their
output, and the runner's capped log is the only one. Only the engines' login files persist across tasks. The
Codex login volume is emptied of everything except a regular `auth.json` before each Codex task, after it,
at restart and at the hourly sweep. Any entry the cleanup cannot remove fails the next Codex task before it
starts.

## Hooks the later slices fill

- Host gate and self-probe (S3): the configured `host_gate` / `host_check` commands print one JSON line,
  `{"state": "ok"}` or `{"state": "busy", "reason": "memory|disk|inference|tests"}`. Anything else, a non-zero
  exit or a timeout, reads busy or failed. Starts are refused while the probe reads `failed`.
- The task image (S3, `services/agent-runner/deploy/`): `image` must be pinned by digest. Both engines' pinned
  flags are checked with `--help` at startup, and a missing flag disables that engine.
- Workspace clone and publication (S4): `Runner(preparer=..., publisher=...)`. Until S4 is wired, nothing is
  cloned and publication settles `refused` with reason `publication_not_configured`.

## Tests

```sh
uv sync --locked --group dev && uv run pytest -q
```

Fake engines, a fake Docker (containers as local processes) and a fake host gate. `tests/test_docker_live.py`
runs real containers from `tests/live_image` (never a real engine). It runs only with
`AGENT_RUNNER_DOCKER_TESTS=required`, which the CI step sets: its restart recovery kills every container
labelled `deedkit.task` on the daemon.
