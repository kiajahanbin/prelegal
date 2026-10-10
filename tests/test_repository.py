from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from prelegal import RevisionConflict, TemplateRepository


VALUES = {
    "first_party_name": "Acme, Inc.",
    "second_party_name": "Example LLC",
    "purpose": "evaluating a commercial relationship",
    "effective_date": "2026-10-11",
    "mnda_term": "2 years",
    "confidentiality_term": "3 years",
    "governing_law": "California",
    "jurisdiction": "San Francisco County, California",
}


class TemplateRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "prelegal.db"
        self.repository = TemplateRepository(self.database)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_initialization_is_idempotent_and_seeds_attributed_template(self):
        TemplateRepository(self.database)
        templates = self.repository.list_templates()

        self.assertEqual(1, len(templates))
        self.assertEqual("common-paper-mutual-nda", templates[0].slug)
        self.assertEqual("CC-BY-4.0", templates[0].license)
        self.assertIn("Common Paper", templates[0].attribution)

    def test_create_document_renders_values_without_mutating_template(self):
        document = self.repository.create_document(
            "common-paper-mutual-nda", VALUES, title="Acme / Example MNDA"
        )

        self.assertEqual(1, document.revision)
        self.assertIn("**Acme, Inc.**", document.content)
        self.assertNotIn("{{first_party_name}}", document.content)
        self.assertEqual(VALUES, document.values)

    def test_create_document_rejects_missing_and_unknown_values(self):
        incomplete = dict(VALUES)
        del incomplete["purpose"]
        with self.assertRaisesRegex(ValueError, "purpose"):
            self.repository.create_document("common-paper-mutual-nda", incomplete)

        extra = dict(VALUES, typo="value")
        with self.assertRaisesRegex(ValueError, "typo"):
            self.repository.create_document("common-paper-mutual-nda", extra)

    def test_revisions_preserve_history_and_prevent_lost_updates(self):
        original = self.repository.create_document("common-paper-mutual-nda", VALUES)
        revised = self.repository.revise_document(
            original.id, original.content + "\n\nCustom clause.", expected_revision=1
        )

        self.assertEqual(2, revised.revision)
        self.assertNotIn("Custom clause", self.repository.get_document(original.id, 1).content)
        with self.assertRaises(RevisionConflict):
            self.repository.revise_document(
                original.id, "stale edit", expected_revision=1
            )

    def test_published_template_versions_are_immutable(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute("UPDATE template_versions SET body = 'changed'")


if __name__ == "__main__":
    unittest.main()
