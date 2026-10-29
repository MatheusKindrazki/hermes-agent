# Maintaining the Kindra fork

The fork keeps the upstream commit history and merges upstream into an integration
branch. Do not replace a running checkout with the upstream repository or install an
upstream Desktop binary over the customized application.

## Integration contract

Compare both sides of a conflict with their merge base. Upstream frequently extracts
functions into modules: resolving the old facade is insufficient unless the fork's
behavior is restored at the new call sites. Preserve these boundaries:

- Durable admission and authenticated native input identity remain profile scoped.
- Delivery ACKs identify a persisted user row. A transport acceptance or model reply
  alone is not a delivery ACK. An envelope targeting a live canonical Bot Chat is
  refused until its owner releases it; it must not create a second writer.
- API run records from the legacy ledger remain readable. Importing an unattached
  claim leaves a tombstone before execution; imported and explicit request-digest
  keys retain their records permanently. Volatile storage cannot admit an
  idempotent run. Hosted-room grants cannot read the legacy owner's ledger.
- Local notification inboxes settle before external connectors, including mixed
  fan-out and failed connector configuration.
- Compression preserves the current turn, attachment/checkpoint and stable inbound
  identity. Upstream's bounded summary replaces the former auxiliary chunk maps;
  automatic recovery above one million tokens stays local.
- Targeted Kanban dispatch validates its signed worker PATH before effects and uses
  a pinned snapshot. Keep `HERMES_WORKER_PATH_LIB` and `K5_WORKER_PATH_SHA256` in the
  runtime environment.

Run the standard Python test runner against a disposable PM-built environment,
Desktop type checks, affected Vitest suites and the active-context Electron E2E.
Use mock providers for integration tests. Record skipped platforms explicitly.
The fork uses standard GitHub-hosted runners, not the upstream organization's
private large-runner labels. Contributor checking excludes only the recorded
upstream integration ancestor; new fork contributions still require attribution.

## Release contract

Build an immutable source archive from the tested integration commit. Install each
machine's Python environment at its final release path with `pm.build_env`; do not
copy a venv between machines. Include the enabled platform/plugin dependencies.

Write `install-stamp.json` with the official `scripts/write_install_stamp.py`, the
full commit, `--source commit-build` and `--update-mechanism external`. The source
value matters: a plain `local` stamp with only `external` does not enforce the
commit-build update refusal. Keep the legacy `.hermes_build_sha` only for older
supervisor receipts; current runtime identity comes from `version_info`.

Build a thin Desktop with `GITHUB_REPOSITORY=MatheusKindrazki/hermes-agent` and
`--publish never`, without a public update-feed URL; inspect the artifact's stamp
and ensure it has no upstream update feed. Keep the stable application identity
so saved connections remain available. Pin its local backend with the private
`backend-runtime.json` file in Electron userData (`version: 1`, absolute `root`,
optional absolute `python`). A missing/invalid pinned installation must fail
rather than bootstrap upstream. Remote connections do not use this pin. A runtime
CLI upgrade does not upgrade the Electron application.

Before promotion, preserve the old application, launch definitions, wrapper and
configuration, and take consistent SQLite backups. Drain active turns, change all
runtime paths together (Python, `PYTHONPATH`, `VIRTUAL_ENV`, working directory and
CLI wrapper), then start only the services that were previously running. Preserve
credentials and model configuration.

Verify the loaded process's `code_sha`, per-profile health, Desktop/backend identity
and a controlled execution. Rollback restores executable pointers and service
configuration; database snapshots must not be restored automatically after new
messages have arrived.
