CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    first_name VARCHAR(100) NOT NULL CHECK (length(btrim(first_name)) > 0),
    second_name VARCHAR(100) NOT NULL CHECK (length(btrim(second_name)) > 0),
    birthdate DATE NOT NULL CHECK (birthdate >= DATE '1900-01-01'),
    gender VARCHAR(20) NOT NULL CHECK (gender IN ('male', 'female', 'other', 'unspecified')),
    biography TEXT NOT NULL CHECK (length(biography) <= 5000),
    city VARCHAR(100) NOT NULL CHECK (length(btrim(city)) > 0),
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
