"""Microsoft 365 / Outlook via Microsoft Graph (OAuth 2.0, delegated Mail.ReadWrite + Mail.Send)."""
import base64
from datetime import datetime
from urllib.parse import urlencode

from django.conf import settings
from django.utils.dateparse import parse_datetime

from .base import AuthError, BaseProvider, Identity, IncomingMessage, OutgoingMessage, SentResult, TokenSet, html_to_text

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access Mail.ReadWrite Mail.Send User.Read"


class OutlookProvider(BaseProvider):
    name = "outlook"
    label = "Outlook"

    def _base(self):
        return f"https://login.microsoftonline.com/{settings.MS_OAUTH_TENANT}/oauth2/v2.0"

    def is_configured(self):
        return bool(settings.MS_OAUTH_CLIENT_ID and settings.MS_OAUTH_CLIENT_SECRET)

    def authorize_url(self, state):
        return f"{self._base()}/authorize?" + urlencode({
            "client_id": settings.MS_OAUTH_CLIENT_ID, "response_type": "code", "redirect_uri": self.redirect_uri(),
            "response_mode": "query", "scope": SCOPES, "state": state, "prompt": "select_account",
        })

    def _token(self, data):
        payload = self._request("POST", f"{self._base()}/token", data={
            "client_id": settings.MS_OAUTH_CLIENT_ID, "client_secret": settings.MS_OAUTH_CLIENT_SECRET, "scope": SCOPES, **data,
        })
        if "access_token" not in payload:
            raise AuthError(f"Microsoft token error: {payload.get('error_description') or payload}")
        return TokenSet(payload["access_token"], payload.get("refresh_token", ""), int(payload.get("expires_in", 3600)),
                        payload.get("scope", ""))

    def exchange_code(self, code):
        return self._token({"code": code, "redirect_uri": self.redirect_uri(), "grant_type": "authorization_code"})

    def refresh(self, refresh_token):
        return self._token({"refresh_token": refresh_token, "grant_type": "refresh_token"})

    def identity(self, tokens):
        me = self._request("GET", f"{GRAPH}/me", token=tokens.access_token)
        return Identity(email=(me.get("mail") or me.get("userPrincipalName") or "").lower(),
                        display_name=me.get("displayName", ""), account_id=me.get("id", ""))

    def _graph_message(self, msg: OutgoingMessage) -> dict:
        body = {
            "subject": msg.subject,
            "body": {"contentType": "Text", "content": msg.body_text},
            "toRecipients": [{"emailAddress": {"address": a}} for a in msg.to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in msg.cc],
            "attachments": [
                {"@odata.type": "#microsoft.graph.fileAttachment", "name": a.filename, "contentType": a.mimetype,
                 "contentBytes": base64.b64encode(a.content).decode()}
                for a in msg.attachments
            ],
        }
        custom = [{"name": k, "value": v} for k, v in msg.headers.items() if k.lower().startswith("x-")]
        if custom:
            body["internetMessageHeaders"] = custom
        return body

    def send(self, account, msg: OutgoingMessage) -> SentResult:
        token = self.access_token(account)
        if msg.reply_to_provider_id:
            draft = self._request("POST", f"{GRAPH}/me/messages/{msg.reply_to_provider_id}/createReply", token=token, json={})
            patch = self._graph_message(msg)
            if not msg.to:
                patch.pop("toRecipients", None)
            self._request("PATCH", f"{GRAPH}/me/messages/{draft['id']}", token=token, json=patch)
        else:
            draft = self._request("POST", f"{GRAPH}/me/messages", token=token, json=self._graph_message(msg))
        self._request("POST", f"{GRAPH}/me/messages/{draft['id']}/send", token=token, expect_json=False)
        return SentResult(provider_message_id=draft.get("id", ""), thread_id=draft.get("conversationId", ""),
                          rfc_message_id=draft.get("internetMessageId", ""))

    def fetch_new(self, account, cursor):
        token = self.access_token(account)
        params = {
            "$orderby": "receivedDateTime asc", "$top": "50",
            "$select": "id,conversationId,internetMessageId,subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments,internetMessageHeaders",
        }
        if cursor:
            params["$filter"] = f"receivedDateTime gt {cursor}"
        url = f"{GRAPH}/me/mailFolders/inbox/messages"
        messages, newest = [], cursor
        for _ in range(10):
            data = self._request("GET", url, token=token, params=params, headers={"Prefer": 'outlook.body-content-type="text"'})
            for m in data.get("value", []):
                headers = {h["name"].lower(): h["value"] for h in m.get("internetMessageHeaders") or []}
                frm = (m.get("from") or {}).get("emailAddress", {})
                body = m.get("body") or {}
                text = body.get("content", "") if body.get("contentType", "").lower() == "text" else html_to_text(body.get("content", ""))
                received = parse_datetime(m["receivedDateTime"]) or datetime.now()
                messages.append(IncomingMessage(
                    provider_message_id=m["id"], thread_id=m.get("conversationId", ""), from_email=(frm.get("address") or "").lower(),
                    from_name=frm.get("name", ""), to=[r["emailAddress"]["address"].lower() for r in m.get("toRecipients", [])],
                    cc=[r["emailAddress"]["address"].lower() for r in m.get("ccRecipients", [])], subject=m.get("subject") or "",
                    body_text=text, received_at=received, rfc_message_id=m.get("internetMessageId", ""),
                    in_reply_to=headers.get("in-reply-to", ""), references=headers.get("references", ""),
                    headers={k: v for k, v in headers.items() if k in ("auto-submitted", "x-autoreply", "x-failed-recipients", "precedence")},
                    attachment_names=["(attachment)"] if m.get("hasAttachments") else [],
                ))
                newest = m["receivedDateTime"]
            next_link = data.get("@odata.nextLink")
            if not next_link:
                break
            url, params = next_link, None
        return messages, newest or ""
