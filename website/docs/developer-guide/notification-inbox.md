# Isolated automation inbox

To keep scheduled updates out of an interactive conversation, set this in
the receiving scheduler profile's `config.yaml`:

```yaml
notifications:
  isolated_inbox: true
```

Scheduled deliveries appear in the visible **Atualizações automáticas** session
in that same profile. This is a passive local inbox: receipt of a notification
does not invoke a model or continue the human's Bot Chat. The preference is read
at delivery time. It defaults off; remove it or set it to false to restore the
job's configured destination. Local-only jobs still retain output in cron history.

This setting replaces all non-local scheduled destinations, including Telegram,
origin and Bot Chat delivery. It does not forward notifications to another
profile, even if the job previously specified a different destination. Jobs,
their schedules and their active/paused states are unchanged.

The companion Personal OS Work Control monitor honors the same preference and
retains original Work/session identifiers in the notification. It renders the
ledger digest directly rather than spending a model turn in the human chat.

Repeated delivery is deduplicated within the inbox's compression lineage. A
conflicting human-owned title, a busy inbox, or failed persistence is reported
as a delivery error; none falls back to the main chat. Existing cron history and
the monitor's durable reservation/error ledger remain the recovery source.

This isolates scheduler and Work monitor notifications, not explicit teammate
handoffs via `message_agent`, human messages, or tool responses in an ongoing task.
