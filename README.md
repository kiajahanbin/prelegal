# prelegal

A platform for drafting common legal agreements.

> **Project Status:** Work in progress. The project is expected to be completed next week.

## Legal template database

The initial backend stores immutable, versioned legal templates in SQLite and creates
independently editable user documents from them. It ships with the Common Paper Mutual
NDA Standard Terms Version 1.0 under the CC BY 4.0 license.

```python
from prelegal import TemplateRepository

repository = TemplateRepository("prelegal.db")
document = repository.create_document(
    "common-paper-mutual-nda",
    {
        "first_party_name": "Acme, Inc.",
        "second_party_name": "Example LLC",
        "purpose": "evaluating a commercial relationship",
        "effective_date": "2026-10-11",
        "mnda_term": "2 years",
        "confidentiality_term": "3 years",
        "governing_law": "California",
        "jurisdiction": "San Francisco County, California",
    },
)

# User edits create an auditable revision and cannot overwrite a newer edit.
document = repository.revise_document(
    document.id,
    document.content + "\n\nAdditional negotiated language.",
    expected_revision=document.revision,
)
```

Run the test suite with only the Python standard library:

```shell
python -m unittest discover -s tests -v
```

Legal templates are starting points, not legal advice. Users should have completed
documents reviewed by qualified counsel.
