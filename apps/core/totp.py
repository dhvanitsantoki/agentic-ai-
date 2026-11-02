"""Time-based one-time passwords (RFC 6238, HMAC-SHA1, 6 digits, 30 s) for two-step login.

Compatible with Google Authenticator, Microsoft Authenticator, Authy, Zoho OneAuth, etc.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    clean = secret.replace(" ", "").upper()
    return base64.b32decode(clean + "=" * (-len(clean) % 8))


def hotp(secret: str, counter: int, digits: int = DIGITS) -> str:
    mac = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def current_step(at: float | None = None) -> int:
    return int((time.time() if at is None else at) // STEP_SECONDS)


def verify(secret: str, code: str, *, at: float | None = None, window: int = 1, last_step: int | None = None) -> int | None:
    """Return the matching time step, or None. Steps at or before `last_step` are rejected (no replay)."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) != DIGITS:
        return None
    now = current_step(at)
    for step in range(now - window, now + window + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(hotp(secret, step), digits):
            return step
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"


def qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=7, border=2)
    return img.to_string(encoding="unicode")
