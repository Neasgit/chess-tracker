CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS puzzles (
    puzzle_id TEXT PRIMARY KEY,
    rating INTEGER NOT NULL,
    rating_deviation INTEGER,
    popularity INTEGER,
    nb_plays INTEGER,
    themes TEXT NOT NULL,
    game_url TEXT,
    fen TEXT NOT NULL,
    moves TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_puzzles_themes ON puzzles(themes);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    puzzle_id TEXT NOT NULL REFERENCES puzzles(puzzle_id),
    attempted_at TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('win', 'loss')),
    time_ms INTEGER,
    puzzle_rating_after INTEGER,
    UNIQUE(user_id, puzzle_id, attempted_at)
);
CREATE INDEX IF NOT EXISTS idx_attempts_user_time ON attempts(user_id, attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_puzzle ON attempts(puzzle_id);
CREATE TABLE IF NOT EXISTS srs (
    user_id INTEGER NOT NULL REFERENCES users(id),
    puzzle_id TEXT NOT NULL REFERENCES puzzles(puzzle_id),
    last_result TEXT NOT NULL CHECK (last_result IN ('win', 'loss')),
    success_streak INTEGER NOT NULL DEFAULT 0,
    interval_days INTEGER NOT NULL DEFAULT 0,
    due_date DATE NOT NULL,
    last_reviewed DATE NOT NULL,
    PRIMARY KEY (user_id, puzzle_id)
);
CREATE INDEX IF NOT EXISTS idx_srs_due ON srs(due_date);
