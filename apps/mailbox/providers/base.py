"""Provider-neutral message types and the adapter interface (Gmail, Microsoft Graph, Zoho Mail)."""
import email.utils
import logging
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)
TIMEOUT = 30


class ProviderError(Exception):
    pass


class AuthError(ProviderError):
    """Token revoked/invalid - the mailbox must be reconnected."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str = ""
    expires_in: int = 3600
    scope: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Identity:
    email: str
    display_name: str = ""
    account_id: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Attachment:
    filename: str
    content: bytes
    mimetype: str = ""

    def __post_init__(self):
        if not self.mimetype:
            self.mimetype = mimetypes.guess_type(self.filename)[0] or "application/octet-stream"


@dataclass
class OutgoingMessage:
    from_email: str
    from_name: str
    to: list
    subject: str
    body_text: str
    cc: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    thread_id: str = ""
    reply_to_provider_id: str = ""
    reply_to_folder_id: str = ""


@dataclass
class SentResult:
    provider_message_id: str = ""
    thread_id: str = ""
    rfc_message_id: str = ""


@dataclass
class IncomingMessage:
    provider_message_id: str
    from_email: str
    subject: str
    body_text: str
    received_at: datetime
    from_name: str = ""
    to: list = field(default_factory=list)
    cc: list = field(default_factory=list)
    thread_id: str = ""
    folder_id: str = ""
    rfc_message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    headers: dict = field(default_factory=dict)
    attachment_names: list = field(default_factory=list)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all(["br"]):
        br.replace_with("\n")
    for p in soup.find_all(["p", "div", "tr", "li"]):
        p.insert_after("\n")
    text = soup.get_text()
    lines = [line.rstrip() for line in text.splitlines()]
    out, blank = [], 0
    for line in lines:
        blank = blank + 1 if not line.strip() else 0
        if blank <= 1:
            out.append(line)
    return "\n".join(out).strip()


def parse_address(value: str) -> tuple[str, str]:
    name, addr = email.utils.parseaddr(value or "")
    return name, addr.lower()


def parse_address_list(value: str) -> list[str]:
    return [a.lower() for _, a in email.utils.getaddresses([value or ""]) if a]


def build_mime(msg: OutgoingMessage) -> bytes:
    m = EmailMessage()
    m["From"] = email.utils.formataddr((msg.from_name, msg.from_email)) if msg.from_name else msg.from_email
    m["To"] = ", ".join(msg.to)
    if msg.cc:
        m["Cc"] = ", ".join(msg.cc)
    m["Subject"] = msg.subject
    m["Date"] = email.utils.formatdate(localtime=True)
    m["Message-ID"] = msg.message_id or email.utils.make_msgid(domain=msg.from_email.split("@")[-1])
    if msg.in_reply_to:
        m["In-Reply-To"] = msg.in_reply_to
        m["References"] = (msg.references + " " + msg.in_reply_to).strip()
    for k, v in msg.headers.items():
        m[k] = v
    m.set_content(msg.body_text)
    for a in msg.attachments:
        maintype, _, subtype = a.mimetype.partition("/")
        m.add_attachment(a.content, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=a.filename)
    return m.as_bytes()


class BaseProvider:
    name = ""
    label = ""

    def is_configured(self) -> bool:
        raise NotImplementedError

    def redirect_uri(self) -> str:
        return f"{settings.SITE_URL}/mailbox/oauth/callback/{self.name}/"

    def authorize_url(self, state: str) -> str:
        raise NotImplementedError

    def exchange_code(self, code: str) -> TokenSet:
        raise NotImplementedError

    def refresh(self, refresh_token: str) -> TokenSet:
        raise NotImplementedError

    def identity(self, tokens: TokenSet) -> Identity:
        raise NotImplementedError

    def send(self, account, msg: OutgoingMessage) -> SentResult:
        raise NotImplementedError

    def fetch_new(self, account, cursor: str) -> tuple[list[IncomingMessage], str]:
        raise NotImplementedError

    # ---- helpers ----------------------------------------------------------------------
    def access_token(self, account) -> str:
        if account.token_is_expired:
            if not account.refresh_token:
                raise AuthError("Access token expired and no refresh token stored - reconnect the mailbox.")
            tokens = self.refresh(account.refresh_token)
            account.access_token = tokens.access_token
            if tokens.refresh_token:
                account.refresh_token = tokens.refresh_token
            account.token_expires_at = timezone.now() + timezone.timedelta(seconds=tokens.expires_in or 3600)
            account.save(update_fields=["_access_token", "_refresh_token", "token_expires_at"])
        return account.access_token

    def _request(self, method, url, *, token=None, auth_scheme="Bearer", expect_json=True, **kwargs):
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = f"{auth_scheme} {token}"
        try:
            resp = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise ProviderError(f"{self.label}: network error {exc.__class__.__name__}") from exc
        if resp.status_code in (401, 403):
            raise AuthError(f"{self.label}: authorisation failed ({resp.status_code}): {resp.text[:300]}")
        if resp.status_code >= 400:
            raise ProviderError(f"{self.label}: HTTP {resp.status_code}: {resp.text[:500]}")
        if not expect_json or resp.status_code == 204 or not resp.content:
            return resp
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.label}: invalid JSON response") from exc


def get_provider(name: str) -> BaseProvider:
    from .gmail import GmailProvider
    from .outlook import OutlookProvider
    from .zoho import ZohoProvider

    providers = {"gmail": GmailProvider, "outlook": OutlookProvider, "zoho": ZohoProvider}
    if name not in providers:
        raise ProviderError(f"Unknown provider {name}")
    return providers[name]()
