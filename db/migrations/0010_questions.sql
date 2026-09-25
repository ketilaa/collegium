-- migrate:up

-- Collegium, Milestone 4: Ask the Organization.
--
-- * A question from the owner is a node, so it has provenance like any
--   record and goals can investigate it. The Researcher answers it from
--   memory only ('ask' jobs); the answer's citations are 'cites'
--   relationships from the question to the records it rests on.
-- * status: pending until answered; answered when memory held an answer;
--   unanswered when it did not. `missing` says what memory lacks. Recent
--   unanswered questions are knowledge gaps for the Strategist.

ALTER TABLE nodes DROP CONSTRAINT nodes_kind_check;
ALTER TABLE nodes ADD CONSTRAINT nodes_kind_check CHECK (kind IN (
    'entity', 'observation', 'hypothesis', 'evidence', 'critique',
    'relationship', 'program', 'goal', 'decision', 'question'));

CREATE TABLE questions (
    id          uuid PRIMARY KEY,
    kind        text NOT NULL DEFAULT 'question' CHECK (kind = 'question'),
    text        text NOT NULL CHECK (length(trim(text)) > 0),
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'answered', 'unanswered')),
    answer      text,
    missing     text,          -- what memory lacks to answer fully
    answered_at timestamptz,
    CHECK ((status = 'pending') = (answered_at IS NULL)),
    CHECK (status = 'pending' OR answer IS NOT NULL),
    FOREIGN KEY (id, kind) REFERENCES nodes (id, kind)
);

CREATE INDEX questions_status_idx ON questions (status);

CREATE TRIGGER questions_audit AFTER INSERT OR UPDATE OR DELETE ON questions
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER questions_no_delete BEFORE DELETE ON questions
    FOR EACH ROW EXECUTE FUNCTION reject_change('retire or retract instead');
CREATE TRIGGER questions_no_truncate BEFORE TRUNCATE ON questions
    FOR EACH STATEMENT EXECUTE FUNCTION reject_change('memory is permanent');

GRANT SELECT ON questions TO collegium_reader;
GRANT UPDATE (status, answer, missing, answered_at) ON questions TO collegium_worker;
-- Only the board asks.
GRANT INSERT ON questions TO collegium_board;

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('scout', 'research', 'review', 'record', 'map', 'resolve',
                    'strategize', 'corroborate', 'ask'));

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
