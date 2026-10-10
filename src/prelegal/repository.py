"""SQLite persistence and customization API for legal document templates."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import importlib.resources
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from .seed import BUILT_IN_TEMPLATES

_PLACEHOLDER = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}")


class RevisionConflict(RuntimeError):
    """Raised when a document was changed after a caller last read it."""


@dataclass(frozen=True)
class Template:
    slug: str
    title: str
    description: str
    version: str
    variables: tuple[dict[str, Any], ...]
    source_url: str | None
    license: str | None
    attribution: str | None


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    template_slug: str
    template_version: str
    revision: int
    content: str
    values: dict[str, str]


@dataclass(frozen=True)
class DocumentSummary:
    id: str
    title: str
    template_slug: str
    template_version: str
    revision: int
    updated_at: str


class TemplateRepository:
    """Manage immutable templates and independently editable user documents."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        """Apply migrations and insert built-in templates idempotently."""
        migration = (
            importlib.resources.files("prelegal")
            .joinpath("migrations/001_initial.sql")
            .read_text(encoding="utf-8")
        )
        with closing(self._connect()) as connection, connection:
            applied = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not applied or not connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 1"
            ).fetchone():
                connection.executescript(migration)
                connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
            self._seed(connection)

    @staticmethod
    def _seed(connection: sqlite3.Connection) -> None:
        for item in BUILT_IN_TEMPLATES:
            connection.execute(
                """INSERT OR IGNORE INTO legal_templates
                   (slug, title, description, source_url, license, attribution)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    item["slug"], item["title"], item["description"],
                    item["source_url"], item["license"], item["attribution"],
                ),
            )
            template_id = connection.execute(
                "SELECT id FROM legal_templates WHERE slug = ?", (item["slug"],)
            ).fetchone()["id"]
            connection.execute(
                """INSERT OR IGNORE INTO template_versions
                   (template_id, version, body, variables_json, content_sha256, published_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    template_id,
                    item["version"],
                    item["body"],
                    json.dumps(item["variables"], separators=(",", ":")),
                    hashlib.sha256(item["body"].encode()).hexdigest(),
                    item["published_at"],
                ),
            )

    def list_templates(self) -> list[Template]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """SELECT t.*, v.version, v.variables_json
                   FROM legal_templates t JOIN template_versions v ON v.template_id = t.id
                   ORDER BY t.title, v.version"""
            ).fetchall()
        return [self._template_from_row(row) for row in rows]

    def get_template(self, slug: str, version: str = "1.0") -> Template:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """SELECT t.*, v.version, v.variables_json
                   FROM legal_templates t JOIN template_versions v ON v.template_id = t.id
                   WHERE t.slug = ? AND v.version = ?""",
                (slug, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown template version: {slug}@{version}")
        return self._template_from_row(row)

    def create_document(
        self,
        slug: str,
        values: Mapping[str, object],
        *,
        version: str = "1.0",
        title: str | None = None,
    ) -> Document:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """SELECT t.id AS template_id, t.title AS template_title,
                          v.id AS version_id, v.body, v.variables_json
                   FROM legal_templates t JOIN template_versions v ON v.template_id = t.id
                   WHERE t.slug = ? AND v.version = ?""",
                (slug, version),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown template version: {slug}@{version}")
            normalized, content = self._render(row["body"], row["variables_json"], values)
            document_id = str(uuid4())
            document_title = title or row["template_title"]
            connection.execute(
                """INSERT INTO documents(id, template_version_id, title)
                   VALUES (?, ?, ?)""",
                (document_id, row["version_id"], document_title),
            )
            connection.execute(
                """INSERT INTO document_revisions
                   (document_id, revision, content, values_json) VALUES (?, 1, ?, ?)""",
                (document_id, content, json.dumps(normalized, separators=(",", ":"))),
            )
        return self.get_document(document_id)

    def revise_document(
        self, document_id: str, content: str, *, expected_revision: int
    ) -> Document:
        if not content.strip():
            raise ValueError("document content cannot be empty")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT current_revision FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown document: {document_id}")
            if current["current_revision"] != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {current['current_revision']}"
                )
            values_json = connection.execute(
                """SELECT values_json FROM document_revisions
                   WHERE document_id = ? AND revision = ?""",
                (document_id, expected_revision),
            ).fetchone()["values_json"]
            next_revision = expected_revision + 1
            connection.execute(
                """INSERT INTO document_revisions
                   (document_id, revision, content, values_json) VALUES (?, ?, ?, ?)""",
                (document_id, next_revision, content, values_json),
            )
            connection.execute(
                """UPDATE documents SET current_revision = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (next_revision, document_id),
            )
        return self.get_document(document_id)

    def get_document(self, document_id: str, revision: int | None = None) -> Document:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """SELECT d.id, d.title, d.current_revision, r.revision, r.content,
                          r.values_json, t.slug, v.version
                   FROM documents d
                   JOIN template_versions v ON v.id = d.template_version_id
                   JOIN legal_templates t ON t.id = v.template_id
                   JOIN document_revisions r ON r.document_id = d.id
                   WHERE d.id = ? AND r.revision = COALESCE(?, d.current_revision)""",
                (document_id, revision),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown document or revision: {document_id}@{revision}")
        return Document(
            id=row["id"], title=row["title"], template_slug=row["slug"],
            template_version=row["version"], revision=row["revision"],
            content=row["content"], values=json.loads(row["values_json"]),
        )

    def list_documents(self, limit: int = 20) -> list[DocumentSummary]:
        """Return recently updated documents without loading their full content."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """SELECT d.id, d.title, d.current_revision, d.updated_at,
                          t.slug, v.version
                   FROM documents d
                   JOIN template_versions v ON v.id = d.template_version_id
                   JOIN legal_templates t ON t.id = v.template_id
                   ORDER BY d.updated_at DESC, d.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            DocumentSummary(
                id=row["id"],
                title=row["title"],
                template_slug=row["slug"],
                template_version=row["version"],
                revision=row["current_revision"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def list_revisions(self, document_id: str) -> list[int]:
        """List a document's available revision numbers, newest first."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """SELECT revision FROM document_revisions
                   WHERE document_id = ? ORDER BY revision DESC""",
                (document_id,),
            ).fetchall()
        if not rows:
            raise KeyError(f"unknown document: {document_id}")
        return [row["revision"] for row in rows]

    @staticmethod
    def _render(
        body: str, variables_json: str, supplied: Mapping[str, object]
    ) -> tuple[dict[str, str], str]:
        definitions = json.loads(variables_json)
        allowed = {definition["name"] for definition in definitions}
        unknown = set(supplied) - allowed
        if unknown:
            raise ValueError(f"unknown template values: {', '.join(sorted(unknown))}")
        normalized = {key: str(value).strip() for key, value in supplied.items()}
        missing = [
            definition["name"]
            for definition in definitions
            if definition.get("required") and not normalized.get(definition["name"])
        ]
        if missing:
            raise ValueError(f"missing required template values: {', '.join(missing)}")
        return normalized, _PLACEHOLDER.sub(lambda match: normalized[match.group(1)], body)

    @staticmethod
    def _template_from_row(row: sqlite3.Row) -> Template:
        return Template(
            slug=row["slug"], title=row["title"], description=row["description"],
            version=row["version"], variables=tuple(json.loads(row["variables_json"])),
            source_url=row["source_url"], license=row["license"],
            attribution=row["attribution"],
        )
