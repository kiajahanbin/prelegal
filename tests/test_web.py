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

    def request(self, method="GET", path="/", values=None, query=""):
        body = urlencode(values or {}).encode()
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
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
        self.assertIn("<h1>Mutual Non-Disclosure Agreement</h1>", page)
        self.assertNotIn("<pre>", page)
        match = re.search(r'href="(/documents/[^\"]+/download)"', page)
        self.assertIsNotNone(match)

        download = self.request("GET", match.group(1))
        self.assertTrue(download["status"].startswith("200"))
        self.assertEqual("text/markdown; charset=utf-8", download["headers"]["Content-Type"])
        self.assertIn("Acme & Sons, Inc.", download["body"].decode())
        self.assertIn("attachment", download["headers"]["Content-Disposition"])

        document_path = match.group(1).removesuffix("/download")
        html_download = self.request("GET", document_path + "/download.html")
        self.assertEqual("text/html; charset=utf-8", html_download["headers"]["Content-Type"])
        self.assertIn("mutual-nda.html", html_download["headers"]["Content-Disposition"])

        home = self.request()
        self.assertIn("Acme &amp; Sons, Inc.", home["body"].decode())

    def test_document_can_be_edited_with_revision_history(self):
        created = self.request("POST", "/documents", VALUES)
        match = re.search(r'href="(/documents/[^\"]+)/download"', created["body"].decode())
        document_path = match.group(1)

        updated = self.request(
            "POST",
            document_path,
            {"expected_revision": "1", "content": "# Revised NDA\n\nCustom terms."},
        )
        self.assertTrue(updated["status"].startswith("200"))
        self.assertIn("Revision 2 saved", updated["body"].decode())
        self.assertIn("<h1>Revised NDA</h1>", updated["body"].decode())

        old_revision = self.request("GET", document_path, query="revision=1")
        self.assertIn("Viewing historical revision 1", old_revision["body"].decode())
        self.assertNotIn("Save new revision", old_revision["body"].decode())

        stale = self.request(
            "POST",
            document_path,
            {"expected_revision": "1", "content": "Stale overwrite"},
        )
        self.assertTrue(stale["status"].startswith("409"))
        self.assertIn("found 2", stale["body"].decode())

        empty = self.request(
            "POST", document_path, {"expected_revision": "2", "content": ""}
        )
        self.assertTrue(empty["status"].startswith("422"))
        self.assertIn("cannot be empty", empty["body"].decode())

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
