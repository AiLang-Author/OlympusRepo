-- sql/005_functions.sql
-- Helper functions for auth and session management
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT

-- =========================================================================
-- USER MANAGEMENT
-- =========================================================================

-- Create a user with a bcrypt-hashed password. Returns user_id.
CREATE OR REPLACE FUNCTION repo_create_user(
    p_username TEXT,
    p_password TEXT,
    p_role     TEXT DEFAULT 'mortal',
    p_full_name TEXT DEFAULT NULL,
    p_email    TEXT DEFAULT NULL
) RETURNS BIGINT AS $$
DECLARE
    new_id BIGINT;
BEGIN
    INSERT INTO repo_users (username, password_hash, role, full_name, email)
    VALUES (p_username, crypt(p_password, gen_salt('bf', 10)), p_role, p_full_name, p_email)
    RETURNING user_id INTO new_id;
    RETURN new_id;
END;
$$ LANGUAGE plpgsql;

-- Verify a password. Returns user_id on success, NULL on failure.
-- Also updates last_login.
CREATE OR REPLACE FUNCTION repo_verify_password(
    p_username TEXT,
    p_password TEXT
) RETURNS BIGINT AS $$
DECLARE
    found_id BIGINT;
BEGIN
    SELECT user_id INTO found_id
      FROM repo_users
     WHERE username = p_username
       AND password_hash = crypt(p_password, password_hash)
       AND is_active = TRUE;

    IF found_id IS NOT NULL THEN
        UPDATE repo_users SET last_login = NOW() WHERE user_id = found_id;
    END IF;

    RETURN found_id;
END;
$$ LANGUAGE plpgsql;

-- =========================================================================
-- SESSION MANAGEMENT
-- =========================================================================

-- Create a new session token. Returns the session_id string.
CREATE OR REPLACE FUNCTION repo_create_session(
    p_user_id   BIGINT,
    p_ip        INET DEFAULT NULL,
    p_agent     TEXT DEFAULT NULL,
    p_ttl_hours INTEGER DEFAULT 24
) RETURNS TEXT AS $$
DECLARE
    new_token TEXT;
BEGIN
    new_token := encode(gen_random_bytes(32), 'hex');
    INSERT INTO repo_sessions (session_id, user_id, expires_at, ip_address, user_agent)
    VALUES (new_token, p_user_id, NOW() + (p_ttl_hours || ' hours')::INTERVAL, p_ip, p_agent);
    RETURN new_token;
END;
$$ LANGUAGE plpgsql;

-- Validate a session token. Returns user_id if valid and not expired,
-- NULL otherwise. Deletes expired sessions lazily.
CREATE OR REPLACE FUNCTION repo_validate_session(
    p_session_id TEXT
) RETURNS BIGINT AS $$
DECLARE
    found_user BIGINT;
BEGIN
    SELECT user_id INTO found_user
      FROM repo_sessions
     WHERE session_id = p_session_id
       AND expires_at > NOW();

    -- Opportunistic cleanup (cheap, bounded)
    DELETE FROM repo_sessions
     WHERE expires_at < NOW() - INTERVAL '7 days';

    RETURN found_user;
END;
$$ LANGUAGE plpgsql;
