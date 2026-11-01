import base64
import json
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.testing import make_user
from apps.mailbox.models import MailAccount
from apps.mailbox.providers.base import Attachment, AuthError, OutgoingMessage, get_provider


class FakeResp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data
        self.content = b"x" if data is not None or text else b""
        self.text = text or json.dumps(data or {})

    def json(self):
        return self._data


def router(routes):
    calls = []

    def _request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        for (m, fragment), resp in routes.items():
            if m == method and fragment in url:
                return resp(kwargs) if callable(resp) else resp
        raise AssertionError(f"unexpected {method} {url}")

    return _request, calls


def account(provider, **kw):
    acc = MailAccount(provider=provider, email_address="export@flotec.example", **kw)
    acc.access_token = "AT"
    acc.refresh_token = "RT"
    acc.token_expires_at = timezone.now() + timezone.timedelta(hours=1)
    acc.save()
    return acc


MSG = OutgoingMessage(from_email="export@flotec.example", from_name="Flotec", to=["buyer@delta.example"], subject="Hello",
                      body_text="Body text", attachments=[Attachment("cat.pdf", b"%PDF")], message_id="<abc@flotec.example>",
                      headers={"List-Unsubscribe": "<https://x/u/t/>"})


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", GOOGLE_OAUTH_CLIENT_SECRET="gsecret", MS_OAUTH_CLIENT_ID="mid",
                   MS_OAUTH_CLIENT_SECRET="msecret", ZOHO_OAUTH_CLIENT_ID="zid", ZOHO_OAUTH_CLIENT_SECRET="zsecret", ZOHO_DC="in")
class ProviderTests(TestCase):
    def test_tokens_encrypted_at_rest(self):
        acc = account("gmail")
        raw = MailAccount.objects.filter(pk=acc.pk).values_list("_access_token", "_refresh_token").get()
        self.assertNotIn("AT", raw[0])
        self.assertNotIn("RT", raw[1])
        self.assertEqual(MailAccount.objects.get(pk=acc.pk).access_token, "AT")

    def test_gmail_send_and_fetch(self):
        acc = account("gmail")
        sent_body = {}

        def send(kwargs):
            sent_body.update(kwargs["json"])
            return FakeResp(200, {"id": "g1", "threadId": "t1"})

        payload = {"id": "in1", "threadId": "t1", "internalDate": "1767225600000", "payload": {
            "headers": [{"name": "From", "value": "Amina <buyer@delta.example>"}, {"name": "Subject", "value": "Re: Hello"},
                        {"name": "Message-ID", "value": "<r1@delta.example>"}, {"name": "In-Reply-To", "value": "<abc@flotec.example>"}],
            "mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Yes, send prices").decode()}},
                {"mimeType": "application/pdf", "filename": "rfq.pdf", "body": {}}]}}
        req, calls = router({
            ("POST", "/messages/send"): send,
            ("GET", "/messages/in1"): FakeResp(200, payload),
            ("GET", "/users/me/messages"): FakeResp(200, {"messages": [{"id": "in1"}]}),
            ("GET", "/profile"): FakeResp(200, {"emailAddress": "export@flotec.example", "historyId": "99"}),
        })
        with mock.patch("apps.mailbox.providers.base.requests.request", side_effect=req):
            p = get_provider("gmail")
            res = p.send(acc, MSG)
            msgs, cursor = p.fetch_new(acc, "")
        self.assertEqual((res.provider_message_id, res.thread_id), ("g1", "t1"))
        raw = base64.urlsafe_b64decode(sent_body["raw"]).decode(errors="replace")
        self.assertIn("Message-ID: <abc@flotec.example>", raw)
        self.assertIn("List-Unsubscribe", raw)
        self.assertIn('filename="cat.pdf"', raw)
        self.assertEqual(cursor, "99")
        m = msgs[0]
        self.assertEqual((m.from_email, m.in_reply_to, m.body_text), ("buyer@delta.example", "<abc@flotec.example>", "Yes, send prices"))
        self.assertEqual(m.attachment_names, ["rfq.pdf"])

    def test_outlook_send_via_draft_and_fetch(self):
        acc = account("outlook")
        req, calls = router({
            ("POST", "/me/messages/d1/send"): FakeResp(202, None),
            ("POST", "/me/messages"): FakeResp(201, {"id": "d1", "conversationId": "c1", "internetMessageId": "<o1@outlook>"}),
            ("GET", "/mailFolders/inbox/messages"): FakeResp(200, {"value": [{
                "id": "i1", "conversationId": "c1", "internetMessageId": "<x@y>", "subject": "Re: Hello",
                "from": {"emailAddress": {"address": "Buyer@Delta.example", "name": "B"}}, "toRecipients": [],
                "receivedDateTime": "2026-11-01T10:00:00Z", "body": {"contentType": "text", "content": "Interested"},
                "internetMessageHeaders": [{"name": "In-Reply-To", "value": "<o1@outlook>"}]}]}),
        })
        with mock.patch("apps.mailbox.providers.base.requests.request", side_effect=req):
            p = get_provider("outlook")
            res = p.send(acc, MSG)
            msgs, cursor = p.fetch_new(acc, "")
        self.assertEqual(res.rfc_message_id, "<o1@outlook>")
        draft_json = calls[0][2]["json"]
        self.assertEqual(draft_json["attachments"][0]["name"], "cat.pdf")
        self.assertNotIn("internetMessageHeaders", draft_json, "Graph only accepts x- headers")
        self.assertEqual(msgs[0].from_email, "buyer@delta.example")
        self.assertEqual(msgs[0].in_reply_to, "<o1@outlook>")
        self.assertEqual(cursor, "2026-11-01T10:00:00Z")

    def test_zoho_send_with_attachment_and_token_errors(self):
        acc = account("zoho", provider_account_id="123")
        req, calls = router({
            ("POST", "/messages/attachments"): FakeResp(200, {"status": {"code": 200}, "data": {"storeName": "s", "attachmentPath": "/p", "attachmentName": "cat.pdf"}}),
            ("POST", "/accounts/123/messages"): FakeResp(200, {"status": {"code": 200}, "data": {"messageId": "z1", "mailId": "<z@zoho>"}}),
            ("POST", "accounts.zoho.in/oauth/v2/token"): FakeResp(200, {"error": "invalid_code"}),
        })
        with mock.patch("apps.mailbox.providers.base.requests.request", side_effect=req):
            p = get_provider("zoho")
            res = p.send(acc, MSG)
            self.assertEqual((res.provider_message_id, res.rfc_message_id), ("z1", "<z@zoho>"))
            body = calls[1][2]["json"]
            self.assertEqual(body["attachments"][0]["attachmentName"], "cat.pdf")
            self.assertEqual(calls[1][2]["headers"]["Authorization"], "Zoho-oauthtoken AT")
            self.assertIn("mail.zoho.in", calls[1][1])
            with self.assertRaises(AuthError):
                p.exchange_code("bad")

    def test_expired_token_is_refreshed(self):
        acc = account("gmail")
        acc.token_expires_at = timezone.now() - timezone.timedelta(minutes=1)
        acc.save()
        req, calls = router({("POST", "oauth2.googleapis.com/token"): FakeResp(200, {"access_token": "NEW", "expires_in": 3600})})
        with mock.patch("apps.mailbox.providers.base.requests.request", side_effect=req):
            self.assertEqual(get_provider("gmail").access_token(acc), "NEW")
        acc.refresh_from_db()
        self.assertEqual(acc.access_token, "NEW")
        self.assertEqual(acc.refresh_token, "RT")

    def test_http_401_raises_auth_error(self):
        acc = account("gmail")
        req, _ = router({("POST", "/messages/send"): FakeResp(401, {"error": "invalid"})})
        with mock.patch("apps.mailbox.providers.base.requests.request", side_effect=req), self.assertRaises(AuthError):
            get_provider("gmail").send(acc, MSG)


@override_settings(ZOHO_OAUTH_CLIENT_ID="zid", ZOHO_OAUTH_CLIENT_SECRET="zsecret", ZOHO_DC="in")
class OAuthFlowTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin")
        self.client.force_login(self.admin)

    def test_state_checked_and_account_created(self):
        resp = self.client.get("/mailbox/oauth/start/zoho/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("accounts.zoho.in/oauth/v2/auth", resp["Location"])
        state = self.client.session["oauth_state_zoho"]
        bad = self.client.get("/mailbox/oauth/callback/zoho/", {"state": "wrong", "code": "c"})
        self.assertEqual(bad.status_code, 302)
        self.assertFalse(MailAccount.objects.exists())
        self.client.get("/mailbox/oauth/start/zoho/")
        state = self.client.session["oauth_state_zoho"]
        tokens = SimpleNamespace(access_token="AT", refresh_token="RT", expires_in=3600, scope="s", extra={})
        ident = SimpleNamespace(email="export@flotec.example", display_name="Export", account_id="777", extra={})
        with mock.patch("apps.mailbox.providers.zoho.ZohoProvider.exchange_code", return_value=tokens), \
                mock.patch("apps.mailbox.providers.zoho.ZohoProvider.identity", return_value=ident):
            self.client.get("/mailbox/oauth/callback/zoho/", {"state": state, "code": "c"})
        acc = MailAccount.objects.get()
        self.assertEqual((acc.email_address, acc.provider_account_id, acc.refresh_token), ("export@flotec.example", "777", "RT"))
        self.assertTrue(acc.is_default_sender)

    def test_non_admin_cannot_connect(self):
        from apps.core.models import Role

        self.client.force_login(make_user("ex", Role.EXECUTIVE))
        self.assertEqual(self.client.get("/mailbox/oauth/start/zoho/").status_code, 403)
