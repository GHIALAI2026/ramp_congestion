# Database migrations

One-shot SQL scripts that fix up DB state on existing deployments. These
are **not** auto-applied by the cloud server — they're operator-run when a
deployment needs to catch up with a structural or data change that
happened after the original install.

Scripts are named `YYYY-MM-DD_HHMM_<short_description>.sql` so the order
is obvious from `ls`. Every script should be:

- **Idempotent** — re-running on a deployment that's already migrated
  does nothing harmful (use `WHERE old_value` predicates so each UPDATE
  is a no-op the second time).
- **Atomic** — wrap in `BEGIN; ... COMMIT;` so a partial failure
  rolls back cleanly.
- **Self-documenting** — top comment explaining what changed and why.

To apply on a fresh deployment that started from `schema.sql`: skip them,
the canonical schema already reflects the post-migration state.

To apply on a deployment that needs to catch up:

```bash
PGPASSWORD=apexedge psql -U apexedge -h localhost -d vehicle_zone \
    -f db/migrations/<filename>.sql
```

After data migrations that affect zone_ids or camera_ids, also:

1. Purge stale Redis keys whose ID no longer exists in the DB
   (`vzone:<old_id>:latest`, `vzone:entry:<old_id>:*`).
2. SIGKILL the edge so the supervisor relaunches it with fresh zone
   configs from the API.
