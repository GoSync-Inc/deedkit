# Messaging source layout

The existing AnyMsg Python SDK and backend now live under `services/messaging`.
Their names, import paths, command entrypoints, versions and SQL contents are
unchanged. The service has its own `pyproject.toml` and `uv.lock`. Root `uv sync`
is a development facade installing this package; it does not install calendar,
voice or gateway dependencies. `python scripts/build.py` builds the same wheel
and sdist into root `dist/`. `uv build --project services/messaging` also works.

The root Dockerfile is a compatibility symlink to the module Dockerfile. Build
from repository root; Compose keeps the existing project, volumes and targets.
Production state never follows checkout paths. Source distributions contain the
legacy anymsg package layout and reviewed generic Node assets, without other
service code or private data. Their standalone Docker targets use that layout.

Python tests remain in `tests/`, Node collector sources and tests in `whatsapp/`,
and the JavaScript SDK remains in `packages/npm`. This deliberately avoids a
physical test move until a Tiden identity/source mapping has real CI readback.
The reviewed registry `config/tiden-test-identities.json` recorded 1,281 original
case IDs and matcher signatures; it does not normalize renamed parameters or
replace test identities. On 2026-10-18 three entries whose tests had been renamed
and whose Tiden records were deleted left it, so it holds 1,278. Existing eleven messaging component IDs are retained.
The gateway/calendar/calls tests have distinct qualified identities.

Run `uv run pytest tests` with an isolated `messaging_probe_test` DSN file, the
existing packaging checks, and module CI. Existing provider state and credential
files are never inputs to these tests. Installing the wheel outside the checkout
checks that imports come from the package rather than a leftover source tree.
