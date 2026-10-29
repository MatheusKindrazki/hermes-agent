# Operational settings in the fork

Behavioral settings live in the owning profile's `config.yaml`:

```yaml
operations:
  shadow_producer_enabled: false
  shadow_receipt_dir: /absolute/private/receipt/directory
  tone_gate_bin: /absolute/path/to/tone-gate
  tone_schema_sha256: <trusted schema digest>
  work_control_projection_enabled: false
  work_control_projection_url: https://your-authority.example/projection
```

The Work Control read credential remains `HERMES_WORK_CONTROL_READ_TOKEN` in
that profile's secret scope. It is never returned to the renderer.

`hermes_cli.operational_settings` explicitly translates the six historical
fork deployment variables only when the corresponding config key is absent.
This preserves existing deployments during upgrade. An explicit `false` or
empty config value wins; environment values cannot silently re-enable it.
The compatibility reader uses the existing profile-scoped configuration API,
not the launch process environment. New behavior must use config keys.
To migrate, copy each old setting to the corresponding key above and remove
its legacy deployment variable. Do not copy credentials into config.yaml.

No installed app configuration or running gateway is changed by the source
refactor. Migration is applied by the operator during a future runtime update.

## Refactor ownership

The integration preserves the fork's existing behavior with smaller implementation owners:

- `gateway/run_notifications_watch.py`: process-event notification routing and in-flight delivery.
- `gateway/run_turn_display.py`: platform-specific turn presentation.
- `gateway/run_media_history.py`: history and produced-media deduplication.
- `gateway/platforms/api_server_execution.py`: isolated API turn execution.
- `gateway/platforms/base_history_media.py`: transcript media extraction.
- `gateway/relay/adapter_draft.py`: relay draft publication and sealing.
- `hermes_cli/kanban_worker_command.py`: profile-scoped worker commands and toolsets.
- `hermes_cli/kanban_worker_path.py`: immutable, hash-verified worker environment snapshots.
- `hermes_cli/kanban_db_artifacts.py`: completion artifact preservation.
- `cron/scheduler_bot_notification.py`: passive Bot Chat notifications.
- `tools/bot_delivery_runtime_env.py`: release-local, profile-scoped transport environment.

Desktop presentation, route ownership guards, avatar animation, viewport geometry and
file metadata each have separate helpers. The active-context identity gate and single
canonical conversation per character remain authoritative. The parallel panel stays
inside the app and retains its live conversation and steering controls.
