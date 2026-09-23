-- Login users for the group roles. Idempotent; run after migrations:
--
--   psql -v ON_ERROR_STOP=1 -v worker_password=... -v board_password=... -f db/logins.sql
--
-- Passwords are deployment secrets, which is why this is not a migration.

SELECT format('CREATE ROLE %I LOGIN', r)
FROM unnest(ARRAY['collegium_worker_app', 'collegium_board_app']) AS r
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r)
\gexec

ALTER ROLE collegium_worker_app PASSWORD :'worker_password';
ALTER ROLE collegium_board_app  PASSWORD :'board_password';

GRANT collegium_worker TO collegium_worker_app;
GRANT collegium_board  TO collegium_board_app;
