"""Zoho Mail via the Zoho Mail REST API (OAuth 2.0). Data centre set with ZOHO_DC (e.g. 'in' for India)."""
import re
from datetime import datetime, timezone as dt_tz
from urllib.parse import urlencode

from django.conf import settings

from .base import (
    AuthError, BaseProvider, Identity, IncomingMessage, OutgoingMessage, ProviderError, SentResult, TokenSet, html_to_text,
    parse_address, parse_address_list,
)

SCOPES = "ZohoMail.messages.ALL,ZohoMail.accounts.READ,ZohoMail.folders.READ"


class ZohoProvider(BaseProvider):
    name = "zoho"
    label = "Zoho Mail"

    @property
    def accounts_base(self):
        return f"https://accounts.zoho.{settings.ZOHO_DC}"

    @property
    def api(self):
        return f"https://mail.zoho.{settings.ZOHO_DC}/api"

    def is_configured(self):
        return bool(settings.ZOHO_OAUTH_CLIENT_ID and settings.ZOHO_OAUTH_CLIENT_SECRET)

    def authorize_url(self, state):
        return f"{self.accounts_base}/oauth/v2/auth?" + urlencode({
            "scope": SCOPES, "client_id": settings.ZOHO_OAUTH_CLIENT_ID, "response_type": "code", "access_type": "offline",
            "redirect_uri": self.redirect_uri(), "prompt": "consent", "state": state,
        })

    def _token(self, params):
        payload = self._request("POST", f"{self.accounts_base}/oauth/v2/token", params={
            "client_id": settings.ZOHO_OAUTH_CLIENT_ID, "client_secret": settings.ZOHO_OAUTH_CLIENT_SECRET, **params,
        })
        if "access_token" not in payload:  # Zoho reports errors with HTTP 200
            raise AuthError(f"Zoho token error: {payload.get('error') or payload}")
        return TokenSet(payload["access_token"], payload.get("refresh_token", ""), int(payload.get("expires_in", 3600)),
                        payload.get("scope", ""), {"api_domain": payload.get("api_domain", "")})

    def exchange_code(self, code):
        return self._token({"code": code, "redirect_uri": self.redirect_uri(), "grant_type": "authorization_code"})

    def refresh(self, refresh_token):
        return self._token({"refresh_token": refresh_token, "grant_type": "refresh_token"})

    def _zreq(self, method, path, token, **kwargs):
        data = self._request(method, f"{self.api}{path}", token=token, auth_scheme="Zoho-oauthtoken", **kwargs)
        status = (data.get("status") or {}) if isinstance(data, dict) else {}
        if status and status.get("code") not in (200, 201, None):
            raise ProviderError(f"Zoho Mail error {status.get('code')}: {status.get('description')}")
        return data

    def identity(self, tokens):
        data = self._zreq("GET", "/accounts", tokens.access_token)
        accounts = data.get("data") or []
        if not accounts:
            raise ProviderError("No Zoho Mail account found for this user.")
        acc = accounts[0]
        email = (acc.get("primaryEmailAddress") or acc.get("mailboxAddress") or acc.get("incomingUserName") or "").lower()
        return Identity(email=email, display_name=acc.get("displayName", ""), account_id=str(acc.get("accountId", "")))

    def _inbox_folder(self, account, token):
        folder_id = account.provider_data.get("inbox_folder_id")
        if folder_id:
            return folder_id
        data = self._zreq("GET", f"/accounts/{account.provider_account_id}/folders", token)
        for f in data.get("data", []):
            if (f.get("folderType") or "").lower() == "inbox" or (f.get("folderName") or "").lower() == "inbox":
                account.provider_data = {**account.provider_data, "inbox_folder_id": str(f["folderId"])}
                account.save(update_fields=["provider_data"])
                return str(f["folderId"])
        raise ProviderError("Zoho inbox folder not found.")

    def send(self, account, msg: OutgoingMessage) -> SentResult:
        token = self.access_token(account)
        acc = account.provider_account_id
        attachments = []
        for a in msg.attachments:
            up = self._zreq("POST", f"/accounts/{acc}/messages/attachments", token, params={"fileName": a.filename},
                            data=a.content, headers={"Content-Type": "application/octet-stream"})
            info = up.get("data") or {}
            if isinstance(info, list):
                info = info[0] if info else {}
            attachments.append({k: info.get(k) for k in ("storeName", "attachmentPath", "attachmentName")})
        body = {"fromAddress": msg.from_email, "toAddress": ",".join(msg.to), "subject": msg.subject,
                "content": msg.body_text, "mailFormat": "plaintext", "askReceipt": "no"}
        if msg.cc:
            body["ccAddress"] = ",".join(msg.cc)
        if attachments:
            body["attachments"] = attachments
        if msg.reply_to_provider_id:
            body["action"] = "reply"
            data = self._zreq("POST", f"/accounts/{acc}/messages/{msg.reply_to_provider_id}", token, json=body)
        else:
            data = self._zreq("POST", f"/accounts/{acc}/messages", token, json=body)
        info = data.get("data") or {}
        return SentResult(provider_message_id=str(info.get("messageId", "")), thread_id=str(info.get("threadId", "")),
                          rfc_message_id=str(info.get("mailId", "")))

    @staticmethod
    def _header(raw: str, name: str) -> str:
        m = re.search(rf"^{name}:\s*(.+(?:\r?\n[ \t].+)*)", raw or "", re.I | re.M)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

    def fetch_new(self, account, cursor):
        token = self.access_token(account)
        acc = account.provider_account_id
        folder = self._inbox_folder(account, token)
        since = int(cursor) if cursor and cursor.isdigit() else 0
        data = self._zreq("GET", f"/accounts/{acc}/messages/view", token,
                          params={"folderId": folder, "limit": 100, "sortorder": "false", "includeto": "true"})
        messages, newest = [], since
        for m in data.get("data", []):
            received_ms = int(m.get("receivedTime") or 0)
            if received_ms <= since:
                continue
            mid = str(m["messageId"])
            content = self._zreq("GET", f"/accounts/{acc}/folders/{folder}/messages/{mid}/content", token)
            raw_headers = ""
            try:
                raw_headers = (self._zreq("GET", f"/accounts/{acc}/folders/{folder}/messages/{mid}/header", token).get("data") or {}).get("headerContent", "")
            except ProviderError:
                pass
            name, addr = parse_address(m.get("sender") and f"{m.get('sender')} <{m.get('fromAddress')}>" or m.get("fromAddress", ""))
            html = (content.get("data") or {}).get("content", "")
            messages.append(IncomingMessage(
                provider_message_id=mid, thread_id=str(m.get("threadId") or ""), folder_id=folder, from_email=addr, from_name=name,
                to=parse_address_list(m.get("toAddress", "").replace("&lt;", "<").replace("&gt;", ">")),
                cc=parse_address_list(m.get("ccAddress", "").replace("&lt;", "<").replace("&gt;", ">")),
                subject=m.get("subject", ""), body_text=html_to_text(html),
                received_at=datetime.fromtimestamp(received_ms / 1000, tz=dt_tz.utc),
                rfc_message_id=self._header(raw_headers, "Message-ID"), in_reply_to=self._header(raw_headers, "In-Reply-To"),
                references=self._header(raw_headers, "References"),
                headers={h: self._header(raw_headers, h) for h in ("Auto-Submitted", "X-Autoreply", "X-Failed-Recipients") if self._header(raw_headers, h)},
                attachment_names=["(attachment)"] if str(m.get("hasAttachment", "0")) == "1" else [],
            ))
            newest = max(newest, received_ms)
        return messages, str(newest or since or "")
