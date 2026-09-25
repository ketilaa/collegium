-- migrate:up

-- The points of an answer, each with its source numbers and how firmly the
-- organization holds it: set by code from the sources' kinds and statuses,
-- not by the model's wording.

ALTER TABLE questions ADD COLUMN points jsonb;

GRANT UPDATE (points) ON questions TO collegium_worker;

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
