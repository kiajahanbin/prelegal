"""Unified standard-library web application for creating and editing Mutual NDAs."""

from __future__ import annotations

import argparse
from html import escape
from http import HTTPStatus
from pathlib import Path
import re
from typing import Callable, Iterable
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from .repository import Document, RevisionConflict, TemplateRepository

StartResponse = Callable[[str, list[tuple[str, str]]], None]
_DOCUMENT_PATH = re.compile(
    r"^/documents/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:/(download|download\.html|print))?$"
)
_MAX_REQUEST_BYTES = 128 * 1024
_FIELDS = (
    ("first_party_name", "First party legal name", "text", 200),
    ("second_party_name", "Second party legal name", "text", 200),
    ("purpose", "Purpose of disclosure", "textarea", 1000),
    ("effective_date", "Effective date", "date", 10),
    ("mnda_term", "Agreement term", "text", 100),
    ("confidentiality_term", "Confidentiality term", "text", 100),
    ("governing_law", "State of governing law", "text", 100),
    ("jurisdiction", "Court jurisdiction", "text", 200),
)


def _inline_markdown(text: str) -> str:
    """Render safe inline emphasis after escaping all user-controlled text."""
    safe = escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)


def render_markdown(markdown: str) -> str:
    """Render the small Markdown subset used by legal templates without raw HTML."""
    lines = markdown.splitlines()
    result: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            result.append(f"<p>{_inline_markdown(' '.join(paragraph))}</p>")
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            flush_paragraph()
            index += 1
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            result.append(f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>")
            index += 1
            continue
        if line == "---":
            flush_paragraph()
            result.append("<hr>")
            index += 1
            continue
        if (
            line.startswith("|")
            and index + 1 < len(lines)
            and re.match(r"^\|?(?:\s*:?-+:?\s*\|)+\s*$", lines[index + 1].strip())
        ):
            flush_paragraph()
            headers = [cell.strip() for cell in line.strip("|").split("|")]
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(
                    [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                )
                index += 1
            head = "".join(f"<th>{_inline_markdown(cell)}</th>" for cell in headers)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline_markdown(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            result.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue
        paragraph.append(line)
        index += 1
    flush_paragraph()
    return "".join(result)


class PrelegalApplication:
    """WSGI application that unifies the template store and document creator."""

    def __init__(self, database: str | Path) -> None:
        self.repository = TemplateRepository(database)

    def __call__(
        self, environ: dict[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        if method == "GET" and path == "/":
            return self._html(start_response, HTTPStatus.OK, self._home_page())
        if method == "POST" and path == "/documents":
            return self._create_document(environ, start_response)

        match = _DOCUMENT_PATH.fullmatch(path)
        if match:
            document_id, action = match.groups()
            if method == "GET" and action is None:
                return self._view_document(document_id, environ, start_response)
            if method == "POST" and action is None:
                return self._revise_document(document_id, environ, start_response)
            if method == "GET" and action:
                return self._export_document(document_id, action, start_response)
        return self._html(
            start_response,
            HTTPStatus.NOT_FOUND,
            self._message_page("Page not found", "The requested page does not exist."),
        )

    def _create_document(
        self, environ: dict[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        values: dict[str, str] = {}
        try:
            form = self._read_form(environ)
            values = {name: form.get(name, "").strip() for name, *_ in _FIELDS}
            self._validate_lengths(values)
            title = f"{values['first_party_name']} / {values['second_party_name']} Mutual NDA"
            document = self.repository.create_document(
                "common-paper-mutual-nda", values, title=title
            )
        except (UnicodeDecodeError, ValueError) as error:
            return self._html(
                start_response,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                self._home_page(str(error), values),
            )
        return self._html(
            start_response,
            HTTPStatus.CREATED,
            self._document_page(document, notice="Document created. Revision 1 is saved."),
        )

    def _view_document(
        self,
        document_id: str,
        environ: dict[str, object],
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        try:
            requested_revision = int(query["revision"][0]) if "revision" in query else None
            document = self.repository.get_document(document_id, requested_revision)
            current = self.repository.get_document(document_id)
        except (KeyError, ValueError):
            return self._document_not_found(start_response)
        historical = document.revision != current.revision
        notice = f"Viewing historical revision {document.revision}." if historical else None
        return self._html(
            start_response,
            HTTPStatus.OK,
            self._document_page(document, notice=notice, editable=not historical),
        )

    def _revise_document(
        self,
        document_id: str,
        environ: dict[str, object],
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        try:
            form = self._read_form(environ)
            expected_revision = int(form.get("expected_revision", ""))
            content = form.get("content", "")
            document = self.repository.revise_document(
                document_id, content, expected_revision=expected_revision
            )
        except RevisionConflict as error:
            current = self.repository.get_document(document_id)
            return self._html(
                start_response,
                HTTPStatus.CONFLICT,
                self._document_page(current, error=str(error)),
            )
        except KeyError:
            return self._document_not_found(start_response)
        except ValueError as error:
            try:
                current = self.repository.get_document(document_id)
            except KeyError:
                return self._document_not_found(start_response)
            return self._html(
                start_response,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                self._document_page(current, error=str(error)),
            )
        return self._html(
            start_response,
            HTTPStatus.OK,
            self._document_page(document, notice=f"Revision {document.revision} saved."),
        )

    def _export_document(
        self, document_id: str, action: str, start_response: StartResponse
    ) -> Iterable[bytes]:
        try:
            document = self.repository.get_document(document_id)
        except KeyError:
            return self._document_not_found(start_response)

        if action == "download":
            return self._response(
                start_response,
                HTTPStatus.OK,
                document.content.encode("utf-8"),
                "text/markdown; charset=utf-8",
                [("Content-Disposition", 'attachment; filename="mutual-nda.md"')],
            )
        printable = self._print_page(document).encode("utf-8")
        disposition = []
        if action == "download.html":
            disposition.append(("Content-Disposition", 'attachment; filename="mutual-nda.html"'))
        return self._response(
            start_response,
            HTTPStatus.OK,
            printable,
            "text/html; charset=utf-8",
            disposition,
        )

    @staticmethod
    def _read_form(environ: dict[str, object]) -> dict[str, str]:
        try:
            length = int(str(environ.get("CONTENT_LENGTH") or "0"))
        except ValueError as error:
            raise ValueError("invalid request size") from error
        if length < 0 or length > _MAX_REQUEST_BYTES:
            raise ValueError("form submission is too large")
        stream = environ.get("wsgi.input")
        if stream is None or not hasattr(stream, "read"):
            raise ValueError("form body is missing")
        raw = stream.read(length)  # type: ignore[union-attr]
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
        return {name: values[0] for name, values in parsed.items()}

    @staticmethod
    def _validate_lengths(values: dict[str, str]) -> None:
        oversized = [
            label
            for name, label, _kind, maximum in _FIELDS
            if len(values.get(name, "")) > maximum
        ]
        if oversized:
            raise ValueError(f"value is too long: {', '.join(oversized)}")

    def _home_page(
        self, error: str | None = None, values: dict[str, str] | None = None
    ) -> str:
        values = values or {}
        fields = []
        for name, label, kind, maximum in _FIELDS:
            value = escape(values.get(name, ""), quote=True)
            if kind == "textarea":
                control = (
                    f'<textarea id="{name}" name="{name}" maxlength="{maximum}" '
                    f'required>{value}</textarea>'
                )
            else:
                control = (
                    f'<input id="{name}" name="{name}" type="{kind}" '
                    f'maxlength="{maximum}" value="{value}" required>'
                )
            fields.append(f'<div class="field"><label for="{name}">{label}</label>{control}</div>')

        documents = self.repository.list_documents(8)
        recent = "".join(
            f'<a class="document-card" href="/documents/{item.id}">'
            f'<strong>{escape(item.title)}</strong><span>Revision {item.revision} · '
            f'{escape(item.updated_at)} UTC</span></a>'
            for item in documents
        ) or '<p class="empty">No documents yet. Create your first Mutual NDA below.</p>'
        alert = f'<div class="alert" role="alert">{escape(error)}</div>' if error else ""
        content = f"""
        <main class="shell">
          <header class="hero"><div><span class="eyebrow">Prelegal workspace</span>
            <h1>Legal documents,<br><em>made practical.</em></h1>
            <p>Create, review, revise, and export a Mutual NDA from one private local workspace.</p></div>
            <div class="hero-mark" aria-hidden="true">P</div>
          </header>
          <section class="recent"><div class="section-heading"><div><span class="eyebrow">Workspace</span>
            <h2>Recent documents</h2></div><span class="count">{len(documents)} saved</span></div>
            <div class="document-grid">{recent}</div></section>
          {alert}
          <form action="/documents" method="post" class="creator">
            <div class="section-heading"><div><span class="eyebrow">New agreement</span>
              <h2>Create a Mutual NDA</h2></div><span class="step">01 / Details</span></div>
            <section class="card form-grid">{''.join(fields)}</section>
            <div class="form-footer"><p class="notice">Uses Common Paper MNDA v1.0 · CC BY 4.0.<br>
              Templates are starting points, not legal advice.</p>
              <button type="submit">Create and preview <span aria-hidden="true">→</span></button></div>
          </form>
        </main>"""
        return self._layout("Mutual NDA workspace", content)

    def _document_page(
        self,
        document: Document,
        *,
        notice: str | None = None,
        error: str | None = None,
        editable: bool = True,
    ) -> str:
        revisions = self.repository.list_revisions(document.id)
        revision_links = "".join(
            f'<a class="revision {"active" if revision == document.revision else ""}" '
            f'href="/documents/{document.id}?revision={revision}">v{revision}</a>'
            for revision in revisions
        )
        banner = ""
        if notice:
            banner = f'<div class="success" role="status">{escape(notice)}</div>'
        if error:
            banner = f'<div class="alert" role="alert">{escape(error)} Reloaded the latest version.</div>'
        editor = ""
        if editable:
            editor = f"""
            <details class="editor"><summary>Edit agreement text</summary>
              <form method="post" action="/documents/{document.id}">
                <input type="hidden" name="expected_revision" value="{document.revision}">
                <label for="content">Document Markdown</label>
                <textarea id="content" name="content" maxlength="100000" required>{escape(document.content)}</textarea>
                <button type="submit">Save new revision</button>
              </form></details>"""
        else:
            editor = f'<p><a href="/documents/{document.id}">Return to current revision →</a></p>'
        content = f"""
        <main class="shell shell-wide">
          <nav><a href="/">← Workspace</a></nav>{banner}
          <header class="document-header"><div><span class="eyebrow">Mutual NDA · Revision {document.revision}</span>
            <h1>{escape(document.title)}</h1><div class="revisions" aria-label="Revision history">{revision_links}</div></div>
            <div class="actions"><a class="button secondary" href="/documents/{document.id}/print">Print / PDF</a>
              <a class="button secondary" href="/documents/{document.id}/download.html">Download HTML</a>
              <a class="button" href="/documents/{document.id}/download">Download Markdown</a></div>
          </header>
          <article class="paper legal-document">{render_markdown(document.content)}</article>
          {editor}
          <p class="attribution">Common Paper Mutual NDA Standard Terms, Version 1.0 · CC BY 4.0</p>
        </main>"""
        return self._layout(document.title, content)

    def _print_page(self, document: Document) -> str:
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(document.title)}</title>
<style>body{{max-width:800px;margin:40px auto;padding:0 24px;color:#202723;font:16px/1.65 Georgia,serif}}
h1,h2,h3{{line-height:1.2}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #aaa;padding:8px;text-align:left}}
@media print{{body{{margin:0;max-width:none}}}}</style></head><body>{render_markdown(document.content)}</body></html>"""

    def _document_not_found(self, start_response: StartResponse) -> Iterable[bytes]:
        return self._html(
            start_response,
            HTTPStatus.NOT_FOUND,
            self._message_page("Document not found", "This document or revision is unavailable."),
        )

    def _message_page(self, title: str, message: str) -> str:
        return self._layout(
            title,
            f'<main class="shell"><section class="card"><h1>{escape(title)}</h1>'
            f'<p>{escape(message)}</p><a href="/">Return to the workspace</a></section></main>',
        )

    @staticmethod
    def _layout(title: str, content: str) -> str:
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Prelegal</title>
<style>{_STYLES}</style></head><body>{content}</body></html>"""

    @classmethod
    def _html(
        cls, start_response: StartResponse, status: HTTPStatus, page: str
    ) -> Iterable[bytes]:
        return cls._response(
            start_response, status, page.encode("utf-8"), "text/html; charset=utf-8"
        )

    @staticmethod
    def _response(
        start_response: StartResponse,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> Iterable[bytes]:
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"),
        ]
        headers.extend(extra_headers or [])
        start_response(f"{status.value} {status.phrase}", headers)
        return [body]


_STYLES = """
:root{--ink:#15241e;--muted:#617068;--paper:#fffefa;--accent:#176b4d;--line:#d8e2dc;--wash:#edf4ef;--gold:#c8a96a}
*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,sans-serif;line-height:1.55}
.shell{width:min(1120px,calc(100% - 32px));margin:46px auto 90px}.shell-wide{width:min(1240px,calc(100% - 32px))}a{color:var(--accent);font-weight:700}
h1,h2{font-family:Georgia,serif;letter-spacing:-.035em}h1{font-size:clamp(2.4rem,7vw,5.4rem);line-height:.98;margin:.3rem 0 1rem}h2{font-size:2rem;margin:.2rem 0}p{color:var(--muted)}
.eyebrow{color:var(--accent);font-size:.75rem;font-weight:850;letter-spacing:.15em;text-transform:uppercase}.hero{display:flex;justify-content:space-between;align-items:center;min-height:330px}.hero em{color:var(--accent);font-weight:400}.hero p{max-width:600px;font-size:1.1rem}.hero-mark{display:grid;place-items:center;width:180px;height:180px;border:1px solid var(--accent);border-radius:50%;font:italic 7rem Georgia;color:var(--accent);opacity:.75}
.section-heading,.document-header,.form-footer{display:flex;align-items:end;justify-content:space-between;gap:24px}.count,.step{color:var(--muted);font-size:.85rem}.recent{margin-bottom:70px}.document-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-top:18px}.document-card{display:grid;gap:8px;background:#f9fcfa;border:1px solid var(--line);border-radius:12px;padding:18px;text-decoration:none;color:var(--ink)}.document-card:hover{border-color:var(--accent);transform:translateY(-2px)}.document-card span{color:var(--muted);font-size:.78rem}.empty{border:1px dashed #aebdb4;padding:20px;border-radius:12px}
.creator{border-top:1px solid #b9c8bf;padding-top:40px}.card,.paper,.editor{background:var(--paper);border:1px solid var(--line);box-shadow:0 18px 50px rgba(20,35,29,.07)}.card{padding:clamp(22px,5vw,42px);border-radius:18px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 26px;margin:20px 0}.field{display:grid;gap:7px;margin:12px 0}.field:has(textarea){grid-column:1/-1}label{font-weight:750;font-size:.9rem}input,textarea{width:100%;border:1px solid #b8c6be;border-radius:8px;background:white;color:var(--ink);font:inherit;padding:12px 14px}textarea{min-height:110px;resize:vertical}input:focus,textarea:focus{outline:3px solid #bde4d3;border-color:var(--accent)}
button,.button{display:inline-block;border:0;border-radius:8px;background:var(--accent);color:white;font:inherit;font-weight:800;padding:13px 18px;text-decoration:none;cursor:pointer}.secondary{background:transparent;color:var(--accent);border:1px solid var(--accent)}button:hover,.button:hover{filter:brightness(.88)}.notice,.attribution{font-size:.82rem}.alert,.success{padding:12px 15px;border-radius:8px;margin:18px 0}.alert{background:#fff2ef;border:1px solid #eab6aa;color:#8e2f1f}.success{background:#e4f5eb;border:1px solid #a3d1b7;color:#155a3d}
nav{margin-bottom:24px}.document-header{margin-bottom:30px}.document-header h1{font-size:clamp(2rem,5vw,3.7rem)}.actions{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:8px}.revisions{display:flex;gap:7px}.revision{padding:4px 9px;border:1px solid var(--line);border-radius:999px;text-decoration:none;font-size:.78rem}.revision.active{background:var(--accent);color:white}.paper{padding:clamp(25px,7vw,82px);border-radius:3px}.legal-document{font:1rem/1.72 Georgia,serif}.legal-document h1{font-size:2.3rem}.legal-document h2{font-size:1.45rem;margin-top:2.2rem}.legal-document h3{font-size:1.15rem}.legal-document p{color:#202723}.legal-document table{border-collapse:collapse;width:100%;margin:24px 0}.legal-document th,.legal-document td{border:1px solid #bdc8c1;padding:9px;text-align:left}.editor{margin-top:22px;padding:20px;border-radius:10px}.editor summary{font-weight:800;cursor:pointer}.editor textarea{min-height:420px;margin:12px 0;font-family:ui-monospace,monospace}
@media(max-width:760px){.hero-mark{display:none}.hero{min-height:280px}.form-grid{grid-template-columns:1fr}.field:has(textarea){grid-column:auto}.document-header,.form-footer{align-items:start;flex-direction:column}.actions{justify-content:flex-start}.shell{margin-top:28px}}
"""


def create_app(database: str | Path = "prelegal.db") -> PrelegalApplication:
    return PrelegalApplication(database)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the unified Prelegal web application")
    parser.add_argument("--database", default="prelegal.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    with make_server(arguments.host, arguments.port, create_app(arguments.database)) as server:
        print(f"Prelegal is available at http://{arguments.host}:{arguments.port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
