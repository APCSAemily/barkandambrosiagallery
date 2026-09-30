# After deploying: what to do once

Deploying `main` (publish a release, as usual) applies the database migrations by itself and deletes nothing.
Two things need a person, once. A script does both:

```
ssh you@server
cd /opt/barkandambrosiagallery
bash scripts/post_deploy_setup.sh
```

1. **Email for "Request access".** The script checks `.env.prod` for `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and
   `DEFAULT_FROM_EMAIL`, asks for any that are missing, saves them and restarts the web container. You need an SMTP account
   (university mail, Gmail with an app password, SendGrid ...). Without it **nobody can finish asking for an account or reset a
   password**, because both work through an emailed link. Send yourself a test request before announcing it.
2. **The published interactions dataset** is loaded into the database (`import_pathogen_interactions`). The review page needs it
   to tell reviewers when a claim is already published. It is safe to run again and never touches accepted or uploaded rows.

It also lists who can approve access requests. **Every superuser can**, and every superuser with an email address is emailed when a
request arrives, in addition to `ACCESS_REQUEST_RECIPIENTS` (default `gmarais@ufl.edu`). Make someone a superuser under
*My Account → edit user → role*.
