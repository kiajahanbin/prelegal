PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS legal_templates (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    source_url TEXT,
    license TEXT,
    attribution TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS template_versions (
    id INTEGER PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES legal_templates(id),
    version TEXT NOT NULL,
    body TEXT NOT NULL,
    variables_json TEXT NOT NULL CHECK (json_valid(variables_json)),
    content_sha256 TEXT NOT NULL,
    published_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (template_id, version)
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    template_version_id INTEGER NOT NULL REFERENCES template_versions(id),
    title TEXT NOT NULL,
    current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS document_revisions (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision > 0),
    content TEXT NOT NULL,
    values_json TEXT NOT NULL CHECK (json_valid(values_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (document_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_template_versions_template
    ON template_versions(template_id);
CREATE INDEX IF NOT EXISTS idx_document_revisions_document
    ON document_revisions(document_id, revision DESC);

-- Published template versions are source records. Users customize copies in
-- document_revisions rather than changing the source text for everyone.
CREATE TRIGGER IF NOT EXISTS immutable_template_versions_update
BEFORE UPDATE ON template_versions
BEGIN
    SELECT RAISE(ABORT, 'published template versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_template_versions_delete
BEFORE DELETE ON template_versions
BEGIN
    SELECT RAISE(ABORT, 'published template versions are immutable');
END;
