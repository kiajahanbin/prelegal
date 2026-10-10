"""Small standard-library web prototype for creating Mutual NDAs."""

from __future__ import annotations

import argparse
from html import escape
from http import HTTPStatus
from pathlib import Path
import re
from typing import Callable, Iterable
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from .repository import Document, TemplateRepository

StartResponse = Callable[[str, list[tuple[str, str]]], None]
_DOCUMENT_PATH = re.compile(
    r"^/documents/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/(download)$"
)
_MAX_REQUEST_BYTES = 64 * 1024
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


class MutualNDAApplication:
    """WSGI application for creating, previewing, and downloading an MNDA."""

    def __init__(self, database: str | Path) -> None:
        self.repository = TemplateRepository(database)

    def __call__(
        self, environ: dict[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))

        if method == "GET" and path == "/":
            return self._html(start_response, HTTPStatus.OK, self._form_page())
        if method == "POST" and path == "/documents":
            return self._create_document(environ, start_response)

        match = _DOCUMENT_PATH.fullmatch(path)
        if method == "GET" and match:
            return self._download(match.group(1), start_response)
        return self._html(
            start_response,
            HTTPStatus.NOT_FOUND,
            self._message_page("Page not found", "The requested page does not exist."),
        )

    def _create_document(
        self, environ: dict[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        try:
            values = self._read_form(environ)
            self._validate_lengths(values)
            title = (
                f"{values['first_party_name']} / {values['second_party_name']} Mutual NDA"
            )
            document = self.repository.create_document(
                "common-paper-mutual-nda", values, title=title
            )
        except (UnicodeDecodeError, ValueError) as error:
            return self._html(
                start_response,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                self._form_page(str(error), locals().get("values", {})),
            )
        return self._html(
            start_response, HTTPStatus.CREATED, self._preview_page(document)
        )

    def _download(self, document_id: str, start_response: StartResponse) -> Iterable[bytes]:
        try:
            document = self.repository.get_document(document_id)
        except KeyError:
            return self._html(
                start_response,
                HTTPStatus.NOT_FOUND,
                self._message_page("Document not found", "This document is unavailable."),
            )
        body = document.content.encode("utf-8")
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/markdown; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Content-Disposition", 'attachment; filename="mutual-nda.md"'),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [body]

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
        form = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
        return {name: form.get(name, [""])[0].strip() for name, *_ in _FIELDS}

    @staticmethod
    def _validate_lengths(values: dict[str, str]) -> None:
        oversized = [
            label
            for name, label, _kind, maximum in _FIELDS
            if len(values.get(name, "")) > maximum
        ]
        if oversized:
            raise ValueError(f"value is too long: {', '.join(oversized)}")

    def _form_page(
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
            fields.append(
                f'<div class="field"><label for="{name}">{escape(label)}</label>{control}</div>'
            )
        alert = f'<div class="alert" role="alert">{escape(error)}</div>' if error else ""
        content = f"""
        <main class="shell">
          <header><span class="eyebrow">Prelegal prototype</span>
            <h1>Create a Mutual NDA</h1>
            <p>Enter the agreement details, review the completed document, and download it locally.</p>
          </header>
          {alert}
          <form action="/documents" method="post">
            <section class="card"><h2>Agreement details</h2>{''.join(fields)}</section>
            <p class="notice">This template is a starting point, not legal advice. Consider review by qualified counsel.</p>
            <button type="submit">Create and preview NDA <span aria-hidden="true">→</span></button>
          </form>
        </main>"""
        return self._layout("Create a Mutual NDA", content)

    def _preview_page(self, document: Document) -> str:
        content = f"""
        <main class="shell shell-wide">
          <nav><a href="/">← Create another NDA</a></nav>
          <header class="preview-header"><div><span class="eyebrow">Document preview</span>
            <h1>{escape(document.title)}</h1><p>Review the generated agreement before downloading.</p></div>
            <a class="button" href="/documents/{document.id}/download">Download Markdown</a>
          </header>
          <article class="paper"><pre>{escape(document.content)}</pre></article>
          <p class="attribution">Common Paper Mutual NDA Standard Terms, Version 1.0 · CC BY 4.0</p>
        </main>"""
        return self._layout(document.title, content)

    def _message_page(self, title: str, message: str) -> str:
        return self._layout(
            title,
            f'<main class="shell"><section class="card"><h1>{escape(title)}</h1>'
            f'<p>{escape(message)}</p><a href="/">Return to the form</a></section></main>',
        )

    @staticmethod
    def _layout(title: str, content: str) -> str:
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · Prelegal</title><style>
:root{{--ink:#14231d;--muted:#617068;--paper:#fffefa;--accent:#176b4d;--line:#dce4df;--wash:#eef5f0}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--wash);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,sans-serif;line-height:1.55}}
.shell{{width:min(760px,calc(100% - 32px));margin:48px auto 80px}}.shell-wide{{width:min(1060px,calc(100% - 32px))}}
header{{margin-bottom:28px}}h1{{font-family:Georgia,serif;font-size:clamp(2.2rem,7vw,4.6rem);line-height:1.02;letter-spacing:-.035em;margin:.25rem 0 1rem}}h2{{font-family:Georgia,serif;margin-top:0}}
.eyebrow{{color:var(--accent);font-size:.78rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase}}p{{color:var(--muted)}}
.card,.paper{{background:var(--paper);border:1px solid var(--line);box-shadow:0 18px 50px rgba(20,35,29,.08)}}.card{{padding:clamp(22px,5vw,42px);border-radius:18px}}
.field{{display:grid;gap:7px;margin:20px 0}}label{{font-weight:700;font-size:.91rem}}input,textarea{{width:100%;border:1px solid #b8c6be;border-radius:8px;background:white;color:var(--ink);font:inherit;padding:12px 14px}}textarea{{min-height:110px;resize:vertical}}input:focus,textarea:focus{{outline:3px solid #bde4d3;border-color:var(--accent)}}
button,.button{{display:inline-block;border:0;border-radius:9px;background:var(--accent);color:white;font:inherit;font-weight:800;padding:13px 19px;text-decoration:none;cursor:pointer}}button:hover,.button:hover{{background:#0f543b}}.notice,.attribution{{font-size:.84rem}}.alert{{background:#fff2ef;border:1px solid #eab6aa;color:#8e2f1f;padding:12px 15px;border-radius:8px;margin-bottom:20px}}
nav{{margin-bottom:22px}}a{{color:var(--accent);font-weight:700}}.preview-header{{display:flex;align-items:end;justify-content:space-between;gap:24px}}.preview-header h1{{font-size:clamp(2rem,5vw,3.5rem)}}
.paper{{padding:clamp(24px,6vw,72px);border-radius:3px}}pre{{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:1rem/1.72 Georgia,serif;color:#202723}}
@media(max-width:700px){{.preview-header{{align-items:start;flex-direction:column}}.shell{{margin-top:28px}}}}
</style></head><body>{content}</body></html>"""

    @staticmethod
    def _html(
        start_response: StartResponse, status: HTTPStatus, page: str
    ) -> Iterable[bytes]:
        body = page.encode("utf-8")
        start_response(
            f"{status.value} {status.phrase}",
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
                ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"),
            ],
        )
        return [body]


def create_app(database: str | Path = "prelegal.db") -> MutualNDAApplication:
    return MutualNDAApplication(database)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Prelegal Mutual NDA prototype")
    parser.add_argument("--database", default="prelegal.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    with make_server(arguments.host, arguments.port, create_app(arguments.database)) as server:
        print(f"Prelegal is available at http://{arguments.host}:{arguments.port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
