-- sql/019_repair_v2_upload.sql
-- Repair migration for the v2.0 web-upload bug.
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT
--
-- BUG SUMMARY
-- -----------
-- commit_files() (the web-upload path in core/repo.py) inserted into
-- repo_objects with byte_offset = 0 for loose objects. Migration 017
-- added the byte_offset_matches_pack CHECK that requires byte_offset
-- IS NULL when pack_id IS NULL, so on a v2 schema the INSERT fails.
-- The surrounding try/except: pass swallowed the exception, leaving
-- the PG transaction aborted; every subsequent statement on the same
-- connection raised InFailedSqlTransaction (the user-visible symptom
-- was the UPDATE repo_refs failing with that message).
--
-- The code path is fixed in core/repo.py (commit_files now inserts
-- NULL and lets exceptions propagate). This migration is here for
-- two reasons:
--   1. Backfill any pre-017 rows that snuck in with byte_offset = 0
--      and pack_id = NULL — they would block the constraint from
--      re-applying cleanly on a manually-tinkered schema.
--   2. Re-assert the constraint so installs that skipped 017 (e.g.
--      from a `cp -r` upgrade rather than running setup.sh) end up
--      in the same final state.
--
-- Idempotent. Safe to re-run on any v2-or-later install.

BEGIN;

-- Make byte_offset nullable in case 017 was skipped.
ALTER TABLE repo_objects ALTER COLUMN byte_offset DROP NOT NULL;

-- Drop the constraint before backfilling so the UPDATE can move rows
-- that currently violate the rule.
ALTER TABLE repo_objects
    DROP CONSTRAINT IF EXISTS byte_offset_matches_pack;

-- Backfill: loose objects (pack_id IS NULL) must have NULL byte_offset.
UPDATE repo_objects
   SET byte_offset = NULL
 WHERE pack_id IS NULL
   AND byte_offset IS NOT NULL;

-- Re-assert the constraint.
ALTER TABLE repo_objects
    ADD CONSTRAINT byte_offset_matches_pack CHECK (
        (pack_id IS NULL AND byte_offset IS NULL) OR
        (pack_id IS NOT NULL AND byte_offset IS NOT NULL)
    );

COMMIT;
