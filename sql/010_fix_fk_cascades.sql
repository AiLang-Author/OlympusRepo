-- sql/010_fix_fk_cascades.sql
-- Fix FK constraints that block repo deletion
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT

BEGIN;

-- repo_promotions: drop and recreate FKs with CASCADE
ALTER TABLE repo_promotions
    DROP CONSTRAINT IF EXISTS repo_promotions_staging_id_fkey,
    DROP CONSTRAINT IF EXISTS repo_promotions_repo_id_fkey;

ALTER TABLE repo_promotions
    ADD CONSTRAINT repo_promotions_staging_id_fkey
        FOREIGN KEY (staging_id) REFERENCES repo_staging(staging_id)
        ON DELETE CASCADE,
    ADD CONSTRAINT repo_promotions_repo_id_fkey
        FOREIGN KEY (repo_id) REFERENCES repo_repositories(repo_id)
        ON DELETE CASCADE;

-- repo_refs: drop and recreate FK with CASCADE
ALTER TABLE repo_refs
    DROP CONSTRAINT IF EXISTS repo_refs_commit_hash_fkey;

ALTER TABLE repo_refs
    ADD CONSTRAINT repo_refs_commit_hash_fkey
        FOREIGN KEY (commit_hash) REFERENCES repo_commits(commit_hash)
        ON DELETE CASCADE;

-- repo_commits: ensure repo_id cascades
ALTER TABLE repo_commits
    DROP CONSTRAINT IF EXISTS repo_commits_repo_id_fkey;

ALTER TABLE repo_commits
    ADD CONSTRAINT repo_commits_repo_id_fkey
        FOREIGN KEY (repo_id) REFERENCES repo_repositories(repo_id)
        ON DELETE CASCADE;

-- repo_staging: ensure repo_id cascades
ALTER TABLE repo_staging
    DROP CONSTRAINT IF EXISTS repo_staging_repo_id_fkey;

ALTER TABLE repo_staging
    ADD CONSTRAINT repo_staging_repo_id_fkey
        FOREIGN KEY (repo_id) REFERENCES repo_repositories(repo_id)
        ON DELETE CASCADE;

-- repo_staging_changes: ensure staging_id cascades
ALTER TABLE repo_staging_changes
    DROP CONSTRAINT IF EXISTS repo_staging_changes_staging_id_fkey;

ALTER TABLE repo_staging_changes
    ADD CONSTRAINT repo_staging_changes_staging_id_fkey
        FOREIGN KEY (staging_id) REFERENCES repo_staging(staging_id)
        ON DELETE CASCADE;

-- repo_changesets: ensure commit_hash cascades
ALTER TABLE repo_changesets
    DROP CONSTRAINT IF EXISTS repo_changesets_commit_hash_fkey;

ALTER TABLE repo_changesets
    ADD CONSTRAINT repo_changesets_commit_hash_fkey
        FOREIGN KEY (commit_hash) REFERENCES repo_commits(commit_hash)
        ON DELETE CASCADE;

-- repo_audit_log: set null on repo delete (audit log should survive)
ALTER TABLE repo_audit_log
    DROP CONSTRAINT IF EXISTS repo_audit_log_repo_id_fkey;

ALTER TABLE repo_audit_log
    ADD CONSTRAINT repo_audit_log_repo_id_fkey
        FOREIGN KEY (repo_id) REFERENCES repo_repositories(repo_id)
        ON DELETE SET NULL;

COMMIT;
-- Allow NULL rev on slave commits (rev assigned by canonical at promotion)
ALTER TABLE repo_commits ALTER COLUMN rev DROP NOT NULL;
ALTER TABLE repo_commits ALTER COLUMN rev DROP DEFAULT;

-- Allow NULL global_rev in file revisions (populated at promotion)
ALTER TABLE repo_file_revisions ALTER COLUMN global_rev DROP NOT NULL;

-- Allow NULL from_rev and base_rev on offers from slave instances
ALTER TABLE repo_offers ALTER COLUMN from_rev DROP NOT NULL;
ALTER TABLE repo_offers ALTER COLUMN base_rev DROP NOT NULL;
