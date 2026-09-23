-- migrate:up

-- Collegium, Milestone 1: institutional memory.
--
-- Design:
--
-- * Every knowledge record (entity, observation, hypothesis, evidence,
--   critique, relationship, program, goal, decision) is a row in `nodes`
--   plus a row in its own table with the same id. This gives all knowledge
--   one id space, so relationships, critiques, evidence, confidence, domain
--   tags and audit entries can point at any record with a real foreign key.
--
-- * Domains are data, not schema. Adding a research area is an INSERT.
--
-- * Provenance is mandatory. Each transaction that writes must first run
--       SELECT set_config('collegium.actor_id', '<actor uuid>', true);
--       SELECT set_config('collegium.run_id',   '<run uuid>',   true); -- optional
--   Writes without an actor are rejected, and every created_by-style
--   column must equal that actor.
--
-- * History is never overwritten:
--     - audit_log records every insert/update/delete with before/after rows.
--     - confidence_assessments is append-only; current confidence is the
--       latest row.
--     - Knowledge rows cannot be deleted. They are retired through status,
--       and links are retracted with retracted_at.
--     - Statements are immutable. A changed claim is a new record that
--       supersedes the old one.
--
-- * Enforcement has two layers:
--     - Database roles (bottom of file) decide what each process may do:
--       collegium_reader, collegium_worker (agent roles), collegium_board
--       (the owner's interface). Login users are created per deployment and
--       granted one of these.
--     - Triggers enforce what must hold for everyone, including the schema
--       owner: audit, provenance, no deletes, append-only history.
--
-- * Vocabularies that will grow (statuses, stances) are text + CHECK rather
--   than enum types, so they can be changed with a plain ALTER.
--
-- Applied by dbmate, which wraps the file in one transaction. Run as a role
-- that can create roles (CREATEROLE) and will own the schema.

-- ---------------------------------------------------------------------------
-- Group roles
-- ---------------------------------------------------------------------------

-- Roles are cluster-wide, so they may already exist from another database.
DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['collegium_reader', 'collegium_worker', 'collegium_board']
    LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END
$$;

-- board ⊃ worker ⊃ reader
GRANT collegium_reader TO collegium_worker;
GRANT collegium_worker TO collegium_board;

-- ---------------------------------------------------------------------------
-- Session context
-- ---------------------------------------------------------------------------

CREATE FUNCTION current_actor() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('collegium.actor_id', true), '')::uuid
$$;

CREATE FUNCTION current_run() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('collegium.run_id', true), '')::uuid
$$;

-- ---------------------------------------------------------------------------
-- Who acts, and in which run
-- ---------------------------------------------------------------------------

-- The owner, each agent role, and the system itself.
CREATE TABLE actors (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL CHECK (kind IN ('owner', 'role', 'system')),
    name        text NOT NULL UNIQUE,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- One execution of a role. model and role_version record which LLM and
-- which role definition produced a piece of knowledge, so agents can be
-- replaced without losing track of where beliefs came from.
CREATE TABLE runs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id     uuid NOT NULL REFERENCES actors,
    status       text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'succeeded', 'failed')),
    model        text,
    role_version text,
    input        jsonb NOT NULL DEFAULT '{}',
    notes        text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

-- ---------------------------------------------------------------------------
-- Domains and sources
-- ---------------------------------------------------------------------------

CREATE TABLE domains (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    name        text NOT NULL,
    description text,
    status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'retired')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  uuid NOT NULL DEFAULT current_actor() REFERENCES actors
);

-- Where information came from. content_sha256 lets us notice when a
-- source changes after we read it.
CREATE TABLE sources (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    uri            text,
    title          text,
    publisher      text,
    published_at   timestamptz,
    retrieved_at   timestamptz NOT NULL DEFAULT now(),
    content_sha256 text,
    metadata       jsonb NOT NULL DEFAULT '{}',
    created_by     uuid NOT NULL DEFAULT current_actor() REFERENCES actors,
    created_in_run uuid DEFAULT current_run() REFERENCES runs
);

CREATE INDEX sources_uri_idx ON sources (uri);

-- ---------------------------------------------------------------------------
-- Knowledge nodes
-- ---------------------------------------------------------------------------

CREATE TABLE nodes (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind           text NOT NULL CHECK (kind IN (
                       'entity', 'observation', 'hypothesis', 'evidence',
                       'critique', 'relationship', 'program', 'goal',
                       'decision')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     uuid NOT NULL DEFAULT current_actor() REFERENCES actors,
    created_in_run uuid DEFAULT current_run() REFERENCES runs,
    UNIQUE (id, kind)
);

-- A node can belong to several domains; cross-domain knowledge is normal.
CREATE TABLE node_domains (
    node_id   uuid NOT NULL REFERENCES nodes,
    domain_id uuid NOT NULL REFERENCES domains,
    PRIMARY KEY (node_id, domain_id)
);

CREATE INDEX node_domains_domain_idx ON node_domains (domain_id);

-- Each subtype table repeats `kind` with a fixed value so the composite
-- foreign key guarantees e.g. a hypotheses row points at a 'hypothesis' node.

-- Things in the world: companies, people, technologies, countries...
-- entity_type is free text so each domain can use its own vocabulary.
CREATE TABLE entities (
    id          uuid PRIMARY KEY,
    kind        text NOT NULL DEFAULT 'entity' CHECK (kind = 'entity'),
    entity_type text NOT NULL,
    name        text NOT NULL,
    aliases     text[] NOT NULL DEFAULT '{}',
    description text,
    attributes  jsonb NOT NULL DEFAULT '{}',
    status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'merged', 'retired')),
    merged_into uuid REFERENCES entities,
    CHECK ((status = 'merged') = (merged_into IS NOT NULL)),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

CREATE INDEX entities_type_name_idx ON entities (entity_type, lower(name));

-- Something noticed, mostly proposed by the Scout.
CREATE TABLE observations (
    id             uuid PRIMARY KEY,
    kind           text NOT NULL DEFAULT 'observation' CHECK (kind = 'observation'),
    statement      text NOT NULL,
    occurred_at    timestamptz,          -- when the observed event happened, if known
    source_id      uuid REFERENCES sources,
    recommendation text,                 -- why it may deserve investigation
    status         text NOT NULL DEFAULT 'proposed'
                   CHECK (status IN ('proposed', 'investigating', 'accepted', 'dismissed')),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

CREATE INDEX observations_status_idx ON observations (status);

-- A claim the organization may come to believe. Current confidence lives
-- in confidence_assessments, not here.
CREATE TABLE hypotheses (
    id            uuid PRIMARY KEY,
    kind          text NOT NULL DEFAULT 'hypothesis' CHECK (kind = 'hypothesis'),
    statement     text NOT NULL,
    rationale     text,
    status        text NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed', 'under_review', 'accepted',
                                    'rejected', 'superseded', 'retired')),
    superseded_by uuid REFERENCES hypotheses,
    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

CREATE INDEX hypotheses_status_idx ON hypotheses (status);

-- A specific piece of information, usually a claim taken from a source.
CREATE TABLE evidence (
    id          uuid PRIMARY KEY,
    kind        text NOT NULL DEFAULT 'evidence' CHECK (kind = 'evidence'),
    summary     text NOT NULL,
    excerpt     text,                    -- verbatim text from the source
    source_id   uuid REFERENCES sources,
    reliability numeric(3,2) CHECK (reliability BETWEEN 0 AND 1),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

-- An objection to any record, mostly raised by the Skeptic. Critiques are
-- nodes themselves, so they can be critiqued, linked and backed by evidence.
CREATE TABLE critiques (
    id                      uuid PRIMARY KEY,
    kind                    text NOT NULL DEFAULT 'critique' CHECK (kind = 'critique'),
    target_id               uuid NOT NULL REFERENCES nodes,
    argument                text NOT NULL,
    alternative_explanation text,
    severity                smallint NOT NULL DEFAULT 3 CHECK (severity BETWEEN 1 AND 5),
    status                  text NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'upheld', 'addressed', 'dismissed')),
    resolution              text,        -- how it was upheld, addressed or dismissed
    CHECK (status = 'open' OR resolution IS NOT NULL),
    CHECK (target_id <> id),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

CREATE INDEX critiques_target_idx ON critiques (target_id) WHERE status = 'open';

-- Typed links between any two nodes: subject –predicate→ object.
-- Examples: (company) employs (person), (hypothesis) derived_from
-- (observation), (goal) investigates (hypothesis), (critique) cites
-- (evidence). Relationships are nodes, so they carry evidence, confidence
-- and critiques like any claim. predicate is free snake_case text so
-- domains need no schema change. valid_from/valid_until describe when the
-- relationship held in the world; nodes.created_at/retracted_at describe
-- when we believed it.
CREATE TABLE relationships (
    id                uuid PRIMARY KEY,
    kind              text NOT NULL DEFAULT 'relationship' CHECK (kind = 'relationship'),
    subject_id        uuid NOT NULL REFERENCES nodes,
    predicate         text NOT NULL CHECK (predicate ~ '^[a-z][a-z0-9_]*$'),
    object_id         uuid NOT NULL REFERENCES nodes,
    valid_from        timestamptz,
    valid_until       timestamptz,
    rationale         text,
    retracted_at      timestamptz,
    retracted_by      uuid REFERENCES actors,
    retraction_reason text,
    CHECK (subject_id <> object_id),
    CHECK (id NOT IN (subject_id, object_id)),
    CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from),
    CHECK ((retracted_at IS NULL) = (retracted_by IS NULL)),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

CREATE UNIQUE INDEX relationships_active_uniq
    ON relationships (subject_id, predicate, object_id) WHERE retracted_at IS NULL;
CREATE INDEX relationships_object_idx ON relationships (object_id, predicate);

-- Why a research area exists and how much attention it gets.
CREATE TABLE programs (
    id       uuid PRIMARY KEY,
    kind     text NOT NULL DEFAULT 'program' CHECK (kind = 'program'),
    name     text NOT NULL,
    charter  text NOT NULL,
    priority smallint NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    status   text NOT NULL DEFAULT 'proposed'
             CHECK (status IN ('proposed', 'active', 'paused', 'closed')),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

CREATE TABLE goals (
    id               uuid PRIMARY KEY,
    kind             text NOT NULL DEFAULT 'goal' CHECK (kind = 'goal'),
    program_id       uuid REFERENCES programs,
    statement        text NOT NULL,
    success_criteria text,
    priority         smallint NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    status           text NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'active', 'achieved', 'abandoned')),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

CREATE INDEX goals_program_idx ON goals (program_id);

-- A choice such as "reduce focus on AI models". Any actor may propose one;
-- only the board (the owner's interface) may resolve it.
CREATE TABLE decisions (
    id            uuid PRIMARY KEY,
    kind          text NOT NULL DEFAULT 'decision' CHECK (kind = 'decision'),
    statement     text NOT NULL,
    rationale     text NOT NULL,
    status        text NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed', 'approved', 'rejected', 'superseded')),
    resolved_by   uuid REFERENCES actors,
    resolved_at   timestamptz,
    superseded_by uuid REFERENCES decisions,
    CHECK ((status = 'proposed') = (resolved_at IS NULL)),
    CHECK ((resolved_at IS NULL) = (resolved_by IS NULL)),
    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

-- ---------------------------------------------------------------------------
-- Evidence and belief
-- ---------------------------------------------------------------------------

-- Evidence for or against a claim. target_kind is carried so the composite
-- foreign key can restrict which kinds of record evidence may bear on.
CREATE TABLE evidence_links (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_id       uuid NOT NULL REFERENCES evidence,
    target_id         uuid NOT NULL,
    target_kind       text NOT NULL CHECK (target_kind IN (
                          'hypothesis', 'relationship', 'observation', 'critique')),
    stance            text NOT NULL CHECK (stance IN ('supports', 'contradicts', 'context')),
    weight            numeric(3,2) CHECK (weight BETWEEN 0 AND 1),
    rationale         text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        uuid NOT NULL DEFAULT current_actor() REFERENCES actors,
    created_in_run    uuid DEFAULT current_run() REFERENCES runs,
    retracted_at      timestamptz,
    retracted_by      uuid REFERENCES actors,
    retraction_reason text,
    CHECK ((retracted_at IS NULL) = (retracted_by IS NULL)),
    FOREIGN KEY (target_id, target_kind) REFERENCES nodes (id, kind)
);

CREATE UNIQUE INDEX evidence_links_active_uniq
    ON evidence_links (evidence_id, target_id) WHERE retracted_at IS NULL;
CREATE INDEX evidence_links_target_idx ON evidence_links (target_id);

-- Append-only. Each row is one actor's judgement of a claim at a point in
-- time; the Skeptic's confidence adjustments land here.
CREATE TABLE confidence_assessments (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    target_id       uuid NOT NULL,
    target_kind     text NOT NULL CHECK (target_kind IN (
                        'hypothesis', 'relationship', 'observation')),
    confidence      numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    rationale       text NOT NULL,
    assessed_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    assessed_by     uuid NOT NULL DEFAULT current_actor() REFERENCES actors,
    assessed_in_run uuid DEFAULT current_run() REFERENCES runs,
    FOREIGN KEY (target_id, target_kind) REFERENCES nodes (id, kind)
);

CREATE INDEX confidence_assessments_target_idx
    ON confidence_assessments (target_id, assessed_at DESC);

-- ---------------------------------------------------------------------------
-- Audit
-- ---------------------------------------------------------------------------

CREATE TABLE audit_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor_id   uuid NOT NULL REFERENCES actors,
    run_id     uuid REFERENCES runs,
    db_user    text NOT NULL,           -- login role that made the change
    table_name text NOT NULL,
    row_id     uuid,                    -- null for tables without an id column
    action     text NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    old_row    jsonb,
    new_row    jsonb
);

CREATE INDEX audit_log_row_idx ON audit_log (table_name, row_id, at);
CREATE INDEX audit_log_run_idx ON audit_log (run_id);

-- SECURITY DEFINER so clients need no privilege on audit_log and cannot
-- forge entries. It also checks the declared actor against the login role:
-- only board members may act as the owner, and only the schema owner may
-- act as the system.
CREATE FUNCTION audit_row() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    actor_kind text;
    old_j      jsonb;
    new_j      jsonb;
BEGIN
    SELECT kind INTO actor_kind FROM actors WHERE id = current_actor();
    IF actor_kind IS NULL THEN
        RAISE EXCEPTION 'write to % without a valid collegium.actor_id', TG_TABLE_NAME;
    END IF;
    IF actor_kind = 'owner' AND NOT pg_has_role(session_user, 'collegium_board', 'MEMBER') THEN
        RAISE EXCEPTION '% may not act as the owner', session_user;
    END IF;
    -- Inside SECURITY DEFINER, current_user is the schema owner.
    IF actor_kind = 'system' AND NOT pg_has_role(session_user, current_user, 'MEMBER') THEN
        RAISE EXCEPTION '% may not act as the system', session_user;
    END IF;

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

-- Provenance columns (created_by, retracted_by, ...) must name the actor
-- declared for the session. They default to it, so callers normally omit
-- them; an explicit different value is rejected. TG_ARGV lists the columns.
CREATE FUNCTION enforce_provenance() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    col   text;
    new_v text;
BEGIN
    FOREACH col IN ARRAY TG_ARGV
    LOOP
        new_v := to_jsonb(NEW) ->> col;
        IF new_v IS NOT NULL
           AND (TG_OP = 'INSERT' OR new_v IS DISTINCT FROM to_jsonb(OLD) ->> col)
           AND new_v::uuid IS DISTINCT FROM current_actor() THEN
            RAISE EXCEPTION '%.% must be the session actor', TG_TABLE_NAME, col;
        END IF;
    END LOOP;
    RETURN NEW;
END
$$;

CREATE TRIGGER runs_provenance BEFORE INSERT OR UPDATE ON runs
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('actor_id');
CREATE TRIGGER domains_provenance BEFORE INSERT OR UPDATE ON domains
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('created_by');
CREATE TRIGGER sources_provenance BEFORE INSERT OR UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('created_by');
CREATE TRIGGER nodes_provenance BEFORE INSERT OR UPDATE ON nodes
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('created_by');
CREATE TRIGGER relationships_provenance BEFORE INSERT OR UPDATE ON relationships
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('retracted_by');
CREATE TRIGGER decisions_provenance BEFORE INSERT OR UPDATE ON decisions
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('resolved_by');
CREATE TRIGGER evidence_links_provenance BEFORE INSERT OR UPDATE ON evidence_links
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('created_by', 'retracted_by');
CREATE TRIGGER confidence_assessments_provenance BEFORE INSERT ON confidence_assessments
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('assessed_by');

CREATE FUNCTION reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on % is not allowed: %', TG_OP, TG_TABLE_NAME, TG_ARGV[0];
END
$$;

DO $$
DECLARE
    t text;
BEGIN
    -- Audit every table except the audit log itself.
    FOREACH t IN ARRAY ARRAY[
        'actors', 'runs', 'domains', 'sources', 'nodes', 'node_domains',
        'entities', 'observations', 'hypotheses', 'evidence', 'critiques',
        'relationships', 'programs', 'goals', 'decisions', 'evidence_links',
        'confidence_assessments']
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE ON %I
             FOR EACH ROW EXECUTE FUNCTION audit_row()',
            t || '_audit', t);
    END LOOP;

    -- Knowledge is retired or retracted, never deleted.
    FOREACH t IN ARRAY ARRAY[
        'actors', 'runs', 'domains', 'sources', 'nodes', 'entities',
        'observations', 'hypotheses', 'evidence', 'critiques',
        'relationships', 'programs', 'goals', 'decisions', 'evidence_links']
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE DELETE ON %I
             FOR EACH ROW EXECUTE FUNCTION reject_change(%L)',
            t || '_no_delete', t, 'retire or retract instead');
    END LOOP;

    -- Append-only history.
    FOREACH t IN ARRAY ARRAY['confidence_assessments', 'audit_log']
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I
             FOR EACH ROW EXECUTE FUNCTION reject_change(%L)',
            t || '_append_only', t, 'table is append-only');
    END LOOP;

    -- TRUNCATE bypasses row triggers, so block it everywhere.
    FOREACH t IN ARRAY ARRAY[
        'actors', 'runs', 'domains', 'sources', 'nodes', 'node_domains',
        'entities', 'observations', 'hypotheses', 'evidence', 'critiques',
        'relationships', 'programs', 'goals', 'decisions', 'evidence_links',
        'confidence_assessments', 'audit_log']
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE TRUNCATE ON %I
             FOR EACH STATEMENT EXECUTE FUNCTION reject_change(%L)',
            t || '_no_truncate', t, 'memory is permanent');
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- Views answering "why do we believe this?"
-- ---------------------------------------------------------------------------

CREATE VIEW current_confidence AS
SELECT DISTINCT ON (target_id)
       target_id, target_kind, confidence, rationale, assessed_at, assessed_by
FROM confidence_assessments
ORDER BY target_id, assessed_at DESC, id;

-- Every status change of every hypothesis, from the audit log. The first
-- row with status 'accepted' answers "when did we start believing it?".
CREATE VIEW hypothesis_status_history AS
SELECT row_id                  AS hypothesis_id,
       at,
       actor_id,
       run_id,
       old_row ->> 'status'    AS from_status,
       new_row ->> 'status'    AS to_status
FROM audit_log
WHERE table_name = 'hypotheses'
  AND (action = 'INSERT'
       OR old_row ->> 'status' IS DISTINCT FROM new_row ->> 'status');

CREATE VIEW hypothesis_overview AS
SELECT h.id,
       h.statement,
       h.status,
       n.created_at,
       n.created_by        AS proposed_by,
       c.confidence        AS current_confidence,
       c.assessed_at       AS confidence_assessed_at,
       (SELECT min(s.at) FROM hypothesis_status_history s
         WHERE s.hypothesis_id = h.id AND s.to_status = 'accepted')
                           AS first_accepted_at,
       (SELECT count(*) FROM evidence_links l
         WHERE l.target_id = h.id AND l.retracted_at IS NULL AND l.stance = 'supports')
                           AS supporting_evidence,
       (SELECT count(*) FROM evidence_links l
         WHERE l.target_id = h.id AND l.retracted_at IS NULL AND l.stance = 'contradicts')
                           AS contradicting_evidence,
       (SELECT count(*) FROM critiques k
         WHERE k.target_id = h.id AND k.status = 'open')
                           AS open_critiques
FROM hypotheses h
JOIN nodes n ON n.id = h.id
LEFT JOIN current_confidence c ON c.target_id = h.id;

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------
--
-- reader: read everything.
-- worker: agent roles. Insert knowledge; update only lifecycle columns
--         (status, supersession, retraction). Statements are never updated.
--         Cannot create domains or resolve decisions.
-- board:  the owner's interface. Everything the worker can do, plus manage
--         domains and resolve decisions. Only board members may act as the
--         owner actor (checked in audit_row).
-- Nobody gets DELETE except on node_domains, or any privilege on audit_log
-- beyond SELECT. New tables in later migrations must be granted explicitly.

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO collegium_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO collegium_reader;

GRANT INSERT ON runs, sources, nodes, node_domains, entities, observations,
                hypotheses, evidence, critiques, relationships, programs,
                goals, decisions, evidence_links, confidence_assessments
    TO collegium_worker;
GRANT DELETE ON node_domains TO collegium_worker;

GRANT UPDATE (status, finished_at, notes)                        ON runs          TO collegium_worker;
GRANT UPDATE (entity_type, name, aliases, description, attributes,
              status, merged_into)                               ON entities      TO collegium_worker;
GRANT UPDATE (status, recommendation, occurred_at)               ON observations  TO collegium_worker;
GRANT UPDATE (status, superseded_by, rationale)                  ON hypotheses    TO collegium_worker;
GRANT UPDATE (reliability)                                       ON evidence      TO collegium_worker;
GRANT UPDATE (status, resolution, severity)                      ON critiques     TO collegium_worker;
GRANT UPDATE (valid_until, retracted_at, retracted_by,
              retraction_reason)                                 ON relationships TO collegium_worker;
GRANT UPDATE (name, charter, priority, status)                   ON programs      TO collegium_worker;
GRANT UPDATE (program_id, success_criteria, priority, status)    ON goals         TO collegium_worker;
GRANT UPDATE (retracted_at, retracted_by, retraction_reason)     ON evidence_links TO collegium_worker;

GRANT INSERT, UPDATE (name, description, status)                 ON domains       TO collegium_board;
GRANT UPDATE (status, resolved_by, resolved_at, superseded_by)   ON decisions     TO collegium_board;

-- ---------------------------------------------------------------------------
-- Founding actors
-- ---------------------------------------------------------------------------

-- The system actor has a fixed id so migrations and maintenance jobs can
-- identify themselves.
SELECT set_config('collegium.actor_id', '00000000-0000-0000-0000-000000000001', true);

INSERT INTO actors (id, kind, name, description) VALUES
    ('00000000-0000-0000-0000-000000000001', 'system', 'system',
     'Migrations and maintenance.');

INSERT INTO actors (kind, name, description) VALUES
    ('owner', 'owner',      'Board chair and sponsor. Sets direction and approves strategic decisions.'),
    ('role',  'historian',  'Institutional memory. Stores, never researches.'),
    ('role',  'scout',      'Breadth-first exploration. Proposes observations.'),
    ('role',  'researcher', 'Investigates observations. Forms hypotheses and gathers evidence.'),
    ('role',  'skeptic',    'Challenges findings. Adjusts confidence.'),
    ('role',  'strategist', 'Finds knowledge gaps. Creates programs and goals.');

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
