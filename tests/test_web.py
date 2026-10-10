from io import BytesIO
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import urlencode

from prelegal.web import create_app


VALUES = {
    "first_party_name": "Acme & Sons, Inc.",
    "second_party_name": "Example LLC",
    "purpose": "Evaluating a commercial relationship",
    "effective_date": "2026-10-11",
    "mnda_term": "2 years",
    "confidentiality_term": "3 years",
    "governing_law": "California",
    "jurisdiction": "San Francisco County, California",
}


class MutualNDAApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "web.db"
        self.application = create_app(database)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def request(self, method="GET", path="/", values=None):
        body = urlencode(values or {}).encode()
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": BytesIO(body),
        }
        response = {}

        def start_response(status, headers):
            response["status"] = status
            response["headers"] = dict(headers)

        response["body"] = b"".join(self.application(environ, start_response))
        return response

    def test_form_lists_required_agreement_fields(self):
        response = self.request()

        self.assertTrue(response["status"].startswith("200"))
        page = response["body"].decode()
        self.assertIn("Create a Mutual NDA", page)
        self.assertIn('name="first_party_name"', page)
        self.assertIn('name="governing_law"', page)

    def test_create_previews_escaped_document_and_downloads_markdown(self):
        response = self.request("POST", "/documents", VALUES)

        self.assertTrue(response["status"].startswith("201"))
        page = response["body"].decode()
        self.assertIn("Acme &amp; Sons, Inc.", page)
        match = re.search(r'href="(/documents/[^\"]+/download)"', page)
        self.assertIsNotNone(match)

        download = self.request("GET", match.group(1))
        self.assertTrue(download["status"].startswith("200"))
        self.assertEqual("text/markdown; charset=utf-8", download["headers"]["Content-Type"])
        self.assertIn("Acme & Sons, Inc.", download["body"].decode())
        self.assertIn("attachment", download["headers"]["Content-Disposition"])

    def test_invalid_submission_returns_form_with_error_and_preserves_values(self):
        incomplete = dict(VALUES)
        incomplete["purpose"] = ""
        response = self.request("POST", "/documents", incomplete)

        self.assertTrue(response["status"].startswith("422"))
        page = response["body"].decode()
        self.assertIn('role="alert"', page)
        self.assertIn("purpose", page)
        self.assertIn("Example LLC", page)

    def test_unknown_document_returns_not_found(self):
        response = self.request(
            "GET", "/documents/00000000-0000-0000-0000-000000000000/download"
        )
        self.assertTrue(response["status"].startswith("404"))


if __name__ == "__main__":
    unittest.main()
