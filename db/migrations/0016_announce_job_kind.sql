-- migrate:up

-- A new job kind: the Historian queues 'announce' the moment a hypothesis
-- is retired or an accepted one is superseded, for a domain with a
-- Moltbook presence (roles/announcer.py, roles/historian.py).

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('scout', 'research', 'review', 'record', 'map', 'resolve',
                    'strategize', 'corroborate', 'ask', 'draft', 'reply', 'announce'));

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
