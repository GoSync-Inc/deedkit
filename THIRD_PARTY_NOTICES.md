# Third-party notices

AnyMsg source is MIT licensed by GoSync. Dependency licenses remain their own;
installation from npm/PyPI does not relicense those dependencies. The pinned
lock files are the dependency inventory. Preserve dependency notices when
redistributing installed environments or container images.

Message formatting uses [markdown-it-py](https://github.com/executablebooks/markdown-it-py)
and [mdurl](https://github.com/executablebooks/mdurl) (MIT), pinned in the Python
lock files. Raw HTML parsing is disabled in Markdown.

Direct components include [Telethon](https://github.com/LonamiWebs/Telethon)
(MIT), [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
(MIT), [Psycopg](https://github.com/psycopg/psycopg) (LGPL-3.0),
[Starlette](https://github.com/Kludex/starlette), [Uvicorn](https://github.com/Kludex/uvicorn)
and [HTTPX](https://github.com/encode/httpx) (BSD-3-Clause),
[whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js) and
[Puppeteer](https://github.com/puppeteer/puppeteer) (Apache-2.0).
Optional audio processing uses faster-whisper/CTranslate2; model weights have
separate terms and are not bundled. The npm HTTP SDK has no runtime dependencies.
Optional runtime diagnostics use [tiden-telemetry](https://github.com/qase-tms/tiden-telemetry-python)
(MIT), pinned in `services/messaging/requirements-telemetry.lock`. Sending is disabled by default.

The Chromium seccomp policy derives from Moby. Its provenance and Apache-2.0
license are retained in `deploy/chromium-seccomp-SOURCE.md` and
`deploy/MOBY-LICENSE`. The backup Docker target downloads the pinned age binary;
its BSD-3-Clause license is retained in `licenses/age-LICENSE` and that image.
Docker bases, PostgreSQL, Node, Chromium, OCR and audio system packages retain
upstream notices; Dockerfiles record installed package inventories. AnyMsg does
not ship those binaries in the wheel/npm tarball and does not publish container
images in this release. This is an inventory of direct components, not a claim
that every transitive dependency or model has completed a license audit.

The optional native WhatsApp adapter uses [whatsmeow](https://github.com/tulir/whatsmeow)
(MPL-2.0), pinned with its transitive dependencies in `whatsapp/whatsmeow/go.mod`
and `go.sum`. Our adapter is separate MIT source. The build replaces whatsmeow with
a modified version: the fork https://github.com/GoSync-Inc/whatsmeow at revision
fa2ca87d4bd8 (branch `deedkit/f16f9ca30d6a`), which is upstream commit f16f9ca30d6a
(https://github.com/tulir/whatsmeow/tree/f16f9ca30d6a) plus one commit: two hunks in
`request.go` and the test file `deedkit_fork_test.go`, recorded exactly in
`whatsapp/whatsmeow/fork.patch`. The modified files remain MPL-2.0 with their upstream
notices; their source is the fork revision above and `fork.patch` in the source
archive (MPL-2.0 section 3.2). Source archives retain the adapter sources, Go locks
and `fork.patch`. Native container images retain the module inventory (`modules.txt`
lists the replacement) and dependency license texts under `/app/whatsmeow-licenses`;
the Python wheel does not bundle a Go executable. Preserve the source availability
and license obligations when redistributing a native binary.
