-- migrate:up

-- The community agent may upvote on Moltbook (the owner's decision,
-- 2026-09-28): a vote is an outbox row of kind 'vote', for a decision with
-- topic 'vote' that the owner approved, like posts and replies. It targets
-- a post (reply_to_post) or one of its comments (reply_to_comment) and has
-- no text. Approving a reply also upvotes the comment it answers.
--
-- The same thing is never voted for twice: one live vote per target.

ALTER TABLE community_posts DROP CONSTRAINT community_posts_kind_check;
ALTER TABLE community_posts ADD CONSTRAINT community_posts_kind_check
    CHECK (kind IN ('post', 'comment', 'vote'));

ALTER TABLE community_posts ALTER COLUMN content DROP NOT NULL;
ALTER TABLE community_posts ADD CONSTRAINT community_posts_content_by_kind
    CHECK ((kind = 'vote') = (content IS NULL));

ALTER TABLE community_posts DROP CONSTRAINT community_posts_check1;
ALTER TABLE community_posts ADD CONSTRAINT community_posts_shape CHECK (
    CASE kind
    WHEN 'post' THEN submolt IS NOT NULL AND title IS NOT NULL AND reply_to_post IS NULL
    ELSE reply_to_post IS NOT NULL AND title IS NULL AND submolt IS NULL END);

CREATE UNIQUE INDEX community_posts_one_vote ON community_posts
    (reply_to_post, coalesce(reply_to_comment, ''))
    WHERE kind = 'vote' AND status NOT IN ('failed', 'withdrawn');

CREATE OR REPLACE FUNCTION community_post_rules() RETURNS trigger
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
                       AND topic = CASE NEW.kind WHEN 'post' THEN 'post'
                                                 WHEN 'vote' THEN 'vote' ELSE 'reply' END
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

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
