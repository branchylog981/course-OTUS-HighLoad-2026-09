CREATE INDEX IF NOT EXISTS idx_users_name_prefix
    ON users (first_name varchar_pattern_ops, second_name varchar_pattern_ops);
