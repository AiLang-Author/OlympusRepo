-- sql/004_rls.sql
-- Row-level security for private mana
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT
--
-- The application sets the current user with:
--   SELECT set_config('app.current_user_id', '<user_id>', false);
-- This matches db.set_session_user() in the Python layer.

ALTER TABLE repo_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE repo_messages FORCE ROW LEVEL SECURITY;

-- Public mana: everyone can see
CREATE POLICY mana_public_read ON repo_messages
    FOR SELECT
    USING (is_private = FALSE);

-- Private mana: only sender and recipient
CREATE POLICY mana_private_read ON repo_messages
    FOR SELECT
    USING (
        is_private = TRUE
        AND (
            sender_id = NULLIF(current_setting('app.current_user_id', true), '')::BIGINT
            OR recipient_id = NULLIF(current_setting('app.current_user_id', true), '')::BIGINT
        )
    );

-- Inserts: auth check is at application layer
CREATE POLICY mana_insert ON repo_messages
    FOR INSERT
    WITH CHECK (TRUE);
