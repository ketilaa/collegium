-- migrate:up

-- Collegium, Milestone 2.5: crawling owner-approved sources.
--
-- * approved_sources: sources the owner has approved for a domain, which the
--   Scout reads on every run without being asked. Only feeds (RSS, Atom)
--   for now. Approval is governance, so only the board may add or change
--   them, and changes are audited. Sources are paused or retired, never
--   deleted.
--
-- * acquisitions may now record 'crawl' calls.

CREATE TABLE approved_sources (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id  uuid NOT NULL REFERENCES domains,
    kind       text NOT NULL CHECK (kind IN ('feed')),
    url        text NOT NULL,
    title      text,
    status     text NOT NULL DEFAULT 'active'
               CHECK (status IN ('active', 'paused', 'retired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid NOT NULL DEFAULT current_actor() REFERENCES actors,
    UNIQUE (domain_id, url)
);

CREATE TRIGGER approved_sources_audit AFTER INSERT OR UPDATE OR DELETE ON approved_sources
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER approved_sources_provenance BEFORE INSERT OR UPDATE ON approved_sources
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('created_by');
CREATE TRIGGER approved_sources_no_delete BEFORE DELETE ON approved_sources
    FOR EACH ROW EXECUTE FUNCTION reject_change('pause or retire instead');
CREATE TRIGGER approved_sources_no_truncate BEFORE TRUNCATE ON approved_sources
    FOR EACH STATEMENT EXECUTE FUNCTION reject_change('memory is permanent');

GRANT SELECT ON approved_sources TO collegium_reader;
GRANT INSERT, UPDATE (title, status) ON approved_sources TO collegium_board;

ALTER TABLE acquisitions DROP CONSTRAINT acquisitions_capability_check;
ALTER TABLE acquisitions ADD CONSTRAINT acquisitions_capability_check
    CHECK (capability IN ('discover', 'extract', 'crawl'));

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
