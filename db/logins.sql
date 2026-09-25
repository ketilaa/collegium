-- Login users for the group roles. Idempotent; run after migrations:
--
--   psql -v ON_ERROR_STOP=1 -v worker_password=... -v board_password=... \
--        -v publisher_password=... -f db/logins.sql
--
-- Passwords are deployment secrets, which is why this is not a migration.

SELECT format('CREATE ROLE %I LOGIN', r)
FROM unnest(ARRAY['collegium_worker_app', 'collegium_board_app', 'collegium_publisher_app']) AS r
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r)
\gexec

ALTER ROLE collegium_worker_app PASSWORD :'worker_password';
ALTER ROLE collegium_board_app  PASSWORD :'board_password';
ALTER ROLE collegium_publisher_app PASSWORD :'publisher_password';

GRANT collegium_worker TO collegium_worker_app;
GRANT collegium_board  TO collegium_board_app;
-- The Moltbook publisher: the outbox only (migration 0013).
GRANT collegium_publisher TO collegium_publisher_app;
