-- migrate:up

-- Structured details of a decision, for decisions that do something when
-- approved. A proposed source (topic 'source') carries the feed it would
-- add: {"domain_id", "site", "feed_url", "title", "uses"}. Approving it adds
-- the feed to the domain.

ALTER TABLE decisions ADD COLUMN details jsonb;

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
