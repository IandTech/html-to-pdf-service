import base64
import re


EMAIL_MIME_MARKERS = (
    "received:",
    "mime-version:",
    "content-type:",
    "subject:",
    "from:",
    "to:",
)

MSG_COMPOUND_FILE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def looks_like_base64_text(payload: bytes) -> bool:
    if not payload:
        return False

    stripped = b"".join(payload.split())
    if len(stripped) < 16 or len(stripped) % 4 != 0:
        return False

    return re.fullmatch(rb"[A-Za-z0-9+/=]+", stripped) is not None


def looks_like_email_mime(payload: bytes) -> bool:
    if not payload:
        return False

    decoded_text = payload.decode("utf-8", errors="ignore").lower()
    marker_hits = sum(1 for marker in EMAIL_MIME_MARKERS if marker in decoded_text)
    return marker_hits >= 2


def looks_like_msg_binary(payload: bytes) -> bool:
    return payload.startswith(MSG_COMPOUND_FILE_HEADER)


def maybe_double_decode_email_file(file_bytes: bytes, extension: str) -> tuple[bytes, bool]:
    normalized_extension = extension.lower()

    if normalized_extension == ".eml":
        if looks_like_email_mime(file_bytes):
            return file_bytes, False
        if not looks_like_base64_text(file_bytes):
            return file_bytes, False

        try:
            second_decode = base64.b64decode(b"".join(file_bytes.split()), validate=True)
        except Exception:
            return file_bytes, False
        if looks_like_email_mime(second_decode):
            return second_decode, True
        return file_bytes, False

    if normalized_extension == ".msg":
        if looks_like_msg_binary(file_bytes):
            return file_bytes, False
        if not looks_like_base64_text(file_bytes):
            return file_bytes, False

        try:
            second_decode = base64.b64decode(b"".join(file_bytes.split()), validate=True)
        except Exception:
            return file_bytes, False
        if looks_like_msg_binary(second_decode):
            return second_decode, True
        return file_bytes, False

    return file_bytes, False
