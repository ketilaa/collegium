-- migrate:up

-- Collegium, Milestone 2.5: knowledge acquisition layer.
--
-- * acquisitions: every call to an external provider, with what was asked.
--   Answers "how did we find this source?" and "what have we revealed to
--   providers about what we research?". Append-only, like history.
--
-- * sources.acquisition_id: the discovery call that found a source.
--
-- * domains.discovery_sources: which discovery providers the Scout uses for
--   a domain, by provider name. Empty means the default web search. Names
--   are validated by the application, so adding a provider needs no
--   migration.

CREATE TABLE acquisitions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    capability   text NOT NULL CHECK (capability IN ('discover', 'extract')),
    provider     text NOT NULL,
    request      jsonb NOT NULL,        -- the query and parameters, or the urls
    result_count integer NOT NULL DEFAULT 0,
    error        text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    requested_by uuid NOT NULL DEFAULT current_actor() REFERENCES actors,
    run_id       uuid DEFAULT current_run() REFERENCES runs
);

CREATE INDEX acquisitions_run_idx ON acquisitions (run_id);
CREATE INDEX acquisitions_requested_at_idx ON acquisitions (requested_at DESC);

CREATE TRIGGER acquisitions_actor BEFORE INSERT ON acquisitions
    FOR EACH ROW EXECUTE FUNCTION check_session_actor();
CREATE TRIGGER acquisitions_provenance BEFORE INSERT ON acquisitions
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('requested_by');
CREATE TRIGGER acquisitions_append_only BEFORE UPDATE OR DELETE ON acquisitions
    FOR EACH ROW EXECUTE FUNCTION reject_change('table is append-only');
CREATE TRIGGER acquisitions_no_truncate BEFORE TRUNCATE ON acquisitions
    FOR EACH STATEMENT EXECUTE FUNCTION reject_change('memory is permanent');

ALTER TABLE sources ADD COLUMN acquisition_id uuid REFERENCES acquisitions;

ALTER TABLE domains ADD COLUMN discovery_sources text[] NOT NULL DEFAULT '{}';

GRANT SELECT ON acquisitions TO collegium_reader;
GRANT INSERT ON acquisitions TO collegium_worker;
GRANT UPDATE (discovery_sources) ON domains TO collegium_board;

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
