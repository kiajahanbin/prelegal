"""Prelegal's versioned legal-template repository."""

from .repository import (
    Document,
    DocumentSummary,
    RevisionConflict,
    Template,
    TemplateRepository,
)

__all__ = [
    "Document",
    "DocumentSummary",
    "RevisionConflict",
    "Template",
    "TemplateRepository",
]
