"""Requests for access: the public form, the email to the approvers, and approving or denying."""
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


def form_data(**changes):
    data = {
        "name": "Ada Lovelace", "email": "Ada@Example.org", "affiliation": "University of Somewhere",
        "areas": ["browse", "game"], "reason": "I study ambrosia beetles.", "leave_blank": "",
    }
    data.update(changes)
    return data


@override_settings(ACCESS_REQUEST_RECIPIENTS=APPROVERS)
class AccessCase(PageBehaviourCase):
    def send(self, **changes):
        return self.client.post(reverse("request_access"), form_data(**changes))

    def make_request(self, **changes):
        fields = {"name": "Ada Lovelace", "email": "ada@example.org", "affiliation": "Somewhere",
                  "reason": "Research.", "areas": ["browse"]}
        fields.update(changes)
        return AccessRequest.objects.create(**fields)

    def decide(self, access_request, decision, note="", user=None):
        self.client.force_login(user or self.superuser)
        return self.client.post(reverse("access_requests"), {
            "request_id": access_request.id, "decision": decision, "note": note,
        }, follow=True)


class RequestFormTests(AccessCase):
    def test_form_is_public_and_lists_every_area(self):
        response = self.client.get(reverse("request_access"))
        self.assertEqual(response.status_code, 200)
        for _, label, _, _ in access.AREAS:
            self.assertContains(response, label)

    def test_submitting_saves_a_pending_request_and_emails_both_approvers(self):
        response = self.send()
        self.assertRedirects(response, reverse("request_access_sent"))
        saved = AccessRequest.objects.get()
        self.assertEqual((saved.name, saved.email, saved.status), ("Ada Lovelace", "ada@example.org", "pending"))
        self.assertEqual(saved.areas, ["browse", "game"])
        self.assertIsNotNone(saved.notified_at)

        [message] = mail.outbox
        self.assertEqual(message.to, APPROVERS)
        self.assertEqual(message.reply_to, ["ada@example.org"])
        self.assertIn("Ada Lovelace", message.subject)
        for text in ("University of Somewhere", "Browse and download images", "Beetle ID game",
                     "I study ambrosia beetles.", "/tools/access-requests/"):
            self.assertIn(text, message.body)

    def test_nothing_is_created_until_it_is_approved(self):
        self.send()
        self.assertFalse(User.objects.filter(email__iexact="ada@example.org").exists())

    def test_the_form_reports_what_is_wrong_and_saves_nothing(self):
        for changes, message in [
            ({"name": ""}, "This field is required"),
            ({"email": "not-an-email"}, "valid email"),
            ({"affiliation": ""}, "This field is required"),
            ({"reason": ""}, "This field is required"),
            ({"areas": []}, "Choose at least one part of the site"),
            ({"areas": ["superuser"]}, "not one of the available choices"),
            ({"name": "x" * 201}, "at most 200"),
        ]:
            with self.subTest(changes=changes):
                response = self.send(**changes)
                self.assertContains(response, message)
        self.assertFalse(AccessRequest.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_a_name_cannot_break_the_email_subject(self):
        self.send(name="Ada\r\nBcc: someone@example.org")
        [message] = mail.outbox
        self.assertNotIn("\n", message.subject)
        self.assertEqual(message.bcc, [])

    def test_the_hidden_field_catches_bots(self):
        response = self.send(leave_blank="http://spam.example")
        self.assertRedirects(response, reverse("request_access_sent"))
        self.assertFalse(AccessRequest.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_a_second_request_from_the_same_email_is_not_added_or_mailed(self):
        self.send()
        response = self.send(email="ADA@example.org", reason="Again")
        self.assertRedirects(response, reverse("request_access_sent"))
        self.assertEqual(AccessRequest.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_new_request_is_allowed_once_the_earlier_one_is_decided(self):
        self.send()
        self.decide(AccessRequest.objects.get(), "deny")
        self.send(reason="Second try")
        self.assertEqual(AccessRequest.objects.count(), 2)

    def test_too_many_requests_from_one_address_are_turned_away(self):
        for n in range(access.THROTTLE_PER_IP):
            self.send(email=f"person{n}@example.org")
        response = self.send(email="one-more@example.org")
        self.assertContains(response, "too many requests")
        self.assertEqual(AccessRequest.objects.count(), access.THROTTLE_PER_IP)

    def test_the_request_is_kept_when_the_email_cannot_be_sent(self):
        with mock.patch("beetlesgallery.beetles_app.access.EmailMessage.send", side_effect=OSError("no route")):
            response = self.send()
        self.assertRedirects(response, reverse("request_access_sent"))
        saved = AccessRequest.objects.get()
        self.assertIsNone(saved.notified_at)
        self.assertIn("no route", saved.notify_error)

    def test_every_superuser_with_an_email_is_told_too(self):
        self.superuser.email = "boss@example.org"
        self.superuser.save()
        User.objects.create_superuser("gone", email="gone@example.org", password="pw", is_active=False)
        User.objects.create_superuser("dup", email="HULCR@example.org", password="pw")
        self.send()
        self.assertEqual(sorted(mail.outbox[0].to), sorted(APPROVERS + ["boss@example.org"]))

    @override_settings(ACCESS_REQUEST_RECIPIENTS=[])
    def test_the_request_is_kept_when_no_approvers_are_configured(self):
        self.send()
        self.assertIn("No approvers", AccessRequest.objects.get().notify_error)
        self.assertEqual(mail.outbox, [])

    def test_a_signed_in_person_gets_their_details_filled_in(self):
        self.user.email = "user@example.org"
        self.user.save()
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("request_access")), "user@example.org")


class ReviewPageTests(AccessCase):
    def test_only_superusers_can_open_it_or_decide(self):
        pending = self.make_request()
        url = reverse("access_requests")
        self.assertRedirectsToLogin(self.client.get(url))
        for account in (self.user, self.staff):
            self.client.force_login(account)
            self.assertRedirectsToLogin(self.client.get(url))
            self.assertRedirectsToLogin(self.client.post(url, {"request_id": pending.id, "decision": "member"}))
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertFalse(User.objects.filter(email="ada@example.org").exists())

    def test_it_lists_waiting_requests_with_what_they_asked_for(self):
        self.make_request(name="Grace Hopper", email="grace@example.org", areas=["annotate"], reason="Curating")
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("access_requests"))
        self.assertContains(response, "Grace Hopper")
        self.assertContains(response, "Annotate and validate")
        self.assertContains(response, "Curating")

    def test_it_warns_when_the_email_already_has_an_account(self):
        User.objects.create_user("ada", email="ADA@example.org", password="pw")
        self.make_request()
        self.client.force_login(self.superuser)
        self.assertContains(self.client.get(reverse("access_requests")), "already has an account")

    def test_data_management_shows_the_number_waiting_to_superusers_only(self):
        self.make_request()
        self.make_request(name="Grace", email="grace@example.org")
        self.client.force_login(self.superuser)
        self.assertContains(self.client.get(reverse("data_management")), "2 waiting")
        self.client.force_login(self.staff)
        self.assertNotContains(self.client.get(reverse("data_management")), "waiting")


class ApprovalTests(AccessCase):
    def test_approving_as_member_creates_an_account_without_a_password(self):
        pending = self.make_request(name="Ada Lovelace", email="Ada@Example.org")
        self.decide(pending, "member")
        pending.refresh_from_db()
        user = User.objects.get(email="Ada@Example.org")
        self.assertEqual((pending.status, pending.granted_role, pending.user, pending.decided_by),
                         ("approved", "member", user, self.superuser))
        self.assertIsNotNone(pending.decided_at)
        self.assertEqual((user.username, user.first_name), ("Ada", "Ada Lovelace"))
        self.assertFalse(user.has_usable_password())
        self.assertEqual((user.is_active, user.is_staff, user.is_superuser), (True, False, False))

    def test_approving_as_curator_makes_staff_but_never_a_superuser(self):
        pending = self.make_request()
        self.decide(pending, "curator")
        user = User.objects.get(email="ada@example.org")
        self.assertEqual((user.is_staff, user.is_superuser), (True, False))

    def test_the_applicant_can_set_a_password_from_the_email_and_sign_in(self):
        pending = self.make_request(name="Ada Lovelace")
        self.decide(pending, "member", note="Welcome aboard")
        [message] = mail.outbox
        self.assertEqual(message.to, ["ada@example.org"])
        self.assertIn("Your username is: ada", message.body)
        self.assertIn("Member access", message.body)
        self.assertIn("Welcome aboard", message.body)
        link = re.search(r"http://testserver/accounts/set-password/\S+", message.body).group(0)

        self.client.logout()
        response = self.client.get(link, follow=True)
        self.assertContains(response, "Set your password")
        form_url = response.redirect_chain[-1][0]
        done = self.client.post(form_url, {"new_password1": STRONG, "new_password2": STRONG})
        self.assertRedirects(done, reverse("login"), fetch_redirect_response=False)
        self.assertTrue(self.client.login(username="ada", password=STRONG))

    def test_the_set_password_link_stops_working_once_used(self):
        pending = self.make_request()
        self.decide(pending, "member")
        link = re.search(r"http://testserver/accounts/set-password/\S+", mail.outbox[-1].body).group(0)
        self.client.logout()
        form_url = self.client.get(link, follow=True).redirect_chain[-1][0]
        self.client.post(form_url, {"new_password1": STRONG, "new_password2": STRONG})
        self.client.logout()
        self.assertContains(self.client.get(link, follow=True), "expired or was already used")

    def test_a_weak_password_is_refused(self):
        pending = self.make_request()
        self.decide(pending, "member")
        link = re.search(r"http://testserver/accounts/set-password/\S+", mail.outbox[-1].body).group(0)
        self.client.logout()
        form_url = self.client.get(link, follow=True).redirect_chain[-1][0]
        response = self.client.post(form_url, {"new_password1": "12345678", "new_password2": "12345678"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.get(username="ada").has_usable_password())

    def test_usernames_do_not_collide(self):
        User.objects.create_user("ada", email="someone-else@example.org", password="pw")
        pending = self.make_request()
        self.decide(pending, "member")
        self.assertEqual(User.objects.get(email="ada@example.org").username, "ada2")

    def test_approving_an_existing_account_raises_it_but_never_lowers_it(self):
        member = User.objects.create_user("ada", email="ada@example.org", password="pw")
        self.decide(self.make_request(), "curator")
        member.refresh_from_db()
        self.assertTrue(member.is_staff)
        self.assertEqual(User.objects.filter(email__iexact="ada@example.org").count(), 1)
        self.assertIn("existing account (ada)", mail.outbox[-1].body)
        self.assertNotIn("set-password", mail.outbox[-1].body)

        staff = User.objects.create_user("gh", email="grace@example.org", password="pw", is_staff=True)
        self.decide(self.make_request(name="Grace", email="grace@example.org"), "member")
        staff.refresh_from_db()
        self.assertTrue(staff.is_staff)

        boss = User.objects.create_superuser("boss", email="boss@example.org", password="pw")
        self.decide(self.make_request(name="Boss", email="boss@example.org"), "member")
        boss.refresh_from_db()
        self.assertTrue(boss.is_superuser)

    def test_two_accounts_with_the_email_are_not_guessed_between(self):
        User.objects.create_user("a1", email="ada@example.org", password="pw")
        User.objects.create_user("a2", email="ADA@example.org", password="pw")
        pending = self.make_request()
        response = self.decide(pending, "curator")
        self.assertContains(response, "Several accounts use ada@example.org")
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertFalse(User.objects.filter(is_staff=True, username__in=["a1", "a2"]).exists())

    def test_a_deactivated_account_is_not_reactivated_by_a_request(self):
        User.objects.create_user("ada", email="ada@example.org", password="pw", is_active=False)
        pending = self.make_request()
        response = self.decide(pending, "member")
        self.assertContains(response, "deactivated account")
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertFalse(User.objects.get(username="ada").is_active)

    def test_if_the_applicant_cannot_be_emailed_the_approver_gets_the_link(self):
        pending = self.make_request()
        with mock.patch("beetlesgallery.beetles_app.access.EmailMessage.send", side_effect=OSError("no route")):
            response = self.decide(pending, "member")
        self.assertContains(response, "could not be emailed")
        self.assertContains(response, "/accounts/set-password/")
        pending.refresh_from_db()
        self.assertEqual(pending.status, "approved")  # the decision stands


class DenialTests(AccessCase):
    def test_denying_creates_no_account_and_tells_the_applicant(self):
        pending = self.make_request()
        self.decide(pending, "deny", note="Please apply with your institutional address.")
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.granted_role, pending.user), ("denied", "", None))
        self.assertFalse(User.objects.filter(email="ada@example.org").exists())
        [message] = mail.outbox
        self.assertEqual(message.to, ["ada@example.org"])
        self.assertIn("not able to give you an account", message.body)
        self.assertIn("institutional address", message.body)

    def test_a_request_can_only_be_decided_once(self):
        pending = self.make_request()
        self.decide(pending, "deny")
        response = self.decide(pending, "curator")
        self.assertContains(response, "already denied")
        self.assertFalse(User.objects.filter(email="ada@example.org").exists())
        self.assertEqual(len(mail.outbox), 1)

    def test_an_unknown_decision_or_request_changes_nothing(self):
        pending = self.make_request()
        self.assertContains(self.decide(pending, "superuser"), "Choose Member, Curator or Deny")
        self.assertContains(self.decide(mock.Mock(id="not-a-uuid"), "member"), "no longer exists")
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertEqual(mail.outbox, [])


class RolesTests(AccessCase):
    def test_role_needed(self):
        self.assertEqual(access.role_needed(["browse", "game"]), "member")
        self.assertEqual(access.role_needed(["browse", "upload"]), "curator")
        self.assertEqual(access.role_needed([]), "member")

    def test_login_page_points_to_the_form(self):
        self.assertContains(self.client.get(reverse("login")), reverse("request_access"))
