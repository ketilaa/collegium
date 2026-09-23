-- migrate:up

-- Collegium, Milestone 2: research workflow.
--
-- * jobs: the work queue. The worker claims pending jobs with
--   FOR UPDATE SKIP LOCKED; finishing a job enqueues the next step
--   (scout -> research -> review -> record). Jobs are operational state,
--   not knowledge, so they are not audited; the runs they produce are.
--
-- * A 'service' actor kind for infrastructure that acts on its own, such as
--   the scheduler. Services are neither agent roles nor the system.

ALTER TABLE actors DROP CONSTRAINT actors_kind_check;
ALTER TABLE actors ADD CONSTRAINT actors_kind_check
    CHECK (kind IN ('owner', 'role', 'service', 'system'));

SELECT set_config('collegium.actor_id', '00000000-0000-0000-0000-000000000001', true);

INSERT INTO actors (kind, name, description) VALUES
    ('service', 'scheduler', 'Enqueues recurring work, such as scouting each active domain.');

CREATE TABLE jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          text NOT NULL CHECK (kind IN ('scout', 'research', 'review', 'record')),
    payload       jsonb NOT NULL DEFAULT '{}',
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    priority      smallint NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    attempts      smallint NOT NULL DEFAULT 0,
    max_attempts  smallint NOT NULL DEFAULT 3,
    run_after     timestamptz NOT NULL DEFAULT now(),
    requested_by  uuid NOT NULL DEFAULT current_actor() REFERENCES actors,
    parent_job_id uuid REFERENCES jobs,
    run_id        uuid REFERENCES runs,
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz
);

CREATE INDEX jobs_pending_idx ON jobs (priority, run_after) WHERE status = 'pending';
CREATE INDEX jobs_kind_created_idx ON jobs (kind, created_at DESC);

-- The actor checks from audit_row, extracted so unaudited tables such as
-- jobs get them too.
CREATE FUNCTION assert_session_actor(target text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    actor_kind text;
BEGIN
    SELECT kind INTO actor_kind FROM actors WHERE id = current_actor();
    IF actor_kind IS NULL THEN
        RAISE EXCEPTION 'write to % without a valid collegium.actor_id', target;
    END IF;
    IF actor_kind = 'owner' AND NOT pg_has_role(session_user, 'collegium_board', 'MEMBER') THEN
        RAISE EXCEPTION '% may not act as the owner', session_user;
    END IF;
    -- Inside SECURITY DEFINER, current_user is the schema owner.
    IF actor_kind = 'system' AND NOT pg_has_role(session_user, current_user, 'MEMBER') THEN
        RAISE EXCEPTION '% may not act as the system', session_user;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION audit_row() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    old_j jsonb;
    new_j jsonb;
BEGIN
    PERFORM assert_session_actor(TG_TABLE_NAME);
    IF TG_OP <> 'INSERT' THEN old_j := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN new_j := to_jsonb(NEW); END IF;
    IF TG_OP = 'UPDATE' AND old_j = new_j THEN
        RETURN NULL;
    END IF;
    INSERT INTO audit_log (actor_id, run_id, db_user, table_name, row_id, action, old_row, new_row)
    VALUES (current_actor(), current_run(), session_user, TG_TABLE_NAME,
            (coalesce(new_j, old_j) ->> 'id')::uuid, TG_OP, old_j, new_j);
    RETURN NULL;
END
$$;

CREATE FUNCTION check_session_actor() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM assert_session_actor(TG_TABLE_NAME);
    RETURN NEW;
END
$$;

CREATE TRIGGER jobs_actor BEFORE INSERT ON jobs
    FOR EACH ROW EXECUTE FUNCTION check_session_actor();
CREATE TRIGGER jobs_provenance BEFORE INSERT OR UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('requested_by');
CREATE TRIGGER jobs_no_delete BEFORE DELETE ON jobs
    FOR EACH ROW EXECUTE FUNCTION reject_change('keep job history');
CREATE TRIGGER jobs_no_truncate BEFORE TRUNCATE ON jobs
    FOR EACH STATEMENT EXECUTE FUNCTION reject_change('keep job history');

GRANT SELECT ON jobs TO collegium_reader;
GRANT INSERT ON jobs TO collegium_worker;
GRANT UPDATE (status, attempts, run_after, run_id, last_error, started_at, finished_at)
    ON jobs TO collegium_worker;

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
