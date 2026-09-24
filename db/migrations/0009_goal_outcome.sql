-- migrate:up

-- Why a goal was closed: the Strategist's reason for abandoning it, or what
-- achieved it. The status change itself is in the audit log; this keeps
-- the reason with the goal, so "why did we stop pursuing this?" has an
-- answer.

ALTER TABLE goals ADD COLUMN outcome text;

GRANT UPDATE (outcome) ON goals TO collegium_worker;

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
