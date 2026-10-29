# Releasing AnyMsg

The public repository is a fresh source export, not the Personal OS git history.
Never publish correspondence, operational state, backups, session data or tokens.
Python and npm distribution versions must match `messaging_service.__version__`.
The current source version is 0.9.19 (experimental), with PostgreSQL schema 56.
The published npm/PyPI release remains 0.9.9 until a separate registry release.
Sending is implemented in the source with explicit account opt-in and per-key
permissions; see [SENDING.md](SENDING.md).

1. Run CI with synthetic PostgreSQL, Python and Node tests. Build the wheel,
   sdist and both npm tarballs (`anymsg` and `deedkit`) with `python scripts/build.py`,
   then run `uv run python scripts/check_release.py <version>`. Inspect their
   inventories; install them outside the checkout and verify CLI/help/imports
   and immutable SQL/Node assets.
2. Review staged paths and diff, plus private-data scans. Commit and tag the
   reviewed source. Keep deployment upgrades separate from registry releases.
   Production gets a messaging migration by `deploy` itself when the range qualifies (an added
   `expand` migration and appended role grants, a verified copy, a rehearsal on it, ANYM-449), else by
   hand ([production.md](docs/operations/production.md), "Unattended migrations" and "Migration releases").
3. Configure PyPI Trusted Publishing, then dispatch `publish-pypi.yml` on the tag.
4. For the first npm release, an authorized maintainer publishes the inspected
   tarball through `npm publish --access public`, completing the registry's
   owner authentication/2FA flow. Do not put OTPs or tokens in an issue/chat.
5. After that initial npm package exists, configure its Trusted Publisher for
   `GoSync-Inc/deedkit`, workflow `publish-npm.yml`, environment `npm`. Select
   direct publishing permission if using this workflow (not staged-only).
   A new npm publisher must successfully publish within two days or its
   configuration expires. Validate it with the next reviewed patch release;
   an already published version cannot be overwritten.
6. Verify the public registry versions and hashes, then link them from the
   GitHub prerelease. Never report publishing success based only on a build.

## deedkit npm first publication

`deedkit` (`packages/deedkit`) is the gateway client and CLI; `anymsg` keeps its
name for compatibility. Both carry the messaging release version. CI does not
build or publish `deedkit` yet, so its first version is published by hand by the
owner, from the tagged source, after the tag's required CI is green:

1. From a clean checkout of tag `v<version>`, run `python scripts/build.py` and
   `uv run python scripts/check_release.py <version>`. List the tarball with
   `tar tzf dist/deedkit-<version>.tgz`: only `package.json`, `README.md`,
   `LICENSE`, `THIRD_PARTY_NOTICES.md`, `lib/` and `bin/`.
2. In an empty directory outside the checkout, run
   `npm install --ignore-scripts <checkout>/dist/deedkit-<version>.tgz`, then
   `./node_modules/.bin/deedkit --version` and `./node_modules/.bin/deedkit tools`.
3. `npm view deedkit` must still answer `E404` (the name is free). If it exists,
   stop: someone else owns it.
4. Sign in with `npm login` as the owning npm account, which has two-factor
   authentication, and run `npm publish <checkout>/dist/deedkit-<version>.tgz
   --access public`, completing the 2FA prompt in the terminal. Publish the
   inspected tarball, not the source directory. Do not create an automation
   token and do not paste one-time codes into an issue or chat. A manual
   publication carries no provenance attestation.
5. Check `npm view deedkit@<version> version dist.shasum dist.integrity`
   against `shasum dist/deedkit-<version>.tgz` and the integrity printed by
   `npm pack`, then record the receipt with the GitHub prerelease.
6. In the package's npm settings, set publishing access to require 2FA and
   disallow tokens. Configure its Trusted Publisher (GitHub Actions,
   `GoSync-Inc`/`deedkit`, workflow `publish-npm.yml`, environment `npm`) only
   when that workflow can publish `deedkit`: a new publisher must publish within
   two days or its configuration expires. Today `publish-npm.yml` publishes the
   `npm-dist` artifact, which `checks.yml` fills only with `anymsg`. The pending
   follow-up packs `deedkit` and runs its tests in `checks.yml`, and makes
   `publish-npm.yml` publish each tarball by its exact name; a `dist/*.tgz` glob
   that matches two tarballs must not reach `npm publish`, so the publish change
   lands no later than `checks.yml` starts packing `deedkit`. Until then, later
   `deedkit` versions are published by hand the same way.

## PyPI first publication without a stored token

At https://pypi.org/manage/account/publishing/ add a **pending publisher**:

- PyPI project: `anymsg`
- GitHub owner: `GoSync-Inc`
- Repository: `deedkit`
- Workflow filename: `publish-pypi.yml`
- Environment: `pypi`

The GitHub environment must have exactly that name. The first successful OIDC
publication creates the project for the configuring PyPI account. The account
must satisfy PyPI's verification/2FA requirements. Existing projects configure
publishers in their project settings instead. No PyPI token is committed or
stored as a GitHub secret.

Both release workflows take a `version` input. PyPI requires the selected
matching git tag. npm may run from that tag or from `main`; in either case its
build-and-test job explicitly checks out `refs/tags/v<version>`, never untagged
source. This lets the publication workflow be repaired without moving an already
published release tag.
They fail if a package/version differs or if the version is already registered.
A shared reusable build-and-test workflow validates the source before upload.
Actions have minimum permissions; the upload job alone receives `id-token: write`.
Do not run publication on a pull request from an untrusted contributor.

Official guides: [PyPI pending publishers](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [npm trusted publishers](https://docs.npmjs.com/trusted-publishers/).

## Check publisher trust without publishing

Dispatch the existing registry workflow on `main` with `verify_only=true`.
It exchanges the workflow identity for a short-lived registry credential, keeps
it only in process memory, and records a sanitized success/failure result. It
does not build, upload, stage, tag or change a version. This confirms the OIDC
binding after a repository rename; package release verification remains separate.
