import base64
import unittest

from services.base64_helper import (
    looks_like_base64_text,
    looks_like_email_mime,
    looks_like_msg_binary,
    maybe_double_decode_email_file,
)


class Base64HelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.eml_bytes = (
            b"From: sender@example.com\r\n"
            b"To: receiver@example.com\r\n"
            b"Subject: Test\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Hello world.\r\n"
        )
        self.msg_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"mock-msg-binary"

    def test_looks_like_base64_text(self) -> None:
        encoded = base64.b64encode(self.eml_bytes)
        self.assertTrue(looks_like_base64_text(encoded))
        self.assertFalse(looks_like_base64_text(self.eml_bytes))

    def test_looks_like_email_mime(self) -> None:
        self.assertTrue(looks_like_email_mime(self.eml_bytes))
        self.assertFalse(looks_like_email_mime(b"plain random text"))

    def test_looks_like_msg_binary(self) -> None:
        self.assertTrue(looks_like_msg_binary(self.msg_bytes))
        self.assertFalse(looks_like_msg_binary(self.eml_bytes))

    def test_eml_normal_is_not_double_decoded(self) -> None:
        output_bytes, detected = maybe_double_decode_email_file(self.eml_bytes, ".eml")
        self.assertFalse(detected)
        self.assertEqual(output_bytes, self.eml_bytes)

    def test_eml_double_base64_is_decoded(self) -> None:
        once_encoded = base64.b64encode(self.eml_bytes)
        output_bytes, detected = maybe_double_decode_email_file(once_encoded, ".eml")
        self.assertTrue(detected)
        self.assertEqual(output_bytes, self.eml_bytes)

    def test_msg_normal_is_not_double_decoded(self) -> None:
        output_bytes, detected = maybe_double_decode_email_file(self.msg_bytes, ".msg")
        self.assertFalse(detected)
        self.assertEqual(output_bytes, self.msg_bytes)

    def test_msg_double_base64_is_decoded(self) -> None:
        once_encoded = base64.b64encode(self.msg_bytes)
        output_bytes, detected = maybe_double_decode_email_file(once_encoded, ".msg")
        self.assertTrue(detected)
        self.assertEqual(output_bytes, self.msg_bytes)

    def test_pdf_is_not_double_decoded(self) -> None:
        pdf_bytes = b"%PDF-1.7 mock"
        output_bytes, detected = maybe_double_decode_email_file(pdf_bytes, ".pdf")
        self.assertFalse(detected)
        self.assertEqual(output_bytes, pdf_bytes)


if __name__ == "__main__":
    unittest.main()
