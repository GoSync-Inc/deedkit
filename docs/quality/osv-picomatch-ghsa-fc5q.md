# picomatch GHSA-fc5q-39xc-vjpf: accepted build-time exposure until 2026-12-02

The vulnerability is **not removed**. osv-scanner.toml allowlists this one advisory until
2026-12-02T00:00Z under card ANYM-1002. The card bumps picomatch once a fixed release exists and
then deletes the entry. An expired entry fails the Security report again.

## Advisory (read 2026-11-18)
- GHSA-fc5q-39xc-vjpf / CVE-2026-112366, high (CVSS 7.5 at GitHub, 8.7 at OSV), CWE-1333,
  updated 2026-11-17T15:56Z. `analyzeRepeatedExtglob` in lib/parse.js skips single-branch
  repeated extglobs such as `+(*)` and `+(*a)`, and the result backtracks exponentially on short
  strings. The attacker must supply the **pattern**.
- Affected `<= 4.0.7`, first patched version: none. npm `latest` is 4.0.7, published 2026-08-24;
  no later version exists, so a downgrade fixes nothing. micromatch/picomatch has open, unmerged
  fix pull requests (#237, #238, #259, #262, #265–#270).

## Where picomatch is
All four tracked lockfiles were scanned. The only copy is
`services/calls/frontend/package-lock.json`: picomatch 4.0.7, `dev`. It is required by vite
8.3.4 (`^4.0.7`), tinyglobby 0.2.17 (`^4.0.4`) and fdir 6.5.0 (optional peer). packages/deedkit,
packages/npm and whatsapp (the Node runtime fallback) have none. The runtime dependencies
@pipecat-ai/client-js and small-webrtc-transport do not use it.

## Why the exposure is build-time only
- The frontend has no vite config, and its scripts are only `vite build` and `node --test`. No
  `vite dev`, `vite preview` or `npx vite` runs anywhere in the repository. Patterns are therefore
  Vite's defaults plus repository files, all trusted input.
- picomatch runs in checks / verify (step `calls_frontend`) and in the `frontend` stage of
  services/calls/Dockerfile. The final image copies only `/frontend/dist`, and app.py serves it
  as static files.
- A build of b2760dcd contained no picomatch code in dist: 0 matches for picomatch,
  analyzeRepeatedExtglob, POSIX_REGEX_SOURCE and isExtglob in `dist/assets/*.js` and
  index.html.
- Remaining risk: a malicious change to the repository's own build input could stall a CI build
  or image build. That is denial of service of the build, with no data exposure.

tests/test_osv_npm_exception_boundary.py keeps this boundary. Because osv-scanner filters the ID
in every lockfile, the test fails if:
- a copy appears in a lockfile the reason does not name,
- a copy loses `dev` or has another version, or
- the calls image copies more than dist, or a vite config or new script appears.

## Proof
Pinned osv-scanner 2.6.0 (sha256 ca69b3d3…b108), security.yml command:
- Before: exit 1, one finding, GHSA-fc5q-39xc-vjpf, picomatch (dev) 4.0.7, fixed version `--`.
- After: exit 0, with the advisory and 1 alias filtered by this entry.
- Mutation proof: the boundary test fails for a production copy, a whatsapp copy and a Dockerfile
  that copies the whole frontend.
- Calls frontend: 23 tests pass and `vite build` succeeds.

## Removal
1. Under ANYM-1002, when a fixed picomatch is released, bump the lock with normal npm integrity
   and delete the entry.
2. If no fixed release exists by 2026-12-02, restate fresh evidence with at most 14 more days, or
   replace the dependency path. Never extend the entry silently.
