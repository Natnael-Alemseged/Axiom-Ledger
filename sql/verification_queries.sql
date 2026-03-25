-- Verification queries for seeded PostgreSQL event store (after datagen/generate_all.py).
-- Adjust database name / connection to match your .env.

-- 0) Required event-store tables exist (interim submission contract)
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('events', 'event_streams', 'projection_checkpoints', 'outbox')
ORDER BY table_name;

-- 0b) Required indexes exist (events + outbox hot paths)
SELECT indexname
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname IN (
    'idx_events_stream_id',
    'idx_events_global_pos',
    'idx_events_type',
    'idx_events_recorded',
    'idx_outbox_unpublished'
  )
ORDER BY indexname;

-- 1) Decision outcomes by recommendation (orchestrator output)
SELECT
  payload->>'recommendation' AS recommendation,
  COUNT(*) AS n
FROM events
WHERE event_type = 'DecisionGenerated'
GROUP BY 1
ORDER BY 1;

-- 2) Terminal application states (materialized from loan stream event types)
SELECT event_type, COUNT(*) AS n
FROM events
WHERE event_type IN (
  'ApplicationApproved',
  'ApplicationDeclined',
  'HumanReviewRequested'
)
GROUP BY event_type
ORDER BY event_type;

-- 3) Compliance hard blocks (REG-003 Montana, etc.)
SELECT
  payload->>'rule_id' AS rule_id,
  COUNT(*) AS n
FROM events
WHERE event_type = 'ComplianceRuleFailed'
  AND (payload->>'is_hard_block')::boolean = true
GROUP BY 1
ORDER BY 1;

-- 4) Append-only sanity check (no duplicate stream positions)
SELECT stream_id, stream_position, COUNT(*) AS n
FROM events
GROUP BY stream_id, stream_position
HAVING COUNT(*) > 1;
