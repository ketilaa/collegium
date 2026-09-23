-- migrate:up

-- Collegium, Milestone 2.5: related entities.
--
-- A 'map' job records which entities (organisations, people, products,
-- technologies) an observation and its evidence mention, as entity nodes
-- linked by 'mentions' relationships. It runs after research.

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('scout', 'research', 'review', 'record', 'map'));

CREATE INDEX relationships_predicate_object_idx
    ON relationships (predicate, object_id) WHERE retracted_at IS NULL;
CREATE INDEX entities_aliases_idx ON entities USING gin (aliases);

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
