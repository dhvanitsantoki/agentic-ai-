import time

from django.test import TestCase

from apps.core import totp
from apps.core.models import LoginAttempt, Role, SystemSettings, TwoFactorDevice
from apps.core.testing import make_user

PASSWORD = "Str0ng-Passw0rd!"


def enable_2fa(user, at=None):
    secret = totp.new_secret()
    d = TwoFactorDevice(user=user)
    d.secret = secret
    from django.utils import timezone

    d.confirmed_at = timezone.now()
    d.save()
    return d, secret


class TotpAlgorithmTests(TestCase):
    def test_rfc6238_vectors(self):
        # RFC 6238 Appendix B, SHA1, secret "12345678901234567890" (8-digit values truncated to 6)
        import base64

        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(totp.hotp(secret, 59 // 30, digits=8), "94287082")
        self.assertEqual(totp.hotp(secret, 1111111109 // 30, digits=8), "07081804")
        self.assertEqual(totp.hotp(secret, 2000000000 // 30, digits=8), "69279037")

    def test_window_and_replay(self):
        secret = totp.new_secret()
        now = time.time()
        code = totp.hotp(secret, totp.current_step(now))
        step = totp.verify(secret, code, at=now)
        self.assertIsNotNone(step)
        self.assertIsNone(totp.verify(secret, code, at=now, last_step=step), "same code cannot be reused")
        old = totp.hotp(secret, totp.current_step(now) - 3)
        self.assertIsNone(totp.verify(secret, old, at=now))
        self.assertIsNone(totp.verify(secret, "12345", at=now))

    def test_secret_encrypted_and_qr(self):
        user = make_user("q")
        d, secret = enable_2fa(user)
        raw = TwoFactorDevice.objects.filter(pk=d.pk).values_list("_secret", flat=True).get()
        self.assertNotIn(secret, raw)
        self.assertIn("<svg", totp.qr_svg(totp.provisioning_uri(secret, "q", "Flotec Sales AI")))


class TwoFactorLoginTests(TestCase):
    def setUp(self):
        self.user = make_user("owner", Role.ADMIN, password=PASSWORD)

    def _password(self):
        return self.client.post("/accounts/login/", {"username": "owner", "password": PASSWORD})

    def test_without_2fa_normal_login(self):
        resp = self._password()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_password_alone_is_not_enough(self):
        device, secret = enable_2fa(self.user)
        resp = self._password()
        self.assertRedirects(resp, "/accounts/two-factor/verify/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 302, "not signed in yet")
        resp = self.client.post("/accounts/two-factor/verify/", {"code": "000000"})
        self.assertContains(resp, "not valid")
        code = totp.hotp(secret, totp.current_step())
        resp = self.client.post("/accounts/two-factor/verify/", {"code": code})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.logout()
        self._password()
        resp = self.client.post("/accounts/two-factor/verify/", {"code": code})
        self.assertContains(resp, "not valid", msg_prefix="replayed code rejected")

    def test_recovery_codes_work_once(self):
        device, _ = enable_2fa(self.user)
        codes = device.new_recovery_codes()
        self._password()
        self.client.post("/accounts/two-factor/verify/", {"code": codes[0].upper()})
        self.assertEqual(self.client.get("/").status_code, 200)
        device.refresh_from_db()
        self.assertEqual(device.recovery_codes_left, 9)
        self.client.logout()
        self._password()
        self.assertContains(self.client.post("/accounts/two-factor/verify/", {"code": codes[0]}), "not valid")

    def test_code_brute_force_locked(self):
        enable_2fa(self.user)
        self._password()
        for _ in range(5):
            self.client.post("/accounts/two-factor/verify/", {"code": "111111"})
        resp = self.client.post("/accounts/two-factor/verify/", {"code": "222222"})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(LoginAttempt.objects.filter(username="2fa:owner", success=False).count(), 5)

    def test_pending_step_expires(self):
        enable_2fa(self.user)
        self._password()
        session = self.client.session
        session["two_factor_pending"]["ts"] = time.time() - 600
        session.save()
        self.assertRedirects(self.client.get("/accounts/two-factor/verify/"), "/accounts/login/", fetch_redirect_response=False)

    def test_setup_flow_and_recovery_codes_shown_once(self):
        self.client.force_login(self.user)
        resp = self.client.get("/accounts/two-factor/")
        self.assertContains(resp, "<svg")
        secret = self.client.session["two_factor_setup_secret"]
        self.assertContains(self.client.post("/accounts/two-factor/", {"code": "000000"}), "did not match")
        resp = self.client.post("/accounts/two-factor/", {"code": totp.hotp(secret, totp.current_step())})
        self.assertContains(resp, "shown only once")
        self.assertTrue(TwoFactorDevice.objects.get(user=self.user).is_active)

    def test_required_policy_forces_setup(self):
        s = SystemSettings.get_solo()
        s.require_two_factor = True
        s.save()
        self.client.force_login(make_user("ex", Role.EXECUTIVE))
        self.assertRedirects(self.client.get("/crm/companies/"), "/accounts/two-factor/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/accounts/two-factor/").status_code, 200)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_admin_can_reset_lost_phone(self):
        other = make_user("ex2", Role.EXECUTIVE)
        enable_2fa(other)
        self.client.force_login(self.user)
        self.client.post(f"/settings/users/{other.pk}/reset-2fa/")
        self.assertFalse(TwoFactorDevice.objects.filter(user=other).exists())
        self.client.force_login(make_user("ex3", Role.EXECUTIVE))
        enable_2fa(other)
        self.assertEqual(self.client.post(f"/settings/users/{other.pk}/reset-2fa/").status_code, 403)

    def test_disable_needs_current_code(self):
        device, secret = enable_2fa(self.user)
        self.client.force_login(self.user)
        self.client.post("/accounts/two-factor/manage/", {"code": "000000", "action": "disable"})
        self.assertTrue(TwoFactorDevice.objects.filter(user=self.user).exists())
        self.client.post("/accounts/two-factor/manage/", {"code": totp.hotp(secret, totp.current_step()), "action": "disable"})
        self.assertFalse(TwoFactorDevice.objects.filter(user=self.user).exists())
