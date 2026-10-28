-- The Grafana read-only login. Separate from db/logins.sql on purpose:
-- this is only needed when running the optional "grafana" Compose
-- profile, so a plain `docker compose up` never requires this password.
-- Idempotent; run after migrations:
--
--   psql -v ON_ERROR_STOP=1 -v grafana_password=... -f db/grafana-login.sql

SELECT format('CREATE ROLE %I LOGIN', 'collegium_grafana_app')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'collegium_grafana_app')
\gexec

ALTER ROLE collegium_grafana_app PASSWORD :'grafana_password';

-- Read-only, the same as the CLI's own read queries: every dashboard
-- panel is a SELECT, nothing a dashboard does should ever need to write.
GRANT collegium_reader TO collegium_grafana_app;
