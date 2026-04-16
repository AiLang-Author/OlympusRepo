-- sql/007_seed.sql
-- Optional seed data: default zeus account
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT
--
-- Creates a default 'zeus' account with password 'changeme'.
-- CHANGE THIS PASSWORD IMMEDIATELY AFTER FIRST LOGIN.
--
-- To skip seeding, do not run this file.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM repo_users WHERE username = 'zeus') THEN
        PERFORM repo_create_user('zeus', 'changeme', 'zeus', 'Zeus (default admin)', NULL);
        RAISE NOTICE 'Default zeus account created. CHANGE THE PASSWORD IMMEDIATELY.';
    ELSE
        RAISE NOTICE 'User zeus already exists, skipping seed.';
    END IF;
END $$;
