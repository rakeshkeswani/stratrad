-- database/setup_roles.sql -- ST-012, run ONCE on prod by Rakesh as the postgres superuser:
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -f database/setup_roles.sql
-- Then set the three passwords interactively (never in a file):
--   sudo -u postgres psql -c "\password stratrad_app"      (and stratrad_reader, stratrad_ro)
-- and put them in prod .env (STRATRAD_DB_URL, STRATRAD_READER_URL, MYINVESTIQ_RO_URL; docs/CONFIG.md).
--
-- Roles: stratrad_app = owner of database stratrad (the live system, runs migrations);
--        stratrad_reader = read-only on stratrad (SELECT granted by migration 001 and default privileges);
--        stratrad_ro = read-only on MyInvestIQ's portfolio_watch: corporate_actions, fii_daily_flow and stock_master
--                      ONLY (stock_master added 2026-10-29 for ST-008's sector cap; interim until ST-005).
-- Every session of the three roles uses UTC (server default is IST).

CREATE ROLE stratrad_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE stratrad_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE stratrad_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE stratrad_app SET timezone = 'UTC';
ALTER ROLE stratrad_reader SET timezone = 'UTC';
ALTER ROLE stratrad_ro SET timezone = 'UTC';
ALTER ROLE stratrad_reader SET default_transaction_read_only = on;
ALTER ROLE stratrad_ro SET default_transaction_read_only = on;

CREATE DATABASE stratrad OWNER stratrad_app ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE stratrad FROM PUBLIC;
GRANT CONNECT ON DATABASE stratrad TO stratrad_app, stratrad_reader;

-- MyInvestIQ: only stratrad_ro may read, and only these three tables.
\connect portfolio_watch
GRANT CONNECT ON DATABASE portfolio_watch TO stratrad_ro;
GRANT USAGE ON SCHEMA public TO stratrad_ro;
GRANT SELECT ON public.corporate_actions, public.fii_daily_flow TO stratrad_ro;
-- ST-008 (2026-10-29): first granted on prod by hand as portfolio_rk; recorded here so a rebuild keeps it.
GRANT SELECT ON public.stock_master TO stratrad_ro;
