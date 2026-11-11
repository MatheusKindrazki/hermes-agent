# Kindra operational integrations

These fork integrations are opt-in and scoped to the owning Hermes profile. Set
behavior in that profile's `config.yaml`; keep credentials in its secret store.
Explicit configuration, including `false` and empty strings, overrides the
legacy environment settings supported during migration.

| Setting | Default | Meaning |
| --- | --- | --- |
| `operations.shadow_producer_enabled` | `false` | Enable metadata-only kernel shadow receipts. |
| `operations.shadow_receipt_dir` | `""` | Absolute receipt spool directory; symbolic links are rejected. The producer uses private directory and file permissions. |
| `operations.tone_gate_bin` | `""` | Absolute executable for the tone-envelope gate. The gate runs without a shell and reads its adjacent `tone-policy.v1.json`. |
| `operations.tone_schema_sha256` | `""` | Lowercase SHA-256 pin for the tone schema. Missing or mismatched pins refuse tone-envelope evaluation. |
| `operations.work_control_projection_enabled` | `false` | Enable the read-only Work Control projection. |
| `operations.work_control_projection_url` | `""` | Endpoint consumed by the Work Control projection client. Configure only the intended private service. |

Enabling a projection does not complete Work Control items. Likewise, emitting
shadow receipts does not authorize external actions. These settings configure
integration plumbing; normal execution, approval, and collection policies remain
in force.
