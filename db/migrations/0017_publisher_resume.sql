-- migrate:up

-- The publisher's halt (MAX_FAILED_VERIFICATIONS failed verifications in a
-- row) stops everything until the owner looks, but there was no code path
-- back from that state: publish_next() refuses to attempt anything while
-- halted, and only a new successful attempt would clear it -- which can
-- never happen while halted (docs/decisions.md, 2026-10-08, left as an
-- open thread; confirmed live on 2026-10-23, halted since 2026-10-07 with
-- a growing backlog of approved posts nobody noticed).
--
-- publisher_resumes lets the owner grant exactly one attempt through
-- regardless of the halt, after looking at why it tripped. A grant is
-- consumed at most once, and only counts against a halt that took hold
-- after it was granted: a resume left over from a halt the owner already
-- looked at must not silently wave through a later, unrelated one.

CREATE TABLE publisher_resumes (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid NOT NULL DEFAULT current_actor() REFERENCES actors,
    used_at    timestamptz
);

CREATE FUNCTION publisher_resume_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.used_at IS NOT NULL THEN
        RAISE EXCEPTION 'a publisher resume may be consumed only once';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER publisher_resumes_rules BEFORE UPDATE ON publisher_resumes
    FOR EACH ROW EXECUTE FUNCTION publisher_resume_rules();
CREATE TRIGGER publisher_resumes_audit AFTER INSERT OR UPDATE OR DELETE ON publisher_resumes
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER publisher_resumes_provenance BEFORE INSERT OR UPDATE ON publisher_resumes
    FOR EACH ROW EXECUTE FUNCTION enforce_provenance('created_by');
CREATE TRIGGER publisher_resumes_no_delete BEFORE DELETE ON publisher_resumes
    FOR EACH ROW EXECUTE FUNCTION reject_change('one-time by nature; leave used rows as the record');
CREATE TRIGGER publisher_resumes_no_truncate BEFORE TRUNCATE ON publisher_resumes
    FOR EACH STATEMENT EXECUTE FUNCTION reject_change('memory is permanent');

GRANT SELECT ON publisher_resumes TO collegium_reader;
GRANT INSERT ON publisher_resumes TO collegium_board;
GRANT SELECT, UPDATE (used_at) ON publisher_resumes TO collegium_publisher;

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
