CREATE TABLE IF NOT EXISTS repo_notifications (
    notif_id    BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES repo_users(user_id) ON DELETE CASCADE,
    repo_id     BIGINT REFERENCES repo_repositories(repo_id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    message     TEXT NOT NULL,
    link        TEXT,
    is_read     BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifs_user ON repo_notifications(user_id, is_read);