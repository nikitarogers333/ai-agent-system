CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt TEXT NOT NULL,
  session TEXT DEFAULT NULL,
  status TEXT DEFAULT 'queued' CHECK(status IN ('queued','running','done','failed','cancelled')),
  priority INTEGER DEFAULT 5,
  depends_on TEXT DEFAULT NULL,
  created_at DATETIME DEFAULT (datetime('now')),
  started_at DATETIME DEFAULT NULL,
  completed_at DATETIME DEFAULT NULL,
  result_file TEXT DEFAULT NULL,
  error TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_priority ON tasks(priority DESC, created_at ASC);
