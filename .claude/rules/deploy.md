---
paths:
  - "deploy/**"
  - "scripts/production.py"
---
<!-- Sources: docs/operations/production.md, docs/operations/platform-cutover.md, AGENTS.md -->
# Deployment and production promotion
- The only route is PR -> required CI -> merge to `main` -> green **push** CI on that exact SHA -> `production.py verify|prepare --sha <sha>` -> review the candidate -> `apply --sha <sha>` -> `status`.
- Never deploy a feature branch, a dirty checkout, copied source, an ad hoc image overlay or `compose up --build`.
- The owner keeps (AGENTS.md section 2) restore, rollback over data, any production change other than `production.py deploy` and the accepted-task steps of AGENTS.md section 2 (migration apply, cleanup, feature enable), provider sessions and branch protection (`deploy/main-ruleset.json` is live; `deploy/main-protection.json` is the classic rollback, not applied).
- Preserve the host's Compose topology, state, sessions, mounts and private overlays. Select only the replaced app services; databases are never selected.
- Never `down -v`, prune shared Docker data or delete runtime data to free the 15 GiB minimum. Keep previous images until acceptance.
- Health checks: compose healthchecks for the messaging API, gateway and calendar API; collectors and workers are only checked as running. Sends, ACKs, calls and new sessions are never health probes.
- `deploy/private/` and `*.example.json` copies hold DSNs, keys, permits and session dirs. Keep them out of Git, images and logs.
- The promotion guard's refusals (mount roots, `PYTHON*`/`LD_*` env vars, privileged settings) are deliberate. Do not loosen them to make a candidate pass.
