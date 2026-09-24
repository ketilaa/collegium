-- migrate:up

-- Collegium, Milestone 4: the board.
--
-- * decisions.topic names what kind of decision a record is. 'mission' is
--   the owner's statement of what the organization, or one domain (tagged
--   through node_domains), is for. A changed mission is a new approved
--   decision that supersedes the old one, so every earlier mission is kept.
--   Domains are not nodes, so a mission cannot be linked to its domain
--   with a relationship.
-- * Only the owner may decide. Agents could already not update decisions,
--   but they could insert one that was already approved; now a decision
--   that is not 'proposed' may only be written by the owner.

ALTER TABLE decisions ADD COLUMN topic text CHECK (topic ~ '^[a-z][a-z0-9_]*$');

CREATE INDEX decisions_topic_idx ON decisions (topic) WHERE status = 'approved';

CREATE FUNCTION only_owner_decides() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.status <> 'proposed'
       AND (TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status)
       AND (SELECT kind FROM actors WHERE id = current_actor()) IS DISTINCT FROM 'owner' THEN
        RAISE EXCEPTION 'only the owner may decide (decision %)', NEW.id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER decisions_only_owner_decides BEFORE INSERT OR UPDATE ON decisions
    FOR EACH ROW EXECUTE FUNCTION only_owner_decides();

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
