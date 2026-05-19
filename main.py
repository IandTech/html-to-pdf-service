import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
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
from fastapi import FastAPI, File, Form, Request, Response, UploadFile
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


SERVICE_NAME = "html-to-pdf-service"
SERVICE_VERSION = "1.0.0"
DEFAULT_FILE_NAME = "document.pdf"


def env_to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.render_timeout_ms = int(os.getenv("RENDER_TIMEOUT_MS", "30000"))
        self.log_level = os.getenv("LOG_LEVEL", "INFO").upper()
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
class ExtractedEmailContent:
    html: str
    subject: str | None
    source_format: str


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


def embed_inline_resources(html: str, inline_resources: dict[str, dict[str, Any]]) -> str:
    if not inline_resources:
        return html

    def replace_cid(match: re.Match[str]) -> str:
        content_id = normalize_content_id(match.group(1))
        resource = inline_resources.get(content_id or "")
        if not resource:
            return match.group(0)
        return build_data_url(resource["data"], resource.get("mimeType"))

    return re.sub(r"cid:([^\"' >)]+)", replace_cid, html, flags=re.IGNORECASE)


def text_to_html_document(text: str) -> str:
    escaped_text = html_escape(text or "")
    body_html = escaped_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return (
        "<html><body style=\"font-family: Arial, sans-serif; white-space: normal;\">"
        f"{body_html}"
        "</body></html>"
    )


def extract_email_content(file_bytes: bytes, file_name: str | None) -> ExtractedEmailContent:
    suffix = Path(file_name or "").suffix.lower()

    if not file_bytes:
        raise ServiceError(
            status_code=400,
            error_code="EMAIL_FILE_EMPTY",
            error_type="ValidationError",
            message="The email file received is empty.",
            technical_detail="Uploaded .eml or .msg file contained zero bytes.",
        )

    if suffix == ".eml":
        return extract_eml_content(file_bytes)
    if suffix == ".msg":
        return extract_msg_content(file_bytes)

    raise ServiceError(
        status_code=415,
        error_code="UNSUPPORTED_EMAIL_FILE_TYPE",
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
            error_code="EMAIL_CONTENT_EXTRACTION_FAILED",
            error_type="EmailParsingError",
            message="The email file could not be processed.",
            technical_detail=f"Python email parser failed to read the .eml file: {exc}",
        ) from exc

    html_body: str | None = None
    plain_body: str | None = None
    inline_resources: dict[str, dict[str, Any]] = {}

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

        if content_id and payload:
            inline_resources[content_id] = {
                "data": payload,
                "mimeType": content_type,
            }

        if html_body is None and content_type == "text/html":
            html_body = part.get_content()
        elif plain_body is None and content_type == "text/plain":
            plain_body = part.get_content()

    if not html_body and plain_body:
        html_body = text_to_html_document(plain_body)

    if not html_body:
        raise ServiceError(
            status_code=422,
            error_code="EMAIL_BODY_NOT_FOUND",
            error_type="EmailParsingError",
            message="The email file does not contain renderable content.",
            technical_detail="No HTML body or plain text body could be extracted from the .eml file.",
        )

    return ExtractedEmailContent(
        html=embed_inline_resources(html_body, inline_resources),
        subject=message.get("Subject"),
        source_format="eml",
    )


def extract_msg_content(file_bytes: bytes) -> ExtractedEmailContent:
    msg = None
    try:
        msg = extract_msg.Message(file_bytes)
        html_body = decode_bytes_to_text(getattr(msg, "htmlBody", None))
        plain_body = getattr(msg, "body", None)
        inline_resources: dict[str, dict[str, Any]] = {}

        for attachment in getattr(msg, "attachments", []):
            content_id = normalize_content_id(
                getattr(attachment, "cid", None) or getattr(attachment, "contentId", None)
            )
            attachment_data = getattr(attachment, "data", None)

            if not content_id or not isinstance(attachment_data, bytes):
                continue

            attachment_name = (
                getattr(attachment, "name", None)
                or getattr(attachment, "longFilename", None)
                or getattr(attachment, "shortFilename", None)
                or ""
            )
            mime_type = getattr(attachment, "mimetype", None) or mimetypes.guess_type(attachment_name)[0]
            inline_resources[content_id] = {
                "data": attachment_data,
                "mimeType": mime_type or "application/octet-stream",
            }

        if not html_body and plain_body:
            html_body = text_to_html_document(plain_body)

        if not html_body:
            raise ServiceError(
                status_code=422,
                error_code="EMAIL_BODY_NOT_FOUND",
                error_type="EmailParsingError",
                message="The email file does not contain renderable content.",
                technical_detail="No HTML body or plain text body could be extracted from the .msg file.",
            )

        return ExtractedEmailContent(
            html=embed_inline_resources(html_body, inline_resources),
            subject=getattr(msg, "subject", None),
            source_format="msg",
        )
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError(
            status_code=422,
            error_code="EMAIL_CONTENT_EXTRACTION_FAILED",
            error_type="EmailParsingError",
            message="The email file could not be processed.",
            technical_detail=f"extract-msg failed to read the .msg file: {exc}",
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


async def render_pdf_response(html: str, file_name: str, request: Request) -> Response:
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

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers=response_headers,
        )
    finally:
        if page is not None:
            await page.close()
        if context is not None:
            await context.close()


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

    return await render_pdf_response(
        extracted_email.html,
        request.state.file_name,
        request,
    )
