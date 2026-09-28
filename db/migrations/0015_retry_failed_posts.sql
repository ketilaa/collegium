-- migrate:up

-- The owner may send a failed post, reply or vote back for another
-- publishing attempt (the owner's decision, 2026-09-28: a verification
-- failure is often a one-off, not a reason to abandon approved content).
-- Moltbook's own guidance for a failed challenge is to create new content
-- and try again, so a retry is a fresh POST/upvote call by the publisher,
-- not a re-verification of the old one; the row's external_id, url and
-- verification stay as the failed attempt's until the publisher overwrites
-- them with the new one. The text itself cannot change (community_post_
-- rules already forbids that); only status and error are the owner's to
-- write, as before.

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
            ('approved', 'withdrawn'), ('publishing', 'failed'), ('failed', 'approved')))
    ) THEN
        RAISE EXCEPTION '% may not move a post from % to %', actor, OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END
$$;

-- migrate:down

-- Intentionally empty. Institutional memory is never rolled back; fix
-- mistakes with a new forward migration.
