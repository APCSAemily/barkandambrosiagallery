# Email: what has to be set up

The site sends email in four places, and **nothing in the account flow works without it**:

| When | To | What it says |
|---|---|---|
| Someone asks for access | the applicant | "Confirm your email address" link (valid a week) |
| They confirm it | you and every other approver | "Access request: <name>" with a link to the approval page |
| You approve or deny | the applicant | the decision (approved: username and sign-in link) |
| "Forgot your password?" | the account's owner | reset link (valid a week, once) |

## 1. Testing on your own computer: no email account needed

The site does not need an email account to run locally. `docker-compose.override.yml` adds **Mailpit**, a pretend mail
server that catches every email the site sends and shows it in a web page. Docker Compose loads that file by itself, so:

```
docker compose up -d --build        # first time, or after pulling (this starts the mailpit container too)
```

1. Open the site at http://localhost:8000 and the **mail inbox** at http://localhost:8025 (keep both tabs open).
2. On the site, sign out, go to `/accounts/request-access/`, fill it in and submit.
3. In the inbox you will see "Confirm your email...". Click the link in it.
4. A second email appears, "Access request: <name>", to the approvers (`gmarais@ufl.edu` and every superuser with an email).
   Click its link, sign in as a superuser, and approve. The applicant's "Your access..." email appears in the inbox too.
5. On the sign-in page, **Forgot your password?** sends a reset email the same way.

Nothing leaves your computer, so you can test as much as you like with made-up addresses. You need **a superuser account
with an email address** to approve (My Account → edit the user, or `docker compose run --rm web pixi run python manage.py createsuperuser`).

If the inbox page does not open, `docker compose ps` should list `mailpit` as running; if not, run `docker compose up -d`.
Without the override (for example if you run the site outside Docker) emails are printed in the terminal instead.

The server is different: there the site needs a real SMTP account, see the next sections.

## 2. The settings on the server (`.env.prod`); to test real delivery locally, put them in `.env` (they replace Mailpit)

```
EMAIL_HOST=smtp.example.org          # your SMTP server
EMAIL_PORT=587                       # 587 with TLS is the usual; use 465 only with EMAIL_USE_TLS=false and an SSL-wrapped server
EMAIL_USE_TLS=true
EMAIL_HOST_USER=the-account-that-sends
EMAIL_HOST_PASSWORD=its-password-or-app-password
DEFAULT_FROM_EMAIL=noreply@barkandambrosiagallery.org   # must be an address your SMTP account is allowed to send as
ACCESS_REQUEST_RECIPIENTS=gmarais@ufl.edu                # who is told about new requests (comma separated)
ALLOWED_HOSTS=barkandambrosiagallery.org                 # links in the emails use the address people visited
```

Where to get an SMTP account: your university's outgoing mail (ask UF IT for an SMTP relay or service account),
a Gmail or Microsoft account with an **app password** (normal passwords are refused), or a sending service such as SendGrid,
Mailgun or Amazon SES. Sending from a service needs its sender address verified; for mail that must not land in spam,
also add the SPF and DKIM records the service gives you to the `barkandambrosiagallery.org` DNS.

## 3. Test it

```
docker compose run --rm web pixi run python manage.py send_test_email you@ufl.edu          # local
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec web pixi run python manage.py send_test_email gmarais@ufl.edu   # server
```

It shows how email is configured (never the password), sends one message, and either says it was sent or shows the error.
"The `console` backend does not deliver mail" means `EMAIL_HOST` is not set. Check the spam folder on the first try.

## 4. Who approves, and where

* The approvers are `ACCESS_REQUEST_RECIPIENTS` (default: `gmarais@ufl.edu`) **plus every active superuser that has an email address**.
  Keep the superuser list to people who should be told. Jiri is **not** emailed unless you add his address there or make his account a superuser.
* The email links to **My Account → Access Requests**. Only superusers can open it (and only superusers can see the card).
* A request appears there only **after the applicant has confirmed their email**, so the list is people who own their address.
* Approve as Member or Curator, or deny. Approval activates the account and emails them; denial emails them and frees the username.

## 5. The whole flow, to try once before announcing

1. Sign out. Open `/accounts/request-access/`, fill it in with an address you can read, and submit.
2. Open the confirmation email, click the link ("Email confirmed").
3. You (the approver) get "Access request: <name>". Click its link, sign in as a superuser, approve.
4. The applicant gets "Your access ..." and signs in with the username and password they chose.
5. On the sign-in page use **Forgot your password?** and check the reset email.

If step 2 or 3 never arrives: run `send_test_email`, then check the server log (`docker compose -f docker-compose.yml -f docker-compose.prod.yml logs web | grep -i mail`).
A request is never lost when mail fails: it is kept, and the approval page shows a warning for any request whose approvers could not be emailed.
