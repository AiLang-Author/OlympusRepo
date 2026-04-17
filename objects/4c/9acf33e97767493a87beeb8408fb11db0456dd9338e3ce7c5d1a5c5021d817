-- Add threading support to repo_messages
ALTER TABLE repo_messages ADD COLUMN IF NOT EXISTS
    parent_id BIGINT REFERENCES repo_messages(message_id) ON DELETE CASCADE;

ALTER TABLE repo_messages ADD COLUMN IF NOT EXISTS
    thread_id BIGINT REFERENCES repo_messages(message_id) ON DELETE CASCADE;

ALTER TABLE repo_messages ADD COLUMN IF NOT EXISTS
    reply_count INTEGER DEFAULT 0;

ALTER TABLE repo_messages ADD COLUMN IF NOT EXISTS
    edited_at TIMESTAMPTZ;

-- Line-level comments need line number stored in context_id
-- Format: "filepath:linenum" e.g. "src/auth.py:42"
-- Already supported by existing context_type='file' + context_id

-- Direct message inbox table
-- repo_messages already handles DMs via is_private + recipient_id
-- but we need a read receipt system
CREATE TABLE IF NOT EXISTS repo_message_reads (
    user_id     BIGINT NOT NULL REFERENCES repo_users(user_id) ON DELETE CASCADE,
    message_id  BIGINT NOT NULL REFERENCES repo_messages(message_id) ON DELETE CASCADE,
    read_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_msg_reads_user
    ON repo_message_reads(user_id);

CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON repo_messages(thread_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_messages_parent
    ON repo_messages(parent_id);

CREATE INDEX IF NOT EXISTS idx_messages_recipient
    ON repo_messages(recipient_id, is_private)
    WHERE is_private = TRUE;