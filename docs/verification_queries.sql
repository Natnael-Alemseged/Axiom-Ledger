-- Verification queries for seeded PostgreSQL event store (after datagen/generate_all.py).
-- Adjust database name / connection to match your .env.

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
