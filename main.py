import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import extract_msg
from fastapi import Body, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from pydantic import BaseModel, Field
from services.base64_helper import maybe_double_decode_email_file


SERVICE_NAME = "html-to-pdf-service"
SERVICE_VERSION = "1.0.0"
DEFAULT_FILE_NAME = "document.pdf"
HTML_EXTENSIONS = {".html", ".htm"}
EMAIL_EXTENSIONS = {".eml", ".msg"}
OFFICE_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
TEXT_EXTENSIONS = {".txt"}
PDF_EXTENSIONS = {".pdf"}


def env_to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.render_timeout_ms = int(os.getenv("RENDER_TIMEOUT_MS", "30000"))
        self.log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        self.libreoffice_binary = os.getenv("LIBREOFFICE_BINARY", "libreoffice")
        self.strict_external_resources = env_to_bool(
            os.getenv("STRICT_EXTERNAL_RESOURCES"),
            default=False,
        )


settings = Settings()


logger = logging.getLogger(SERVICE_NAME)
logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter("%(message)s"))
logger.handlers.clear()
logger.addHandler(stream_handler)
logger.propagate = False


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log_event(level: int, event: str, **fields: Any) -> None:
    payload = {
        "timestamp": utc_timestamp(),
        "service": SERVICE_NAME,
        "event": event,
        **fields,
    }
    logger.log(level, json.dumps(payload, ensure_ascii=True, default=str))


class HTMLStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.start_tag_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.start_tag_count += 1


class ConvertHtmlRequest(BaseModel):
    html: Any = None
    fileName: str | None = None
    metadata: dict[str, Any] | None = Field(default_factory=dict)


@dataclass
class EmailAttachmentSummary:
    name: str
    mime_type: str | None = None
    size_bytes: int | None = None
    is_inline: bool = False


@dataclass
class ExtractedEmailContent:
    html: str
    subject: str | None
    sender: str | None
    to: str | None
    cc: str | None
    bcc: str | None
    date: str | None
    message_id: str | None
    source_format: str
    parser_used: str
    has_html_body: bool
    has_plain_text_body: bool
    attachments: list[EmailAttachmentSummary]
    inline_image_count: int = 0
    cid_found_count: int = 0
    cid_resolved_count: int = 0
    unresolved_inline_count: int = 0
    warnings: list[str] | None = None


@dataclass
class RenderedPdfResult:
    pdf_bytes: bytes
    response_headers: dict[str, str]
    final_status: str
    failed_resource_count: int


@dataclass
class ProcessedItemResult:
    index: int
    original_file_name: str
    output_file_name: str
    detected_type: str
    pdf_bytes: bytes
    metadata: dict[str, Any]
    response_headers: dict[str, str]


class ServiceError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        error_type: str,
        message: str,
        technical_detail: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_type = error_type
        self.message = message
        self.technical_detail = technical_detail
        self.details = details or {}


class BrowserManager:
    def __init__(self) -> None:
        self._playwright = None
        self.browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self.browser is not None:
                return

            self._playwright = await async_playwright().start()
            self.browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--font-render-hinting=none",
                    "--no-sandbox",
                ],
            )
            log_event(logging.INFO, "browser_started", browser="chromium", status="connected")

    async def stop(self) -> None:
        async with self._lock:
            if self.browser is not None:
                await self.browser.close()
                self.browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
            log_event(logging.INFO, "browser_stopped", status="disconnected")


browser_manager = BrowserManager()


def build_error_payload(
    *,
    trace_id: str,
    error_code: str,
    error_type: str,
    message: str,
    technical_detail: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "errorCode": error_code,
        "errorType": error_type,
        "message": message,
        "technicalDetail": technical_detail,
        "timestamp": utc_timestamp(),
        "traceId": trace_id,
    }
    if details:
        payload["details"] = details
    return payload


def sanitize_filename(file_name: str | None) -> str:
    if not file_name:
        return DEFAULT_FILE_NAME

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name.strip())
    cleaned = cleaned.strip("._") or DEFAULT_FILE_NAME
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    return cleaned[:180]


def build_pdf_filename(preferred_file_name: str | None, source_name: str | None = None) -> str:
    if preferred_file_name:
        return sanitize_filename(preferred_file_name)

    if source_name:
        source_stem = Path(source_name).stem.strip()
        if source_stem:
            return sanitize_filename(f"{source_stem}.pdf")

    return DEFAULT_FILE_NAME


def sanitize_original_filename(file_name: str | None, fallback: str = "document") -> str:
    if not file_name:
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name.strip())
    return cleaned.strip("._") or fallback


def detect_file_extension(file_name: str | None) -> str:
    return Path(file_name or "").suffix.lower()


def decode_base64_payload(content_base64: Any) -> bytes:
    if content_base64 is None:
        raise ServiceError(
            status_code=400,
            error_code="EMPTY_FILE",
            error_type="ValidationError",
            message="El archivo adjunto esta vacio.",
            technical_detail="contentBase64 is null, blank or decoded to zero bytes.",
        )

    if not isinstance(content_base64, str) or not content_base64.strip():
        raise ServiceError(
            status_code=400,
            error_code="EMPTY_FILE",
            error_type="ValidationError",
            message="El archivo adjunto esta vacio.",
            technical_detail="contentBase64 is null, blank or decoded to zero bytes.",
        )

    payload = content_base64.strip()
    if "," in payload and payload.lower().startswith("data:"):
        payload = payload.split(",", 1)[1]

    try:
        decoded = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ServiceError(
            status_code=400,
            error_code="INVALID_BASE64",
            error_type="ValidationError",
            message="El contenido recibido no es un Base64 valido.",
            technical_detail="Base64 decode failed.",
        ) from exc

    if not decoded:
        raise ServiceError(
            status_code=400,
            error_code="EMPTY_FILE",
            error_type="ValidationError",
            message="El archivo adjunto esta vacio.",
            technical_detail="contentBase64 is null, blank or decoded to zero bytes.",
        )

    return decoded


def normalize_universal_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ServiceError(
            status_code=400,
            error_code="INVALID_REQUEST_BODY",
            error_type="ValidationError",
            message="The request body could not be processed.",
            technical_detail="Request body must be a JSON object.",
        )

    if "items" in payload:
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ServiceError(
                status_code=400,
                error_code="INVALID_REQUEST_BODY",
                error_type="ValidationError",
                message="The request body could not be processed.",
                technical_detail="items must be a non-empty JSON array.",
            )
        if any(not isinstance(item, dict) for item in items):
            raise ServiceError(
                status_code=400,
                error_code="INVALID_REQUEST_BODY",
                error_type="ValidationError",
                message="The request body could not be processed.",
                technical_detail="Each item inside items must be a JSON object.",
            )
        return items

    return [payload]


def normalize_item_metadata(metadata: Any) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ServiceError(
            status_code=400,
            error_code="INVALID_REQUEST_BODY",
            error_type="ValidationError",
            message="The request body could not be processed.",
            technical_detail="metadata must be a JSON object.",
        )
    return metadata


def resolve_item_content_mode(item: dict[str, Any]) -> str:
    has_html = item.get("html") is not None
    has_file = item.get("contentBase64") is not None

    if has_html and has_file:
        raise ServiceError(
            status_code=400,
            error_code="AMBIGUOUS_CONTENT",
            error_type="ValidationError",
            message="The request contains both html and contentBase64.",
            technical_detail="Provide either html or contentBase64, but not both.",
        )

    if has_html:
        return "html"

    if has_file:
        return "file"

    raise ServiceError(
        status_code=400,
        error_code="MISSING_CONTENT",
        error_type="ValidationError",
        message="The request does not contain html or contentBase64.",
        technical_detail="Each item must include html or contentBase64.",
    )


def validate_html_payload(html: Any) -> str:
    if html is None:
        raise ServiceError(
            status_code=400,
            error_code="HTML_EMPTY",
            error_type="ValidationError",
            message="The HTML content received is empty.",
            technical_detail="Request body html property was null or blank.",
        )

    if not isinstance(html, str):
        raise ServiceError(
            status_code=400,
            error_code="INVALID_HTML",
            error_type="ValidationError",
            message="The HTML payload could not be processed.",
            technical_detail="Request body html property must be a string containing HTML markup.",
        )

    html_text = html.strip()
    if not html_text:
        raise ServiceError(
            status_code=400,
            error_code="HTML_EMPTY",
            error_type="ValidationError",
            message="The HTML content received is empty.",
            technical_detail="Request body html property was null or blank.",
        )

    parser = HTMLStructureParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:
        raise ServiceError(
            status_code=400,
            error_code="INVALID_HTML",
            error_type="ValidationError",
            message="The HTML payload could not be processed.",
            technical_detail=f"HTML parser rejected the payload: {exc}",
        ) from exc

    if parser.start_tag_count == 0 or "<" not in html_text or ">" not in html_text:
        raise ServiceError(
            status_code=400,
            error_code="INVALID_HTML",
            error_type="ValidationError",
            message="The HTML payload could not be processed.",
            technical_detail="The html property did not contain detectable HTML markup.",
        )

    return html_text


def decode_bytes_to_text(payload: bytes | str | None) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload

    for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue

    return payload.decode("utf-8", errors="replace")


def normalize_content_id(content_id: str | None) -> str | None:
    if not content_id:
        return None

    normalized = content_id.strip().strip("<>").strip()
    if normalized.lower().startswith("cid:"):
        normalized = normalized[4:]
    return normalized or None


def build_data_url(resource_bytes: bytes, mime_type: str | None) -> str:
    resolved_mime_type = mime_type or "application/octet-stream"
    encoded = base64.b64encode(resource_bytes).decode("ascii")
    return f"data:{resolved_mime_type};base64,{encoded}"


def embed_inline_resources_with_stats(
    html: str,
    inline_resources: dict[str, dict[str, Any]],
) -> tuple[str, int, int, int]:
    if not inline_resources:
        return html, 0, 0, 0

    cid_pattern = r"cid:([^\"' >)]+)"
    cid_found_count = len(re.findall(cid_pattern, html, flags=re.IGNORECASE))
    resolved_count = 0

    def replace_cid(match: re.Match[str]) -> str:
        nonlocal resolved_count
        content_id = normalize_content_id(match.group(1))
        resource = inline_resources.get(content_id or "")
        if not resource:
            return match.group(0)
        resolved_count += 1
        return build_data_url(resource["data"], resource.get("mimeType"))

    rendered_html = re.sub(cid_pattern, replace_cid, html, flags=re.IGNORECASE)
    unresolved_count = len(re.findall(cid_pattern, rendered_html, flags=re.IGNORECASE))
    return rendered_html, cid_found_count, resolved_count, unresolved_count


def count_unresolved_cids(html: str) -> int:
    return len(re.findall(r"cid:[^\"' >)]+", html, flags=re.IGNORECASE))


def text_to_html_fragment(text: str) -> str:
    escaped_text = html_escape(text or "")
    return escaped_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def text_to_html_document(text: str) -> str:
    body_html = text_to_html_fragment(text)
    return (
        "<html><body style=\"font-family: Arial, sans-serif; white-space: normal;\">"
        f"{body_html}"
        "</body></html>"
    )


def extract_html_body_fragment(html: str) -> str:
    body_match = re.search(r"<body[^>]*>(.*)</body>", html, flags=re.IGNORECASE | re.DOTALL)
    if body_match:
        return body_match.group(1).strip()
    html_without_doctype = re.sub(r"<!DOCTYPE[^>]*>", "", html, flags=re.IGNORECASE).strip()
    html_without_html = re.sub(r"</?html[^>]*>", "", html_without_doctype, flags=re.IGNORECASE).strip()
    return re.sub(r"</?head[^>]*>.*?</head>", "", html_without_html, flags=re.IGNORECASE | re.DOTALL).strip()


def build_email_preview_html(
    *,
    original_file_name: str,
    ticket_id: str | None,
    extracted_email: ExtractedEmailContent,
) -> str:
    fields = [
        ("Archivo original", original_file_name),
        ("Subject", extracted_email.subject),
        ("From", extracted_email.sender),
        ("To", extracted_email.to),
        ("CC", extracted_email.cc),
        ("BCC", extracted_email.bcc),
        ("Date", extracted_email.date),
        ("Message-ID", extracted_email.message_id),
        ("TicketId", ticket_id),
    ]

    metadata_rows = []
    for label, value in fields:
        safe_value = html_escape(value or "")
        metadata_rows.append(
            f"<div class=\"metadata-row\"><span class=\"label\">{html_escape(label)}:</span> {safe_value or '&nbsp;'}</div>"
        )

    attachment_items = []
    for attachment in extracted_email.attachments:
        item_parts = [html_escape(attachment.name)]
        if attachment.mime_type:
            item_parts.append(html_escape(attachment.mime_type))
        if attachment.size_bytes is not None:
            item_parts.append(f"{attachment.size_bytes} bytes")
        if attachment.is_inline:
            item_parts.append("inline")
        attachment_items.append(f"<li>{' | '.join(item_parts)}</li>")

    attachments_list_html = "<ul>" + "".join(attachment_items) + "</ul>" if attachment_items else "<p>No se detectaron adjuntos internos.</p>"

    warnings = list(extracted_email.warnings or [])
    if extracted_email.unresolved_inline_count > 0:
        warnings.append(
            f"No se pudieron resolver {extracted_email.unresolved_inline_count} imagen(es) inline CID y pueden no aparecer en el PDF."
        )

    warnings_html = "".join(
        f"<div class=\"warning\">{html_escape(warning)}</div>"
        for warning in warnings
    )

    body_html = extract_html_body_fragment(extracted_email.html)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{
    font-family: Arial, sans-serif;
    font-size: 12px;
    color: #222;
    margin: 24px;
  }}
  .container {{
    width: 100%;
  }}
  .header {{
    border-bottom: 2px solid #444;
    margin-bottom: 16px;
    padding-bottom: 8px;
  }}
  .title {{
    font-size: 18px;
    font-weight: bold;
  }}
  .metadata {{
    margin-bottom: 16px;
    border: 1px solid #ddd;
    padding: 10px;
    background: #f8f8f8;
  }}
  .metadata-row {{
    margin-bottom: 4px;
    line-height: 1.4;
  }}
  .label {{
    font-weight: bold;
  }}
  .body {{
    margin-top: 16px;
  }}
  .attachments {{
    margin-top: 24px;
    border-top: 1px solid #ddd;
    padding-top: 12px;
  }}
  .warning {{
    margin-top: 12px;
    color: #9a6700;
    background: #fff4ce;
    border: 1px solid #ffcc00;
    padding: 8px;
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title">Correo adjunto Trade Ticket</div>
  </div>

  <div class="metadata">
    {''.join(metadata_rows)}
  </div>

  <div class="body">
    {body_html}
  </div>

  <div class="attachments">
    <h3>Adjuntos internos del correo</h3>
    {attachments_list_html}
  </div>

  {warnings_html}
</div>
</body>
</html>""".strip()


def detect_document_type(item: dict[str, Any], content_mode: str) -> str:
    file_name = item.get("fileName")
    extension = detect_file_extension(file_name)

    if content_mode == "html":
        return "html"

    if extension in PDF_EXTENSIONS:
        return "pdf"
    if extension in HTML_EXTENSIONS:
        return "html"
    if extension == ".eml":
        return "eml"
    if extension == ".msg":
        return "msg"
    if extension in OFFICE_EXTENSIONS:
        return extension.lstrip(".")
    if extension in TEXT_EXTENSIONS:
        return "txt"

    raise ServiceError(
        status_code=400,
        error_code="UNSUPPORTED_FILE_TYPE",
        error_type="ValidationError",
        message="Solo se soportan tipos de archivo compatibles con la API.",
        technical_detail=f"Received extension: {extension or '(none)'}",
    )


async def convert_office_bytes_to_pdf(file_bytes: bytes, original_file_name: str) -> bytes:
    extension = detect_file_extension(original_file_name)
    safe_original_name = sanitize_original_filename(original_file_name, fallback=f"source{extension or ''}")

    with tempfile.TemporaryDirectory(prefix="office-input-") as temp_dir:
        input_path = Path(temp_dir) / safe_original_name
        output_dir = Path(temp_dir) / "output"
        profile_dir = Path(temp_dir) / "profile"
        output_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)
        input_path.write_bytes(file_bytes)

        try:
            process = await asyncio.create_subprocess_exec(
                settings.libreoffice_binary,
                "--headless",
                "--nologo",
                "--nolockcheck",
                "--nodefault",
                "--nofirststartwizard",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output_dir),
                f"-env:UserInstallation=file://{profile_dir.as_posix()}",
                str(input_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ServiceError(
                status_code=500,
                error_code="OFFICE_CONVERSION_UNAVAILABLE",
                error_type="ConversionError",
                message="Office document conversion is not available in this environment.",
                technical_detail=f"Executable not found: {settings.libreoffice_binary}",
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=settings.render_timeout_ms / 1000,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise ServiceError(
                status_code=408,
                error_code="PDF_RENDER_TIMEOUT",
                error_type="TimeoutError",
                message="La generacion del PDF excedio el tiempo permitido.",
                technical_detail="Office conversion timeout exceeded.",
            ) from exc

        if process.returncode != 0:
            raise ServiceError(
                status_code=422,
                error_code="OFFICE_CONVERSION_FAILED",
                error_type="ConversionError",
                message="The Office document could not be converted to PDF.",
                technical_detail=(
                    (stderr or stdout or b"LibreOffice conversion failed.")
                    .decode("utf-8", errors="replace")
                    .strip()
                ),
            )

        pdf_path = output_dir / f"{input_path.stem}.pdf"
        if not pdf_path.exists():
            raise ServiceError(
                status_code=422,
                error_code="OFFICE_CONVERSION_FAILED",
                error_type="ConversionError",
                message="The Office document could not be converted to PDF.",
                technical_detail="LibreOffice finished without producing the expected PDF output file.",
            )

        return pdf_path.read_bytes()


def extract_email_content(file_bytes: bytes, file_name: str | None) -> ExtractedEmailContent:
    suffix = Path(file_name or "").suffix.lower()

    if not file_bytes:
        raise ServiceError(
            status_code=400,
            error_code="EMPTY_FILE",
            error_type="ValidationError",
            message="The email file received is empty.",
            technical_detail="Uploaded .eml or .msg file contained zero bytes.",
        )

    if suffix == ".eml":
        return extract_eml_content(file_bytes)
    if suffix == ".msg":
        return extract_msg_content(file_bytes)

    raise ServiceError(
        status_code=400,
        error_code="UNSUPPORTED_FILE_TYPE",
        error_type="ValidationError",
        message="The uploaded email file type is not supported.",
        technical_detail="Supported file types are .eml and .msg.",
        details={"receivedFileName": file_name},
    )


def extract_eml_content(file_bytes: bytes) -> ExtractedEmailContent:
    try:
        message = BytesParser(policy=policy.default).parsebytes(file_bytes)
    except Exception as exc:
        raise ServiceError(
            status_code=422,
            error_code="EML_PARSE_FAILED",
            error_type="ParsingError",
            message="No se pudo extraer el contenido del correo.",
            technical_detail="Python email parser failed to read the .eml file.",
        ) from exc

    html_body: str | None = None
    plain_body: str | None = None
    inline_resources: dict[str, dict[str, Any]] = {}
    attachments: list[EmailAttachmentSummary] = []

    preferred_html = message.get_body(preferencelist=("html",))
    if preferred_html is not None:
        html_body = preferred_html.get_content()

    preferred_plain = message.get_body(preferencelist=("plain",))
    if preferred_plain is not None:
        plain_body = preferred_plain.get_content()

    for part in message.walk():
        if part.is_multipart():
            continue

        content_type = part.get_content_type()
        content_id = normalize_content_id(part.get("Content-ID"))
        payload = part.get_payload(decode=True) or b""
        file_name = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()

        if file_name:
            attachments.append(
                EmailAttachmentSummary(
                    name=file_name,
                    mime_type=content_type,
                    size_bytes=len(payload),
                    is_inline=disposition == "inline" or bool(content_id),
                )
            )

        if content_id and payload:
            inline_resources[content_id] = {
                "data": payload,
                "mimeType": content_type,
            }

        if html_body is None and content_type == "text/html":
            html_body = part.get_content()
        elif plain_body is None and content_type == "text/plain":
            plain_body = part.get_content()

    has_html_body = bool(html_body)
    has_plain_text_body = bool(plain_body)

    if not html_body and plain_body:
        html_body = text_to_html_document(plain_body)

    if not html_body:
        raise ServiceError(
            status_code=422,
            error_code="EMAIL_BODY_NOT_FOUND",
            error_type="ParsingError",
            message="No se pudo extraer el contenido del correo.",
            technical_detail="No HTML body or plain text body could be extracted from the .eml file.",
        )

    final_html, cid_found_count, cid_resolved_count, unresolved_count = embed_inline_resources_with_stats(
        html_body,
        inline_resources,
    )
    warnings: list[str] = []
    if unresolved_count > 0:
        warnings.append("Una o mas imagenes inline CID no pudieron resolverse completamente.")

    return ExtractedEmailContent(
        html=final_html,
        subject=message.get("Subject"),
        sender=message.get("From"),
        to=message.get("To"),
        cc=message.get("Cc"),
        bcc=message.get("Bcc"),
        date=message.get("Date"),
        message_id=message.get("Message-ID"),
        source_format="eml",
        parser_used="email.parser",
        has_html_body=has_html_body,
        has_plain_text_body=has_plain_text_body,
        attachments=attachments,
        inline_image_count=len(inline_resources),
        cid_found_count=cid_found_count,
        cid_resolved_count=cid_resolved_count,
        unresolved_inline_count=unresolved_count,
        warnings=warnings,
    )


def extract_msg_content(file_bytes: bytes) -> ExtractedEmailContent:
    msg = None
    temp_msg_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".msg") as temp_msg_file:
            temp_msg_file.write(file_bytes)
            temp_msg_path = temp_msg_file.name

        try:
            msg = extract_msg.openMsg(temp_msg_path)
            html_body = decode_bytes_to_text(getattr(msg, "htmlBody", None))
            plain_body = getattr(msg, "body", None)
            inline_resources: dict[str, dict[str, Any]] = {}
            attachments: list[EmailAttachmentSummary] = []

            for attachment in getattr(msg, "attachments", []):
                content_id = normalize_content_id(
                    getattr(attachment, "cid", None) or getattr(attachment, "contentId", None)
                )
                attachment_data = getattr(attachment, "data", None)
                attachment_name = (
                    getattr(attachment, "name", None)
                    or getattr(attachment, "longFilename", None)
                    or getattr(attachment, "shortFilename", None)
                    or "attachment"
                )
                mime_type = getattr(attachment, "mimetype", None) or mimetypes.guess_type(attachment_name)[0]
                size_bytes = len(attachment_data) if isinstance(attachment_data, bytes) else None

                attachments.append(
                    EmailAttachmentSummary(
                        name=attachment_name,
                        mime_type=mime_type,
                        size_bytes=size_bytes,
                        is_inline=bool(content_id),
                    )
                )

                if not content_id or not isinstance(attachment_data, bytes):
                    continue

                inline_resources[content_id] = {
                    "data": attachment_data,
                    "mimeType": mime_type or "application/octet-stream",
                }

            has_html_body = bool(html_body)
            has_plain_text_body = bool(plain_body)

            if not html_body and plain_body:
                html_body = text_to_html_document(plain_body)

            if not html_body:
                raise ServiceError(
                    status_code=422,
                    error_code="EMAIL_BODY_NOT_FOUND",
                    error_type="ParsingError",
                    message="No se pudo extraer el contenido del correo.",
                    technical_detail="No HTML body or plain text body found.",
                )

            final_html, cid_found_count, cid_resolved_count, unresolved_count = embed_inline_resources_with_stats(
                html_body,
                inline_resources,
            )
            sender = (
                getattr(msg, "sender", None)
                or getattr(msg, "senderEmail", None)
                or getattr(msg, "sender_email", None)
            )
            to_value = getattr(msg, "to", None)
            cc_value = getattr(msg, "cc", None)
            date_value = decode_bytes_to_text(getattr(msg, "date", None)) or str(getattr(msg, "date", "") or "")
            warnings: list[str] = []
            if unresolved_count > 0:
                warnings.append("Una o mas imagenes inline CID no pudieron resolverse completamente.")

            return ExtractedEmailContent(
                html=final_html,
                subject=getattr(msg, "subject", None),
                sender=sender,
                to=to_value,
                cc=cc_value,
                bcc=None,
                date=date_value or None,
                message_id=getattr(msg, "messageId", None) or getattr(msg, "message_id", None),
                source_format="msg",
                parser_used="extract-msg",
                has_html_body=has_html_body,
                has_plain_text_body=has_plain_text_body,
                attachments=attachments,
                inline_image_count=len(inline_resources),
                cid_found_count=cid_found_count,
                cid_resolved_count=cid_resolved_count,
                unresolved_inline_count=unresolved_count,
                warnings=warnings,
            )
        finally:
            if temp_msg_path:
                try:
                    Path(temp_msg_path).unlink(missing_ok=True)
                except Exception:
                    pass
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError(
            status_code=422,
            error_code="MSG_PARSE_FAILED",
            error_type="ParsingError",
            message="No se pudo procesar el archivo .msg.",
            technical_detail="extract-msg failed with controlled error.",
        ) from exc
    finally:
        if msg is not None:
            try:
                msg.close()
            except Exception:
                pass


def extract_external_resource_urls(html: str) -> list[str]:
    patterns = [
        r"""(?:src|href|poster)\s*=\s*["'](https?://[^"' >]+)["']""",
        r"""url\(\s*["']?(https?://[^"' )]+)["']?\s*\)""",
    ]
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(re.findall(pattern, html, flags=re.IGNORECASE))
    unique_urls = sorted({url for url in matches if url.lower().startswith(("http://", "https://"))})
    return unique_urls


async def create_page(browser: Browser) -> tuple[BrowserContext, Page]:
    context = await browser.new_context(ignore_https_errors=False)
    page = await context.new_page()
    page.set_default_timeout(settings.render_timeout_ms)
    return context, page


async def render_pdf_document(html: str, file_name: str, request: Request) -> RenderedPdfResult:
    html = validate_html_payload(html)
    external_urls = extract_external_resource_urls(html)

    request.state.file_name = file_name
    request.state.html_size = len(html.encode("utf-8"))
    request.state.external_resource_count = len(external_urls)

    if browser_manager.browser is None:
        raise ServiceError(
            status_code=500,
            error_code="BROWSER_RENDER_ERROR",
            error_type="BrowserError",
            message="The browser engine failed while rendering the document.",
            technical_detail="Chromium browser instance is not available.",
        )

    context: BrowserContext | None = None
    page: Page | None = None
    failed_resources: list[dict[str, Any]] = []

    try:
        context, page = await create_page(browser_manager.browser)

        def on_request_failed(failed_request) -> None:
            failure_text = failed_request.failure or "Unknown request failure"
            failed_resources.append(
                {
                    "url": failed_request.url,
                    "method": failed_request.method,
                    "resourceType": failed_request.resource_type,
                    "errorText": failure_text,
                }
            )

        page.on("requestfailed", on_request_failed)

        try:
            await page.set_content(html, wait_until="networkidle", timeout=settings.render_timeout_ms)
            pdf_bytes = await asyncio.wait_for(
                page.pdf(
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "10mm",
                        "right": "10mm",
                        "bottom": "10mm",
                        "left": "10mm",
                    },
                ),
                timeout=settings.render_timeout_ms / 1000,
            )
        except (PlaywrightTimeoutError, asyncio.TimeoutError) as exc:
            raise ServiceError(
                status_code=408,
                error_code="PDF_RENDER_TIMEOUT",
                error_type="TimeoutError",
                message="The PDF rendering process exceeded the allowed timeout.",
                technical_detail=f"Chromium exceeded the configured timeout of {settings.render_timeout_ms}ms.",
            ) from exc
        except PlaywrightError as exc:
            raise ServiceError(
                status_code=500,
                error_code="BROWSER_RENDER_ERROR",
                error_type="BrowserError",
                message="The browser engine failed while rendering the document.",
                technical_detail=str(exc),
            ) from exc

        if failed_resources:
            request.state.failed_resource_count = len(failed_resources)
            failed_resource_details = {
                "failedResourceCount": len(failed_resources),
                "failedResources": failed_resources,
            }

            if settings.strict_external_resources:
                raise ServiceError(
                    status_code=422,
                    error_code="EXTERNAL_RESOURCE_LOAD_FAILED",
                    error_type="ExternalResourceError",
                    message="One or more external resources could not be loaded.",
                    technical_detail="Chromium reported network failures while loading external resources.",
                    details=failed_resource_details,
                )

            request.state.final_status = "SUCCESS_WITH_RESOURCE_WARNINGS"
            log_event(
                logging.WARNING,
                "external_resources_failed_nonfatal",
                traceId=request.state.trace_id,
                fileName=file_name,
                failedResourceCount=len(failed_resources),
                failedResources=failed_resources,
            )

        if not failed_resources:
            request.state.failed_resource_count = 0
            request.state.final_status = "SUCCESS"

        response_headers = {"Content-Disposition": f'attachment; filename="{file_name}"'}
        if failed_resources and not settings.strict_external_resources:
            response_headers["X-External-Resources-Status"] = "warning"
            response_headers["X-Failed-Resource-Count"] = str(len(failed_resources))

        return RenderedPdfResult(
            pdf_bytes=pdf_bytes,
            response_headers=response_headers,
            final_status=request.state.final_status,
            failed_resource_count=request.state.failed_resource_count,
        )
    finally:
        if page is not None:
            await page.close()
        if context is not None:
            await context.close()


async def render_pdf_response(html: str, file_name: str, request: Request) -> Response:
    rendered = await render_pdf_document(html, file_name, request)
    return Response(
        content=rendered.pdf_bytes,
        media_type="application/pdf",
        headers=rendered.response_headers,
    )


async def process_universal_item(
    item: dict[str, Any],
    *,
    index: int,
    request: Request,
) -> ProcessedItemResult:
    parse_start = time.perf_counter()
    metadata = normalize_item_metadata(item.get("metadata"))
    original_file_name = item.get("fileName") if isinstance(item.get("fileName"), str) else None
    content_mode = resolve_item_content_mode(item)
    detected_type = detect_document_type(item, content_mode)
    output_file_name = build_pdf_filename(original_file_name, original_file_name or detected_type)

    request.state.file_name = output_file_name

    if content_mode == "html":
        html_text = item.get("html")
        parse_time_ms = round((time.perf_counter() - parse_start) * 1000, 2)
        log_event(
            logging.INFO,
            "document_parsed",
            traceId=request.state.trace_id,
            index=index,
            originalFileName=original_file_name,
            detectedType=detected_type,
            parseTimeMs=parse_time_ms,
            metadata=metadata,
        )

        render_start = time.perf_counter()
        rendered = await render_pdf_document(str(html_text), output_file_name, request)
        render_time_ms = round((time.perf_counter() - render_start) * 1000, 2)

        log_event(
            logging.INFO,
            "document_rendered",
            traceId=request.state.trace_id,
            index=index,
            originalFileName=original_file_name,
            detectedType=detected_type,
            renderTimeMs=render_time_ms,
            finalStatus=rendered.final_status,
        )

        result_metadata = {"detectedType": detected_type, **metadata}
        return ProcessedItemResult(
            index=index,
            original_file_name=original_file_name or "document.html",
            output_file_name=output_file_name,
            detected_type=detected_type,
            pdf_bytes=rendered.pdf_bytes,
            metadata=result_metadata,
            response_headers=rendered.response_headers,
        )

    file_bytes = decode_base64_payload(item.get("contentBase64"))
    request.state.html_size = len(file_bytes)
    extension = detect_file_extension(original_file_name)

    if detected_type == "pdf":
        parse_time_ms = round((time.perf_counter() - parse_start) * 1000, 2)
        request.state.final_status = "SUCCESS"
        request.state.failed_resource_count = 0
        request.state.external_resource_count = 0
        log_event(
            logging.INFO,
            "document_parsed",
            traceId=request.state.trace_id,
            index=index,
            originalFileName=original_file_name,
            detectedType=detected_type,
            fileExtension=extension,
            fileSizeBytes=len(file_bytes),
            parseTimeMs=parse_time_ms,
            metadata=metadata,
        )
        return ProcessedItemResult(
            index=index,
            original_file_name=original_file_name or "document.pdf",
            output_file_name=output_file_name,
            detected_type=detected_type,
            pdf_bytes=file_bytes,
            metadata={"detectedType": detected_type, **metadata},
            response_headers={"Content-Disposition": f'attachment; filename="{output_file_name}"'},
        )

    if detected_type == "html":
        html_text = decode_bytes_to_text(file_bytes)
        parse_time_ms = round((time.perf_counter() - parse_start) * 1000, 2)
        log_event(
            logging.INFO,
            "document_parsed",
            traceId=request.state.trace_id,
            index=index,
            originalFileName=original_file_name,
            detectedType=detected_type,
            fileExtension=extension,
            fileSizeBytes=len(file_bytes),
            parseTimeMs=parse_time_ms,
            metadata=metadata,
        )

        render_start = time.perf_counter()
        rendered = await render_pdf_document(str(html_text), output_file_name, request)
        render_time_ms = round((time.perf_counter() - render_start) * 1000, 2)

        log_event(
            logging.INFO,
            "document_rendered",
            traceId=request.state.trace_id,
            index=index,
            originalFileName=original_file_name,
            detectedType=detected_type,
            renderTimeMs=render_time_ms,
            finalStatus=rendered.final_status,
        )

        return ProcessedItemResult(
            index=index,
            original_file_name=original_file_name or "document.html",
            output_file_name=output_file_name,
            detected_type=detected_type,
            pdf_bytes=rendered.pdf_bytes,
            metadata={"detectedType": detected_type, **metadata},
            response_headers=rendered.response_headers,
        )

    if detected_type in {"eml", "msg"}:
        normalized_file_bytes, double_base64_detected = maybe_double_decode_email_file(file_bytes, extension)
        if double_base64_detected:
            log_event(
                logging.WARNING,
                "DOUBLE_BASE64_DETECTED",
                traceId=request.state.trace_id,
                fileName=original_file_name,
                detectedType=detected_type,
            )

        extracted_email = extract_email_content(normalized_file_bytes, original_file_name)
        preview_html = build_email_preview_html(
            original_file_name=original_file_name or f"correo{extension}",
            ticket_id=str(metadata.get("ticketId")) if metadata.get("ticketId") is not None else None,
            extracted_email=extracted_email,
        )

        parse_time_ms = round((time.perf_counter() - parse_start) * 1000, 2)
        log_event(
            logging.INFO,
            "document_parsed",
            traceId=request.state.trace_id,
            index=index,
            originalFileName=original_file_name,
            detectedType=detected_type,
            fileExtension=extension,
            fileSizeBytes=len(normalized_file_bytes),
            parserUsed=extracted_email.parser_used,
            hasHtmlBody=extracted_email.has_html_body,
            hasPlainTextBody=extracted_email.has_plain_text_body,
            extractedSubject=extracted_email.subject,
            extractedFrom=extracted_email.sender,
            internalAttachmentCount=len(extracted_email.attachments),
            inlineImageCount=extracted_email.inline_image_count,
            cidFoundCount=extracted_email.cid_found_count,
            cidResolvedCount=extracted_email.cid_resolved_count,
            unresolvedInlineImageCount=extracted_email.unresolved_inline_count,
            doubleBase64Detected=double_base64_detected,
            warnings=extracted_email.warnings or [],
            parseTimeMs=parse_time_ms,
            metadata=metadata,
        )

        if extracted_email.unresolved_inline_count > 0:
            log_event(
                logging.WARNING,
                "email_inline_image_warning",
                traceId=request.state.trace_id,
                index=index,
                originalFileName=original_file_name,
                detectedType=detected_type,
                parserUsed=extracted_email.parser_used,
                warningCode="EMAIL_INLINE_IMAGE_WARNING",
                inlineImageCount=extracted_email.inline_image_count,
                cidFoundCount=extracted_email.cid_found_count,
                cidResolvedCount=extracted_email.cid_resolved_count,
                unresolvedInlineImageCount=extracted_email.unresolved_inline_count,
                message="One or more inline email images could not be resolved.",
            )

        render_start = time.perf_counter()
        try:
            rendered = await render_pdf_document(preview_html, output_file_name, request)
        except ServiceError as exc:
            if exc.error_code == "PDF_RENDER_TIMEOUT":
                raise
            raise ServiceError(
                status_code=500,
                error_code="EMAIL_RENDER_FAILED",
                error_type="RenderError",
                message="No se pudo convertir el correo adjunto a PDF.",
                technical_detail="The parsed email content could not be rendered to PDF.",
            ) from exc
        render_time_ms = round((time.perf_counter() - render_start) * 1000, 2)

        log_event(
            logging.INFO,
            "document_rendered",
            traceId=request.state.trace_id,
            index=index,
            originalFileName=original_file_name,
            detectedType=detected_type,
            renderTimeMs=render_time_ms,
            finalStatus=rendered.final_status,
        )

        result_metadata = {
            "detectedType": detected_type,
            **metadata,
            "subject": extracted_email.subject,
            "from": extracted_email.sender,
            "to": extracted_email.to,
            "cc": extracted_email.cc,
            "bcc": extracted_email.bcc,
            "date": extracted_email.date,
            "messageId": extracted_email.message_id,
        }
        return ProcessedItemResult(
            index=index,
            original_file_name=original_file_name or f"correo{extension}",
            output_file_name=output_file_name,
            detected_type=detected_type,
            pdf_bytes=rendered.pdf_bytes,
            metadata=result_metadata,
            response_headers=rendered.response_headers,
        )

    if detected_type == "txt":
        parse_time_ms = round((time.perf_counter() - parse_start) * 1000, 2)
        log_event(
            logging.INFO,
            "document_parsed",
            traceId=request.state.trace_id,
            index=index,
            originalFileName=original_file_name,
            detectedType=detected_type,
            fileExtension=extension,
            fileSizeBytes=len(file_bytes),
            parseTimeMs=parse_time_ms,
            metadata=metadata,
        )

        render_start = time.perf_counter()
        rendered = await render_pdf_document(
            text_to_html_document(decode_bytes_to_text(file_bytes) or ""),
            output_file_name,
            request,
        )
        render_time_ms = round((time.perf_counter() - render_start) * 1000, 2)
        log_event(
            logging.INFO,
            "document_rendered",
            traceId=request.state.trace_id,
            index=index,
            originalFileName=original_file_name,
            detectedType=detected_type,
            renderTimeMs=render_time_ms,
            finalStatus=rendered.final_status,
        )
        return ProcessedItemResult(
            index=index,
            original_file_name=original_file_name or "document.txt",
            output_file_name=output_file_name,
            detected_type=detected_type,
            pdf_bytes=rendered.pdf_bytes,
            metadata={"detectedType": detected_type, **metadata},
            response_headers=rendered.response_headers,
        )

    parse_time_ms = round((time.perf_counter() - parse_start) * 1000, 2)
    log_event(
        logging.INFO,
        "document_parsed",
        traceId=request.state.trace_id,
        index=index,
        originalFileName=original_file_name,
        detectedType=detected_type,
        fileExtension=extension,
        fileSizeBytes=len(file_bytes),
        parseTimeMs=parse_time_ms,
        metadata=metadata,
    )

    render_start = time.perf_counter()
    pdf_bytes = await convert_office_bytes_to_pdf(file_bytes, original_file_name or f"document{extension}")
    render_time_ms = round((time.perf_counter() - render_start) * 1000, 2)
    request.state.final_status = "SUCCESS"
    request.state.failed_resource_count = 0
    request.state.external_resource_count = 0

    log_event(
        logging.INFO,
        "document_rendered",
        traceId=request.state.trace_id,
        index=index,
        originalFileName=original_file_name,
        detectedType=detected_type,
        renderTimeMs=render_time_ms,
        finalStatus="SUCCESS",
    )

    return ProcessedItemResult(
        index=index,
        original_file_name=original_file_name or f"document{extension}",
        output_file_name=output_file_name,
        detected_type=detected_type,
        pdf_bytes=pdf_bytes,
        metadata={"detectedType": detected_type, **metadata},
        response_headers={"Content-Disposition": f'attachment; filename="{output_file_name}"'},
    )


def build_batch_failure_result(
    *,
    index: int,
    original_file_name: str,
    output_file_name: str,
    exc: ServiceError,
) -> dict[str, Any]:
    return {
        "index": index,
        "originalFileName": original_file_name,
        "outputFileName": output_file_name,
        "status": "failed",
        "errorCode": exc.error_code,
        "message": exc.message,
        "technicalDetail": exc.technical_detail,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    await browser_manager.start()
    yield
    await browser_manager.stop()


app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    request.state.trace_id = trace_id
    request.state.file_name = DEFAULT_FILE_NAME
    request.state.html_size = 0
    request.state.external_resource_count = 0
    request.state.failed_resource_count = 0
    request.state.final_status = "started"

    log_event(
        logging.INFO,
        "request_started",
        traceId=trace_id,
        method=request.method,
        path=request.url.path,
        client=request.client.host if request.client else None,
    )

    try:
        response = await call_next(request)
    except Exception:
        processing_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
        log_event(
            logging.ERROR,
            "request_failed_unhandled",
            traceId=trace_id,
            method=request.method,
            path=request.url.path,
            processingTimeMs=processing_time_ms,
            htmlSizeBytes=request.state.html_size,
            fileName=request.state.file_name,
            externalResourceCount=request.state.external_resource_count,
            failedResourceCount=request.state.failed_resource_count,
            finalStatus="UNHANDLED_EXCEPTION",
        )
        raise

    processing_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
    response.headers["X-Trace-Id"] = trace_id

    if request.state.final_status == "started":
        request.state.final_status = "SUCCESS" if response.status_code < 400 else f"HTTP_{response.status_code}"

    log_event(
        logging.INFO,
        "request_completed",
        traceId=trace_id,
        method=request.method,
        path=request.url.path,
        statusCode=response.status_code,
        processingTimeMs=processing_time_ms,
        htmlSizeBytes=request.state.html_size,
        fileName=request.state.file_name,
        externalResourceCount=request.state.external_resource_count,
        failedResourceCount=request.state.failed_resource_count,
        finalStatus=request.state.final_status,
    )

    return response


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    request.state.final_status = exc.error_code

    payload = build_error_payload(
        trace_id=request.state.trace_id,
        error_code=exc.error_code,
        error_type=exc.error_type,
        message=exc.message,
        technical_detail=exc.technical_detail,
        details=exc.details,
    )

    log_event(
        logging.WARNING if exc.status_code < 500 else logging.ERROR,
        "service_error",
        traceId=request.state.trace_id,
        errorCode=exc.error_code,
        errorType=exc.error_type,
        statusCode=exc.status_code,
        technicalDetail=exc.technical_detail,
        details=exc.details,
    )
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request.state.final_status = "INVALID_REQUEST_BODY"

    payload = build_error_payload(
        trace_id=request.state.trace_id,
        error_code="INVALID_REQUEST_BODY",
        error_type="ValidationError",
        message="The request body could not be processed.",
        technical_detail="The JSON body is missing required fields or contains invalid types.",
        details={"validationErrors": exc.errors()},
    )

    log_event(
        logging.WARNING,
        "request_validation_error",
        traceId=request.state.trace_id,
        statusCode=400,
        details={"validationErrors": exc.errors()},
    )
    return JSONResponse(status_code=400, content=payload)


@app.exception_handler(Exception)
async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request.state.final_status = "UNEXPECTED_SERVER_ERROR"
    logger.exception(
        json.dumps(
            {
                "timestamp": utc_timestamp(),
                "service": SERVICE_NAME,
                "event": "unexpected_exception",
                "traceId": request.state.trace_id,
                "message": "Unhandled exception while generating PDF.",
                "exceptionType": type(exc).__name__,
            },
            ensure_ascii=True,
        )
    )

    payload = build_error_payload(
        trace_id=request.state.trace_id,
        error_code="UNEXPECTED_SERVER_ERROR",
        error_type="InternalServerError",
        message="An unexpected server error occurred while generating the PDF.",
        technical_detail="An unhandled exception occurred. Check server logs with the provided traceId.",
    )
    return JSONResponse(status_code=500, content=payload)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": SERVICE_NAME,
        "status": "ok",
        "version": SERVICE_VERSION,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    browser_status = "connected" if browser_manager.browser is not None else "disconnected"
    return {
        "status": "healthy",
        "browser": browser_status,
        "environment": "render",
    }


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.post("/convert/html-to-pdf")
async def convert_html_to_pdf(payload: ConvertHtmlRequest, request: Request) -> Response:
    file_name = build_pdf_filename(payload.fileName)
    return await render_pdf_response(payload.html, file_name, request)


@app.post("/convert/to-pdf", response_model=None)
async def convert_to_pdf(request: Request, payload: dict[str, Any] = Body(...)) -> Response | JSONResponse:
    items = normalize_universal_items(payload)

    if len(items) == 1:
        result = await process_universal_item(items[0], index=0, request=request)
        return Response(
            content=result.pdf_bytes,
            media_type="application/pdf",
            headers=result.response_headers,
        )

    request.state.file_name = f"batch-{len(items)}-items"
    results: list[dict[str, Any]] = []
    success_count = 0

    for index, item in enumerate(items):
        original_file_name = (
            item.get("fileName") if isinstance(item, dict) and isinstance(item.get("fileName"), str) else f"item-{index + 1}"
        )
        output_file_name = build_pdf_filename(original_file_name, original_file_name)

        try:
            processed = await process_universal_item(item, index=index, request=request)
            success_count += 1
            results.append(
                {
                    "index": index,
                    "originalFileName": processed.original_file_name,
                    "outputFileName": processed.output_file_name,
                    "status": "success",
                    "contentType": "application/pdf",
                    "contentBase64": base64.b64encode(processed.pdf_bytes).decode("ascii"),
                    "metadata": processed.metadata,
                }
            )
        except ServiceError as exc:
            log_event(
                logging.WARNING if exc.status_code < 500 else logging.ERROR,
                "batch_item_failed",
                traceId=request.state.trace_id,
                index=index,
                originalFileName=original_file_name,
                errorCode=exc.error_code,
                errorType=exc.error_type,
                technicalDetail=exc.technical_detail,
            )
            results.append(
                build_batch_failure_result(
                    index=index,
                    original_file_name=original_file_name,
                    output_file_name=output_file_name,
                    exc=exc,
                )
            )
        except Exception as exc:
            logger.exception(
                json.dumps(
                    {
                        "timestamp": utc_timestamp(),
                        "service": SERVICE_NAME,
                        "event": "batch_item_unexpected_exception",
                        "traceId": request.state.trace_id,
                        "index": index,
                        "originalFileName": original_file_name,
                        "exceptionType": type(exc).__name__,
                    },
                    ensure_ascii=True,
                )
            )
            results.append(
                {
                    "index": index,
                    "originalFileName": original_file_name,
                    "outputFileName": output_file_name,
                    "status": "failed",
                    "errorCode": "UNEXPECTED_SERVER_ERROR",
                    "message": "Ocurrio un error inesperado procesando el documento.",
                    "technicalDetail": "Controlled generic detail only.",
                }
            )

    if success_count == len(items):
        global_success: bool | str = True
        request.state.final_status = "SUCCESS"
    elif success_count == 0:
        global_success = False
        request.state.final_status = "BATCH_ALL_FAILED"
    else:
        global_success = "partial"
        request.state.final_status = "BATCH_PARTIAL_SUCCESS"

    request.state.file_name = f"batch-{len(items)}-items"
    return JSONResponse(
        status_code=200,
        content={
            "success": global_success,
            "traceId": request.state.trace_id,
            "results": results,
        },
    )


@app.post("/convert/email-to-pdf")
async def convert_email_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    fileName: str | None = Form(default=None),
    metadata: str | None = Form(default=None),
) -> Response:
    uploaded_file_name = file.filename or "email-message"
    request.state.file_name = build_pdf_filename(fileName, uploaded_file_name)

    metadata_payload: dict[str, Any] = {}
    if metadata:
        try:
            parsed_metadata = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise ServiceError(
                status_code=400,
                error_code="INVALID_METADATA",
                error_type="ValidationError",
                message="The metadata payload could not be processed.",
                technical_detail=f"metadata form field must be valid JSON: {exc}",
            ) from exc

        if not isinstance(parsed_metadata, dict):
            raise ServiceError(
                status_code=400,
                error_code="INVALID_METADATA",
                error_type="ValidationError",
                message="The metadata payload could not be processed.",
                technical_detail="metadata form field must be a JSON object.",
            )
        metadata_payload = parsed_metadata

    try:
        file_bytes = await file.read()
    finally:
        await file.close()

    extracted_email = extract_email_content(file_bytes, uploaded_file_name)

    log_event(
        logging.INFO,
        "email_file_extracted",
        traceId=request.state.trace_id,
        uploadedFileName=uploaded_file_name,
        uploadedFileSizeBytes=len(file_bytes),
        sourceFormat=extracted_email.source_format,
        extractedSubject=extracted_email.subject,
        metadata=metadata_payload,
    )

    preview_html = build_email_preview_html(
        original_file_name=uploaded_file_name,
        ticket_id=str(metadata_payload.get("ticketId")) if metadata_payload.get("ticketId") is not None else None,
        extracted_email=extracted_email,
    )

    return await render_pdf_response(
        preview_html,
        request.state.file_name,
        request,
    )
