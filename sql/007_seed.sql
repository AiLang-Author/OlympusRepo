-- sql/007_seed.sql
-- Intentionally empty in the RenkuOS fork.
--
-- Upstream created a 'zeus' account with password 'changeme' here. setup.sh
-- runs every sql/0*.sql file, so every install got that account in addition
-- to the real admin setup.sh creates interactively. 021_security.sql disables
-- any account still using that password.
--
-- Create the first admin with ./setup.sh (or `olympusrepo user create`).
SELECT 1;
