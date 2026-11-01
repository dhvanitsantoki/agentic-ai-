"""Gmail / Google Workspace via the Gmail REST API (OAuth 2.0, scope gmail.modify)."""
import base64
from datetime import datetime, timezone as dt_tz
from urllib.parse import urlencode

from django.conf import settings

from .base import (
    AuthError, BaseProvider, Identity, IncomingMessage, OutgoingMessage, ProviderError, SentResult, TokenSet, build_mime,
    html_to_text, parse_address, parse_address_list,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = "openid email https://www.googleapis.com/auth/gmail.modify"


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class GmailProvider(BaseProvider):
    name = "gmail"
    label = "Gmail"

    def is_configured(self):
        return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)

    def authorize_url(self, state):
        return AUTH_URL + "?" + urlencode({
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID, "redirect_uri": self.redirect_uri(), "response_type": "code",
            "scope": SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state,
        })

    def _token(self, data):
        payload = self._request("POST", TOKEN_URL, data={
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID, "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET, **data,
        })
        if "access_token" not in payload:
            raise AuthError(f"Gmail token error: {payload.get('error_description') or payload}")
        return TokenSet(payload["access_token"], payload.get("refresh_token", ""), int(payload.get("expires_in", 3600)),
                        payload.get("scope", ""))

    def exchange_code(self, code):
        return self._token({"code": code, "redirect_uri": self.redirect_uri(), "grant_type": "authorization_code"})

    def refresh(self, refresh_token):
        return self._token({"refresh_token": refresh_token, "grant_type": "refresh_token"})

    def identity(self, tokens):
        profile = self._request("GET", f"{API}/profile", token=tokens.access_token)
        return Identity(email=profile["emailAddress"].lower(), extra={"history_id": profile.get("historyId", "")})

    def send(self, account, msg: OutgoingMessage) -> SentResult:
        token = self.access_token(account)
        raw = base64.urlsafe_b64encode(build_mime(msg)).decode()
        body = {"raw": raw}
        if msg.thread_id:
            body["threadId"] = msg.thread_id
        data = self._request("POST", f"{API}/messages/send", token=token, json=body)
        return SentResult(provider_message_id=data.get("id", ""), thread_id=data.get("threadId", ""), rfc_message_id=msg.message_id)

    def _message(self, token, msg_id) -> IncomingMessage:
        data = self._request("GET", f"{API}/messages/{msg_id}", token=token, params={"format": "full"})
        payload = data.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        plain, html, attachments = [], [], []

        def walk(part):
            mime = part.get("mimeType", "")
            body = part.get("body", {})
            if part.get("filename"):
                attachments.append(part["filename"])
            elif mime == "text/plain" and body.get("data"):
                plain.append(_b64url_decode(body["data"]).decode("utf-8", errors="replace"))
            elif mime == "text/html" and body.get("data"):
                html.append(_b64url_decode(body["data"]).decode("utf-8", errors="replace"))
            for p in part.get("parts", []) or []:
                walk(p)

        walk(payload)
        name, addr = parse_address(headers.get("from", ""))
        received = datetime.fromtimestamp(int(data.get("internalDate", "0")) / 1000, tz=dt_tz.utc)
        return IncomingMessage(
            provider_message_id=data["id"], thread_id=data.get("threadId", ""), from_email=addr, from_name=name,
            to=parse_address_list(headers.get("to", "")), cc=parse_address_list(headers.get("cc", "")),
            subject=headers.get("subject", ""), body_text="\n".join(plain) or html_to_text("\n".join(html)),
            received_at=received, rfc_message_id=headers.get("message-id", ""), in_reply_to=headers.get("in-reply-to", ""),
            references=headers.get("references", ""), attachment_names=attachments,
            headers={k: v for k, v in headers.items() if k in ("auto-submitted", "x-autoreply", "x-failed-recipients",
                                                               "precedence", "content-type", "return-path", "list-id")},
        )

    def fetch_new(self, account, cursor):
        token = self.access_token(account)
        ids = []
        new_cursor = cursor
        if cursor:
            try:
                page_token = None
                while True:
                    params = {"startHistoryId": cursor, "historyTypes": "messageAdded", "labelId": "INBOX"}
                    if page_token:
                        params["pageToken"] = page_token
                    data = self._request("GET", f"{API}/history", token=token, params=params)
                    for h in data.get("history", []):
                        for added in h.get("messagesAdded", []):
                            ids.append(added["message"]["id"])
                    new_cursor = data.get("historyId", new_cursor)
                    page_token = data.get("nextPageToken")
                    if not page_token:
                        break
            except ProviderError as exc:
                if "404" not in str(exc):
                    raise
                cursor = ""  # history expired - fall back to a time-based listing
        if not cursor:
            data = self._request("GET", f"{API}/messages", token=token, params={"q": "in:inbox newer_than:3d", "maxResults": 100})
            ids = [m["id"] for m in data.get("messages", [])]
            profile = self._request("GET", f"{API}/profile", token=token)
            new_cursor = str(profile.get("historyId", ""))
        seen, messages = set(), []
        for mid in ids:
            if mid not in seen:
                seen.add(mid)
                messages.append(self._message(token, mid))
        return messages, str(new_cursor or "")
