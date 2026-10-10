"""Prelegal's versioned legal-template repository."""

from .repository import (
    Document,
    RevisionConflict,
    Template,
    TemplateRepository,
)

__all__ = ["Document", "RevisionConflict", "Template", "TemplateRepository"]
