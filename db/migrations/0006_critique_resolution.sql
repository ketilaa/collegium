-- migrate:up

-- Collegium, Milestone 3: the critique-resolution loop.
--
-- A 'resolve' job has the Researcher investigate a hypothesis's open
-- critiques. The Skeptic then re-reviews and settles each critique
-- (upheld, addressed or dismissed, with a reason), and the Historian decides
-- the hypothesis's status from how its critiques were settled.

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('scout', 'research', 'review', 'record', 'map', 'resolve'));

-- Budget checks count recent paid calls.
CREATE INDEX acquisitions_provider_requested_at_idx ON acquisitions (provider, requested_at);

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
