"""Asking for an account: choosing a username and password, confirming the email, approval, signing in and password reset."""
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from beetlesgallery.beetles_app import access
from beetlesgallery.beetles_app.models import AccessRequest
from beetlesgallery.beetles_app.testing import PageBehaviourCase

User = get_user_model()
APPROVERS = ["gmarais@example.org", "hulcr@example.org"]
STRONG = "Correct-Horse-9-Staple"
LINK = r"http://testserver/accounts/[a-z-]+/\S+"


def form_data(**changes):
    data = {
        "name": "Ada Lovelace", "email": "Ada@Example.org", "affiliation": "University of Somewhere",
        "areas": ["browse", "game"], "reason": "I study ambrosia beetles.", "leave_blank": "",
        "username": "ada", "password1": STRONG, "password2": STRONG,
    }
    data.update(changes)
    return data


@override_settings(ACCESS_REQUEST_RECIPIENTS=APPROVERS)
class AccessCase(PageBehaviourCase):
    def send(self, **changes):
        return self.client.post(reverse("request_access"), form_data(**changes))

    def confirm(self):
        """Open the confirmation link from the last email to the applicant."""
        link = re.search(LINK, mail.outbox[-1].body).group(0)
        self.client.logout()
        return self.client.get(link)

    def apply_and_confirm(self, **changes):
        self.send(**changes)
        self.confirm()
        return AccessRequest.objects.get(user__username=changes.get("username", "ada"))

    def decide(self, access_request, decision, note="", user=None):
        self.client.force_login(user or self.superuser)
        return self.client.post(reverse("access_requests"), {
            "request_id": access_request.id, "decision": decision, "note": note,
        }, follow=True)


class RequestFormTests(AccessCase):
    def test_form_is_public_and_asks_for_a_username_and_password(self):
        response = self.client.get(reverse("request_access"))
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password1"')
        for _, label, _, _ in access.AREAS:
            self.assertContains(response, label)

    def test_submitting_makes_an_inactive_account_and_only_emails_the_applicant(self):
        response = self.send()
        self.assertRedirects(response, reverse("request_access_sent"))
        user = User.objects.get(username="ada")
        self.assertEqual((user.is_active, user.is_staff, user.email), (False, False, "ada@example.org"))
        self.assertTrue(user.check_password(STRONG))
        saved = AccessRequest.objects.get()
        self.assertEqual((saved.user, saved.status, saved.email_verified_at), (user, "pending", None))
        [message] = mail.outbox
        self.assertEqual(message.to, ["ada@example.org"])
        self.assertIn("confirm", message.subject.lower())
        self.assertNotIn(STRONG, message.body)

    def test_the_form_reports_what_is_wrong_and_creates_nothing(self):
        User.objects.create_user("taken", email="other@example.org", password="pw")
        User.objects.create_user("owner", email="owner@example.org", password="pw")
        for changes, message in [
            ({"name": ""}, "This field is required"),
            ({"email": "not-an-email"}, "valid email"),
            ({"areas": []}, "Choose at least one part of the site"),
            ({"username": ""}, "Choose a username"),
            ({"username": "TAKEN"}, "taken"),
            ({"username": "bad name!"}, "valid username"),
            ({"password1": STRONG, "password2": "different"}, "do not match"),
            ({"password1": "12345678", "password2": "12345678"}, "entirely numeric"),
            ({"password1": "ada", "password2": "ada"}, "too short"),
            ({"email": "OWNER@example.org"}, "already exists"),
        ]:
            with self.subTest(changes=changes):
                self.assertContains(self.send(**changes), message)
        self.assertFalse(AccessRequest.objects.exists())
        self.assertFalse(User.objects.filter(username="ada").exists())
        self.assertEqual(mail.outbox, [])

    def test_a_name_cannot_break_the_email_subject(self):
        self.send(name="Ada\r\nBcc: someone@example.org")
        self.assertEqual(mail.outbox[0].bcc, [])
        self.assertNotIn("\n", mail.outbox[0].subject)

    def test_the_hidden_field_catches_bots(self):
        self.assertRedirects(self.send(leave_blank="http://spam.example"), reverse("request_access_sent"))
        self.assertFalse(User.objects.filter(username="ada").exists())

    def test_asking_again_with_the_same_email_makes_no_second_account_and_resends_the_link(self):
        self.send()
        response = self.send(username="ada2", email="ADA@example.org")
        self.assertRedirects(response, reverse("request_access_sent"))
        self.assertEqual((AccessRequest.objects.count(), User.objects.filter(username="ada2").count()), (1, 0))
        self.assertEqual([m.to for m in mail.outbox], [["ada@example.org"], ["ada@example.org"]])

    def test_too_many_requests_from_one_address_are_turned_away(self):
        for n in range(access.THROTTLE_PER_IP):
            self.send(username=f"person{n}", email=f"person{n}@example.org")
        self.assertContains(self.send(username="one-more", email="more@example.org"), "too many requests")

    def test_the_request_is_kept_when_the_confirmation_email_cannot_be_sent(self):
        with mock.patch("beetlesgallery.beetles_app.access.EmailMessage.send", side_effect=OSError("no route")):
            self.assertRedirects(self.send(), reverse("request_access_sent"))
        self.assertIn("no route", AccessRequest.objects.get().notify_error)

    def test_someone_who_already_has_an_account_asks_without_a_new_one(self):
        self.user.email = "user@example.org"
        self.user.save()
        self.client.force_login(self.user)
        page = self.client.get(reverse("request_access"))
        self.assertContains(page, "user@example.org")
        self.assertNotContains(page, 'name="password1"')
        self.client.post(reverse("request_access"), {k: v for k, v in form_data(email="user@example.org", areas=["annotate"]).items()
                                                      if k not in ("username", "password1", "password2")})
        saved = AccessRequest.objects.get()
        self.assertEqual((saved.user, saved.email_verified_at is not None), (self.user, True))
        self.assertEqual(sorted(mail.outbox[0].to), sorted(APPROVERS))   # already confirmed: the approvers hear at once


class ConfirmEmailTests(AccessCase):
    def test_confirming_tells_the_approvers_once(self):
        self.send()
        self.assertEqual(len(mail.outbox), 1)
        link = re.search(LINK, mail.outbox[0].body).group(0)
        response = self.confirm()
        self.assertContains(response, "Email confirmed")
        self.assertIsNotNone(AccessRequest.objects.get().email_verified_at)
        approvers_mail = mail.outbox[-1]
        self.assertEqual((sorted(approvers_mail.to), approvers_mail.reply_to), (sorted(APPROVERS), ["ada@example.org"]))
        for text in ("Ada Lovelace", "University of Somewhere", "Browse and download images", "I study ambrosia beetles.", "/tools/access-requests/"):
            self.assertIn(text, approvers_mail.body)
        self.client.get(link)  # opening the link again changes nothing
        self.assertEqual(len(mail.outbox), 2)

    def test_a_bad_link_does_nothing(self):
        self.send()
        self.client.logout()
        self.assertContains(self.client.get("/accounts/verify-email/zz/not-a-token/"), "has expired")
        self.assertEqual(len(mail.outbox), 1)

    def test_an_unconfirmed_request_is_not_shown_to_the_approvers(self):
        self.send()
        self.client.force_login(self.superuser)
        self.assertNotContains(self.client.get(reverse("access_requests")), "Ada Lovelace")
        self.assertNotContains(self.client.get(reverse("data_management")), "waiting")

    @override_settings(ACCESS_REQUEST_RECIPIENTS=[])
    def test_with_no_approvers_configured_the_request_is_still_listed(self):
        self.send()
        self.confirm()
        self.assertIn("No approvers", AccessRequest.objects.get().notify_error)
        self.client.force_login(self.superuser)
        self.assertContains(self.client.get(reverse("access_requests")), "Ada Lovelace")

    def test_every_superuser_with_an_email_is_told_too(self):
        self.superuser.email = "boss@example.org"
        self.superuser.save()
        User.objects.create_superuser("gone", email="gone@example.org", password="pw", is_active=False)
        User.objects.create_superuser("dup", email="HULCR@example.org", password="pw")
        self.send()
        self.confirm()
        self.assertEqual(sorted(mail.outbox[-1].to), sorted(APPROVERS + ["boss@example.org"]))


class ReviewPageTests(AccessCase):
    def test_only_superusers_can_open_it_or_decide(self):
        waiting = self.apply_and_confirm()
        url = reverse("access_requests")
        self.assertRedirectsToLogin(self.client.get(url))
        for account in (self.user, self.staff):
            self.client.force_login(account)
            self.assertRedirectsToLogin(self.client.get(url))
            self.assertRedirectsToLogin(self.client.post(url, {"request_id": waiting.id, "decision": "member"}))
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "pending")
        self.assertFalse(User.objects.get(username="ada").is_active)

    def test_it_shows_the_request_the_account_they_chose_and_what_they_want(self):
        self.apply_and_confirm()
        self.client.force_login(self.superuser)
        page = self.client.get(reverse("access_requests"))
        for text in ("Ada Lovelace", "<strong>ada</strong>", "Browse and download images", "I study ambrosia beetles."):
            self.assertContains(page, text)
        self.assertNotContains(page, "already has an account")   # their own new account is not a duplicate
        self.assertContains(self.client.get(reverse("data_management")), "1 waiting")


class ApprovalTests(AccessCase):
    def test_approving_activates_the_account_they_made_and_they_can_sign_in(self):
        waiting = self.apply_and_confirm()
        self.decide(waiting, "member", note="Welcome aboard")
        waiting.refresh_from_db()
        user = User.objects.get(username="ada")
        self.assertEqual((waiting.status, waiting.granted_role, waiting.decided_by), ("approved", "member", self.superuser))
        self.assertEqual((user.is_active, user.is_staff, user.is_superuser), (True, False, False))
        message = mail.outbox[-1]
        self.assertEqual(message.to, ["ada@example.org"])
        for text in ("Member access", "username you chose: ada", "Welcome aboard", reverse("password_reset")):
            self.assertIn(text, message.body)
        self.assertNotIn("set-password", message.body)
        self.assertNotIn(STRONG, message.body)
        self.client.logout()
        self.assertTrue(self.client.login(username="ada", password=STRONG))

    def test_approving_as_curator_makes_staff_but_never_a_superuser(self):
        self.decide(self.apply_and_confirm(), "curator")
        user = User.objects.get(username="ada")
        self.assertEqual((user.is_staff, user.is_superuser), (True, False))

    def test_an_unconfirmed_request_cannot_be_approved(self):
        self.send()
        waiting = AccessRequest.objects.get()
        response = self.decide(waiting, "member")
        self.assertContains(response, "has not confirmed their email")
        self.assertFalse(User.objects.get(username="ada").is_active)

    def test_asking_while_signed_in_raises_the_account_but_never_lowers_it(self):
        member = User.objects.create_user("m", email="m@example.org", password="pw")
        for account, expected_staff in ((member, True), (self.staff, True)):
            self.client.force_login(account)
            self.client.post(reverse("request_access"), {"name": "X", "email": account.email or "s@example.org", "affiliation": "Y",
                                                         "areas": ["annotate"], "reason": "Z", "leave_blank": ""})
            waiting = AccessRequest.objects.filter(user=account).latest("created_at")
            self.decide(waiting, "member" if account is self.staff else "curator")
            account.refresh_from_db()
            self.assertEqual(account.is_staff, expected_staff)
        boss_request = AccessRequest.objects.create(name="Boss", email="b@example.org", user=self.superuser, email_verified_at=self.superuser.date_joined)
        self.decide(boss_request, "member")
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_superuser)

    def test_a_request_can_only_be_decided_once(self):
        waiting = self.apply_and_confirm()
        self.decide(waiting, "deny")
        response = self.decide(waiting, "curator")
        self.assertContains(response, "already denied")

    def test_an_unknown_decision_or_request_changes_nothing(self):
        waiting = self.apply_and_confirm()
        self.assertContains(self.decide(waiting, "superuser"), "Choose Member, Curator or Deny")
        self.assertContains(self.decide(mock.Mock(id="not-a-uuid"), "member"), "no longer exists")
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "pending")

    def test_if_the_applicant_cannot_be_emailed_the_approver_is_told_the_account_is_ready(self):
        waiting = self.apply_and_confirm()
        with mock.patch("beetlesgallery.beetles_app.access.EmailMessage.send", side_effect=OSError("no route")):
            response = self.decide(waiting, "member")
        self.assertContains(response, "could not be emailed")
        self.assertContains(response, "username ada")
        self.assertTrue(User.objects.get(username="ada").is_active)


class DenialTests(AccessCase):
    def test_denying_removes_the_unused_account_frees_the_username_and_tells_them(self):
        waiting = self.apply_and_confirm()
        self.decide(waiting, "deny", note="Please apply with your institutional address.")
        waiting.refresh_from_db()
        self.assertEqual((waiting.status, waiting.user), ("denied", None))
        self.assertFalse(User.objects.filter(username="ada").exists())
        self.assertIn("institutional address", mail.outbox[-1].body)
        self.assertIn("not able to give you an account", mail.outbox[-1].body)
        self.assertRedirects(self.send(), reverse("request_access_sent"))   # they may ask again, with the same username

    def test_denying_a_signed_in_persons_extra_access_keeps_their_account(self):
        self.client.force_login(self.user)
        self.client.post(reverse("request_access"), {"name": "U", "email": "u@example.org", "affiliation": "Y", "areas": ["upload"],
                                                     "reason": "Z", "leave_blank": ""})
        self.decide(AccessRequest.objects.get(user=self.user), "deny")
        self.assertTrue(User.objects.filter(pk=self.user.pk, is_active=True).exists())

    def test_an_older_request_with_no_account_is_replaced_by_a_new_one(self):
        AccessRequest.objects.create(name="Old", email="ada@example.org")
        self.send()
        self.assertEqual(AccessRequest.objects.get(name="Old").status, "denied")
        self.assertTrue(AccessRequest.objects.filter(user__username="ada", status="pending").exists())


class SignInTests(AccessCase):
    def login(self, username="ada", password=STRONG):
        return self.client.post(reverse("login"), {"username": username, "password": password})

    def test_someone_waiting_is_told_why_they_cannot_sign_in(self):
        self.send()
        self.assertContains(self.login(), "confirm your email first")
        self.confirm()
        self.assertContains(self.login(), "waiting for approval")

    def test_a_wrong_password_or_unknown_user_gets_the_usual_message(self):
        self.apply_and_confirm()
        for username, password in (("ada", "wrong-password-1"), ("nobody", STRONG)):
            with self.subTest(username=username):
                self.assertContains(self.login(username, password), "enter a correct username and password")

    def test_after_approval_sign_in_works_and_a_denied_person_gets_the_usual_message(self):
        self.decide(self.apply_and_confirm(), "member")
        self.client.logout()
        self.assertEqual(self.login().status_code, 302)
        self.client.logout()
        self.decide(self.apply_and_confirm(username="bea", email="bea@example.org", name="Bea"), "deny")
        self.client.logout()
        self.assertContains(self.login("bea"), "enter a correct username and password")

    def test_the_sign_in_page_links_to_reset_and_to_the_request_form(self):
        page = self.client.get(reverse("login"))
        self.assertContains(page, reverse("password_reset"))
        self.assertContains(page, reverse("request_access"))


class PasswordResetTests(AccessCase):
    def setUp(self):
        super().setUp()
        self.user.email = "user@example.org"
        self.user.save()

    def ask(self, email):
        return self.client.post(reverse("password_reset"), {"email": email})

    def test_the_form_and_the_sent_page_are_public(self):
        self.assertEqual(self.client.get(reverse("password_reset")).status_code, 200)
        self.assertEqual(self.client.get(reverse("password_reset_done")).status_code, 200)

    def test_an_account_owner_gets_a_link_and_can_choose_a_new_password(self):
        self.assertRedirects(self.ask("USER@example.org"), reverse("password_reset_done"))
        [message] = mail.outbox
        self.assertEqual(message.to, ["user@example.org"])
        link = re.search(r"http://testserver/accounts/set-password/\S+", message.body).group(0)
        form_url = self.client.get(link, follow=True).redirect_chain[-1][0]
        done = self.client.post(form_url, {"new_password1": STRONG, "new_password2": STRONG})
        self.assertRedirects(done, reverse("login"), fetch_redirect_response=False)
        self.assertTrue(self.client.login(username="user", password=STRONG))
        self.assertContains(self.client.get(link, follow=True), "expired or was already used")  # single use

    def test_unknown_addresses_and_accounts_still_waiting_get_the_same_page_and_no_email(self):
        self.send()
        for email in ("nobody@example.org", "ada@example.org"):
            with self.subTest(email=email):
                before = len(mail.outbox)
                self.assertRedirects(self.ask(email), reverse("password_reset_done"))
                self.assertEqual(len(mail.outbox), before)

    def test_a_weak_new_password_is_refused(self):
        self.ask("user@example.org")
        link = re.search(r"http://testserver/accounts/set-password/\S+", mail.outbox[-1].body).group(0)
        form_url = self.client.get(link, follow=True).redirect_chain[-1][0]
        self.assertEqual(self.client.post(form_url, {"new_password1": "12345678", "new_password2": "12345678"}).status_code, 200)
        self.assertFalse(self.client.login(username="user", password="12345678"))

    def test_asking_too_often_sends_nothing_more_and_says_nothing_different(self):
        for _ in range(10):
            self.ask("user@example.org")
        self.assertEqual(len(mail.outbox), 10)
        self.assertRedirects(self.ask("user@example.org"), reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 10)


class RolesTests(AccessCase):
    def test_role_needed(self):
        self.assertEqual(access.role_needed(["browse", "game"]), "member")
        self.assertEqual(access.role_needed(["browse", "upload"]), "curator")
        self.assertEqual(access.role_needed([]), "member")


class EmailSetupTests(PageBehaviourCase):
    def test_the_test_email_command_reports_and_sends(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("send_test_email", "someone@example.org", stdout=out)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["someone@example.org"])
        self.assertIn("Sent to someone@example.org", out.getvalue())

    def test_the_test_email_command_reports_a_failure(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with mock.patch("beetlesgallery.beetles_app.management.commands.send_test_email.send_mail",
                        side_effect=ConnectionRefusedError("no server")):
            with self.assertRaisesRegex(CommandError, "ConnectionRefusedError: no server"):
                call_command("send_test_email", "someone@example.org", stdout=__import__("io").StringIO())
