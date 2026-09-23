-- migrate:up

-- Collegium, Milestone 3: the Strategist.
--
-- * 'strategize' jobs: the Strategist reviews a domain's state, sets goals
--   and queues work within the budget. The scheduler queues one per active
--   domain each day, instead of a fixed scout.
-- * 'corroborate' jobs: the Researcher looks for independent evidence on a
--   hypothesis, from sites not yet cited.
--
-- Goals are created active by the Strategist (the owner's decision); new
-- research programs are created 'proposed' together with a decision that
-- only the board can resolve.

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('scout', 'research', 'review', 'record', 'map', 'resolve',
                    'strategize', 'corroborate'));

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
