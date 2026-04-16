-- sql/006_defaults.sql
-- Default server configuration values
-- Copyright (c) 2026 Sean Collins, 2 Paws Machine and Engineering
-- License: MIT

INSERT INTO repo_server_config (key, value) VALUES
    ('registration_policy',     'open'),
    ('default_repo_visibility', 'public'),
    ('instance_name',           'OlympusRepo'),
    ('instance_url',            'http://localhost:8000'),
    ('max_pack_size_mb',        '512')
ON CONFLICT (key) DO NOTHING;
