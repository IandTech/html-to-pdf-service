import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from fastapi import FastAPI, Request, Response
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


class Settings:
    def __init__(self) -> None:
        self.render_timeout_ms = int(os.getenv("RENDER_TIMEOUT_MS", "30000"))
        self.log_level = os.getenv("LOG_LEVEL", "INFO").upper()


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


@app.post("/convert/html-to-pdf")
async def convert_html_to_pdf(payload: ConvertHtmlRequest, request: Request) -> Response:
    html = validate_html_payload(payload.html)
    file_name = sanitize_filename(payload.fileName)
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
            failure = failed_request.failure
            failed_resources.append(
                {
                    "url": failed_request.url,
                    "method": failed_request.method,
                    "resourceType": failed_request.resource_type,
                    "errorText": failure["errorText"] if failure else "Unknown request failure",
                }
            )

        page.on("requestfailed", on_request_failed)

        try:
            await page.set_content(html, wait_until="networkidle", timeout=settings.render_timeout_ms)
            pdf_bytes = await page.pdf(
                format="A4",
                print_background=True,
                margin={
                    "top": "10mm",
                    "right": "10mm",
                    "bottom": "10mm",
                    "left": "10mm",
                },
                timeout=settings.render_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
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
            raise ServiceError(
                status_code=422,
                error_code="EXTERNAL_RESOURCE_LOAD_FAILED",
                error_type="ExternalResourceError",
                message="One or more external resources could not be loaded.",
                technical_detail="Chromium reported network failures while loading external resources.",
                details={
                    "failedResourceCount": len(failed_resources),
                    "failedResources": failed_resources,
                },
            )

        request.state.failed_resource_count = 0
        request.state.final_status = "SUCCESS"

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
        )
    finally:
        if page is not None:
            await page.close()
        if context is not None:
            await context.close()
