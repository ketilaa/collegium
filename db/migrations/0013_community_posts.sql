-- migrate:up

-- The community agent on Moltbook, stage 2: posts and replies drafted from
-- memory, approved one by one by the owner, published by a separate
-- component.
--
-- * A draft is a proposed decision: topic 'post' (a question to other
--   agents about a hypothesis) or 'reply' (a comment answering another
--   agent's post or comment from what memory holds). Its details hold the
--   text and what it concerns. Agents can only propose it.
-- * community_posts is the outbox, for posts and comments alike. Only the
--   owner puts anything in it, and only for a decision the owner has
--   approved. The text in the outbox is final: it cannot be changed.
-- * The publisher is its own actor and database role. It can read the
--   outbox and record what happened to a post, and nothing else: no
--   memory, no jobs. It alone holds the Moltbook key (outside the database).
-- * Status: approved -> publishing -> published | failed. The publisher
--   marks a post publishing before it sends it, so a crash midway leaves
--   it publishing, not approved: it is never sent twice unseen. It may put
--   a post back to approved only when nothing was created (rate limited).
--   The owner may withdraw an approved post, or mark a stuck one failed.

SELECT set_config('collegium.actor_id', '00000000-0000-0000-0000-000000000001', true);

INSERT INTO actors (kind, name, description) VALUES
    ('service', 'publisher',
     'Publishes posts the owner approved on Moltbook. The only holder of its key.');

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'collegium_publisher') THEN
        CREATE ROLE collegium_publisher NOLOGIN;
    END IF;
END
$$;

CREATE TABLE community_posts (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id  uuid NOT NULL UNIQUE REFERENCES decisions,
    domain_id    uuid REFERENCES domains,
    about_id     uuid REFERENCES nodes,          -- the hypothesis or observation concerned
    forum        text NOT NULL DEFAULT 'moltbook' CHECK (forum = 'moltbook'),
    kind         text NOT NULL DEFAULT 'post' CHECK (kind IN ('post', 'comment')),
    -- A post goes to a forum with a title; a comment to another post, or
    -- under one of its comments.
    submolt      text CHECK (submolt ~ '^[a-z0-9][a-z0-9-]*$'),
    title        text CHECK (length(trim(title)) BETWEEN 1 AND 300),
    reply_to_post    text CHECK (reply_to_post ~ '^[A-Za-z0-9-]+$'),
    reply_to_comment text CHECK (reply_to_comment ~ '^[A-Za-z0-9-]+$'),
    content      text NOT NULL CHECK (length(trim(content)) BETWEEN 1 AND 40000),
    status       text NOT NULL DEFAULT 'approved'
                 CHECK (status IN ('approved', 'publishing', 'published', 'failed', 'withdrawn')),
    external_id  text,                           -- the forum's id for the post
    url          text CHECK (url LIKE 'https://www.moltbook.com/%'),
    verification text CHECK (verification IN ('passed', 'failed')),
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    attempted_at timestamptz,
    published_at timestamptz,
    CHECK ((status = 'published') = (published_at IS NOT NULL)),
    CHECK (CASE kind
           WHEN 'post' THEN submolt IS NOT NULL AND title IS NOT NULL AND reply_to_post IS NULL
           ELSE reply_to_post IS NOT NULL AND title IS NULL AND submolt IS NULL END)
);

CREATE INDEX community_posts_status_idx ON community_posts (status, created_at);
CREATE INDEX community_posts_about_idx ON community_posts (about_id);

CREATE FUNCTION community_post_rules() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    actor text;
BEGIN
    SELECT name INTO actor FROM actors WHERE id = current_actor();
    IF TG_OP = 'INSERT' THEN
        IF actor IS DISTINCT FROM 'owner' THEN
            RAISE EXCEPTION 'only the owner may approve a post';
        END IF;
        IF NOT EXISTS (SELECT FROM decisions WHERE id = NEW.decision_id
                       AND topic = CASE NEW.kind WHEN 'post' THEN 'post' ELSE 'reply' END
                       AND status = 'approved') THEN
            RAISE EXCEPTION 'a % needs an approved decision', NEW.kind;
        END IF;
        IF NEW.status <> 'approved' THEN
            RAISE EXCEPTION 'a new post starts approved';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.decision_id, NEW.domain_id, NEW.about_id, NEW.forum, NEW.kind, NEW.submolt,
        NEW.title, NEW.reply_to_post, NEW.reply_to_comment, NEW.content, NEW.created_at)
       IS DISTINCT FROM (OLD.decision_id, OLD.domain_id, OLD.about_id, OLD.forum, OLD.kind,
                         OLD.submolt, OLD.title, OLD.reply_to_post, OLD.reply_to_comment,
                         OLD.content, OLD.created_at) THEN
        RAISE EXCEPTION 'an approved post cannot be changed';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (actor = 'publisher' AND (OLD.status, NEW.status) IN (
            ('approved', 'publishing'), ('publishing', 'approved'),
            ('publishing', 'published'), ('publishing', 'failed')))
        OR (actor = 'owner' AND (OLD.status, NEW.status) IN (
            ('approved', 'withdrawn'), ('publishing', 'failed')))
    ) THEN
        RAISE EXCEPTION '% may not move a post from % to %', actor, OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER community_posts_rules BEFORE INSERT OR UPDATE ON community_posts
    FOR EACH ROW EXECUTE FUNCTION community_post_rules();
CREATE TRIGGER community_posts_audit AFTER INSERT OR UPDATE OR DELETE ON community_posts
    FOR EACH ROW EXECUTE FUNCTION audit_row();
CREATE TRIGGER community_posts_no_delete BEFORE DELETE ON community_posts
    FOR EACH ROW EXECUTE FUNCTION reject_change('withdraw instead');
CREATE TRIGGER community_posts_no_truncate BEFORE TRUNCATE ON community_posts
    FOR EACH STATEMENT EXECUTE FUNCTION reject_change('memory is permanent');

GRANT SELECT ON community_posts TO collegium_reader;
GRANT INSERT, UPDATE (status, error) ON community_posts TO collegium_board;

GRANT USAGE ON SCHEMA public TO collegium_publisher;
GRANT SELECT ON actors, community_posts TO collegium_publisher;
GRANT UPDATE (status, external_id, url, verification, error, attempted_at, published_at)
    ON community_posts TO collegium_publisher;

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('scout', 'research', 'review', 'record', 'map', 'resolve',
                    'strategize', 'corroborate', 'ask', 'draft', 'reply'));

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
