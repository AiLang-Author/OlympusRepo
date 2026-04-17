CREATE TABLE IF NOT EXISTS repo_password_resets (
    reset_id    BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES repo_users(user_id) ON DELETE CASCADE,
    token       TEXT UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used        BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_resets_token ON repo_password_resets(token) WHERE used = FALSE;