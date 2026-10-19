# Serene Valley Hotel & Resort

Django booking site with SQLite for local development, PostgreSQL for production, Razorpay **TEST MODE** payments, WhiteNoise static files, and S3-compatible persistent room-image storage.

## Local setup (Windows PowerShell)

From this directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally. Keep `DJANGO_DEBUG=true`, leave `DATABASE_URL` blank to use `db.sqlite3`, and optionally set a stable random `DJANGO_SECRET_KEY` so local sessions survive restarts. The development settings generate a temporary key when it is blank. Razorpay and S3 values can remain blank locally unless you are testing those integrations.

Initialize the local database and run Django:

```powershell
python manage.py migrate
python manage.py runserver
```

## Migrations, tests, and static files

```powershell
python manage.py makemigrations --check --dry-run
python manage.py check
python manage.py test
python manage.py collectstatic --noinput
```

`makemigrations --check --dry-run` only checks for model changes. `migrate` changes the selected database schema; back up a database before applying migrations to a database containing important data.

## Manager and administrator accounts

Create the first administrator interactively after migrations:

```powershell
python manage.py createsuperuser
```

Django prompts for the username, email, and password without placing the password in shell history. A superuser can access the manager dashboard. To create a non-superuser manager later, sign in to Django Admin as the superuser, create a user with a unique password, set **Role** to **Manager**, and enable **Staff status**. Do not share the superuser account with routine hotel staff.

## Razorpay test mode

Use only the Razorpay Dashboard's **Test Mode** key ID, key secret, and webhook secret. Put them in local `.env` for development; enter them in Render's environment settings for deployment. The key ID must begin with `rzp_test_`; production settings reject live key IDs. Never put credentials in templates, Git, or a command line. Configure the Razorpay test webhook to call:

```text
https://YOUR_RENDER_HOST/payments/razorpay/webhook/
```

The webhook route is `/payments/razorpay/webhook/`.

Use the exact webhook secret configured in Razorpay. Test payments do not collect real money. The project verifies the signature and fetches provider order/payment details before recording success.

## Render deployment

No `render.yaml` is required; configure the web service and database in the Render Dashboard. Use a managed PostgreSQL database and an S3-compatible bucket for media. Render's default web-service filesystem is ephemeral, so do not use local SQLite or local `media/` storage in production. The media bucket remains private and Django emits signed image URLs.

1. Push this project to a Git provider. In Render, create a **PostgreSQL** database. Choose a region and keep it in the same region as the web service.
2. Create a **Web Service** from the repository. Set **Root Directory** to `hotel_resort_management`, runtime to Python, and configure these commands separately:

   - **Build Command:** `pip install -r requirements.txt && python manage.py check --deploy --tag security && python manage.py collectstatic --noinput`
   - **Pre-Deploy Command:** `python manage.py migrate --noinput`
   - **Start Command:** `gunicorn resort_project.wsgi:application --bind 0.0.0.0:$PORT`

   The build installs dependencies, checks deployment settings, and collects static assets. Run schema migrations in Render's pre-deploy command, after the build and before the new release starts. Do not run migrations in the build or start command. Render provides pre-deploy commands for paid web services, private services, and background workers. If your selected web-service plan does not support a pre-deploy command, use a reviewed, trusted one-off maintenance procedure against the intended database; do not put migration commands in the server startup command.

3. Create a private S3-compatible bucket (for example, AWS S3 or Cloudflare R2) and an access key limited to that bucket. For R2, set the endpoint URL and region `auto`; for AWS, set the bucket region and leave the endpoint URL blank. Add these environment variables to the Render web service:

   | Key | Value |
   | --- | --- |
   | `DJANGO_DEBUG` | `false` |
   | `DJANGO_SECRET_KEY` | Use Render's **Generate** option; do not reuse a development key |
   | `DJANGO_ALLOWED_HOSTS` | Optional when using the Render hostname (auto-detected); otherwise comma-separated hostnames without schemes |
   | `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://YOUR_RENDER_HOST` and any custom domain origins, comma-separated |
   | `DATABASE_URL` | The database's **Internal Database URL** from Render |
   | `RAZORPAY_KEY_ID` | Test-mode key ID beginning `rzp_test_` |
   | `RAZORPAY_KEY_SECRET` | Razorpay test-mode key secret |
   | `RAZORPAY_WEBHOOK_SECRET` | Secret configured for the Razorpay test webhook |
   | `AWS_STORAGE_BUCKET_NAME` | Private media bucket name |
   | `AWS_ACCESS_KEY_ID` | Bucket-scoped access key |
   | `AWS_SECRET_ACCESS_KEY` | Bucket-scoped secret key |
   | `AWS_S3_REGION_NAME` | Bucket region (use `auto` for R2) |
   | `AWS_S3_ENDPOINT_URL` | R2 S3 endpoint URL; leave empty for AWS S3 |
   | `DJANGO_SECURE_SSL_REDIRECT` | `true` (default) |
   | `DJANGO_HSTS_SECONDS` | `300` initially; increase only after confirming HTTPS for every intended domain |

   The initial HSTS policy is five minutes, without subdomain scope or preload. Keep HTTPS redirect and secure cookies enabled. After confirming all intended domains and subdomains are HTTPS-only, increase the duration deliberately. Do not enable preload casually: browsers can retain that policy for a long time, and `includeSubDomains` affects every subdomain. Django's deployment check will warn that subdomain HSTS and preload are disabled; these are intentional during initial rollout and should be revisited after domain verification.

   Before switching traffic, review the existing files under local `media/` and confirm each is an approved, non-private portfolio asset. Copy only approved room images into the private bucket while preserving their relative object keys (for example, `media/rooms/example.png` becomes `rooms/example.png`); this preserves paths already stored in database records. Do not upload `rooms/Gemini_Generated_Image_46b4mn46b4mn46b4.png`, the unrelated 7 MB Python practice graphic, unless the project owner explicitly approves it. Keep the bucket private; Django serves media through signed URLs. No bucket or credentials are created by this project setup.

   Render supplies `RENDER_EXTERNAL_HOSTNAME`; settings add it to `ALLOWED_HOSTS` and its HTTPS origin to CSRF trusted origins. Include custom domains explicitly in `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` after adding them in Render.

4. Run database migrations before opening the service to customers. Prefer the pre-deploy command above, which runs `python manage.py migrate --noinput` against the service's configured PostgreSQL database before the release. Review migrations and back up existing production data before applying schema changes. Migrations change schema; they do not transfer records from the local SQLite database.

   Create the first administrator through a trusted interactive session after migrations, for example with `python manage.py createsuperuser`. Never put administrator credentials in build or start commands. If local SQLite records must be retained, plan and verify a separate export/import into PostgreSQL; do not assume migrations copy data.

5. Check the deployment logs and visit `/admin/`. Configure `https://YOUR_RENDER_HOST/payments/razorpay/webhook/` in the Razorpay **Test Mode** dashboard. Confirm a test booking and test payment end to end before changing any payment configuration. Do not switch to live credentials without a separate review and approval.

### Render settings that intentionally fail closed

When `DJANGO_DEBUG=false`, startup requires `DJANGO_SECRET_KEY`, either `RENDER_EXTERNAL_HOSTNAME` or `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, S3 bucket name and access credentials, paired Razorpay Test Mode key ID/key secret, and `RAZORPAY_WEBHOOK_SECRET`. The Render hostname is added automatically to allowed hosts and its HTTPS origin to CSRF trusted origins. Configure custom hostnames and HTTPS origins explicitly. `AWS_S3_REGION_NAME` defaults to `us-east-1`; set it for the chosen bucket. `AWS_S3_ENDPOINT_URL` is optional for AWS and required for providers such as R2.

### Environment variable reference

Copy `.env.example` to `.env` for local development. Its values are placeholders, not deployable secrets. Configure production values in the Render service's environment settings or secret manager.

| Variable | Production requirement |
| --- | --- |
| `DJANGO_DEBUG` | Required: `false` |
| `DJANGO_SECRET_KEY` | Required: stable, private key |
| `DJANGO_ALLOWED_HOSTS` | Required only if Render hostname auto-detection is unavailable; include custom hostnames without schemes |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Needed for custom domains; full HTTPS origins. Render hostname is added automatically |
| `DATABASE_URL` | Required: Render PostgreSQL Internal Database URL |
| `DJANGO_SECURE_SSL_REDIRECT` | Optional; defaults to `true` in production |
| `DJANGO_HSTS_SECONDS` | Optional; defaults to `300` during initial rollout |
| `RAZORPAY_KEY_ID` | Required: Test Mode key ID beginning `rzp_test_` |
| `RAZORPAY_KEY_SECRET` | Required: matching Test Mode secret |
| `RAZORPAY_WEBHOOK_SECRET` | Required: secret configured for the Test Mode webhook |
| `AWS_STORAGE_BUCKET_NAME` | Required: private media bucket |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Required: bucket-scoped credentials |
| `AWS_S3_REGION_NAME` | Optional; defaults to `us-east-1` (use the provider's configured region, such as `auto` for R2) |
| `AWS_S3_ENDPOINT_URL` | Optional for AWS S3; set the S3-compatible endpoint when required |
| `HOTEL_NAME`, `HOTEL_ADDRESS`, `HOTEL_EMAIL`, `HOTEL_CONTACT_PHONE`, `HOTEL_GSTIN` | Optional receipt/invoice branding |
| `DJANGO_MEDIA_ROOT` | Optional local media-directory override; production media uses S3 |

## Troubleshooting

- **`ImproperlyConfigured` for secret/host/database/media/payment settings:** check that the named environment variables are set on the Render web service, then rebuild/redeploy. Do not print or paste their values into logs or support messages.
- **`DisallowedHost`:** add the hostname (without `https://`) to `DJANGO_ALLOWED_HOSTS`, or verify Render's `RENDER_EXTERNAL_HOSTNAME` is present.
- **CSRF 403 after adding a custom domain:** add its full HTTPS origin to `DJANGO_CSRF_TRUSTED_ORIGINS`, including scheme and port if nonstandard.
- **Static assets missing:** review the build log for `collectstatic` errors and confirm the Render build command runs from this directory.
- **Room images missing:** check the S3 bucket name, region/endpoint, access-key permissions, and Render environment variables. Keep the bucket private; the app signs media URLs.
- **Database connection errors:** use the Render PostgreSQL **Internal Database URL**, ensure the database and service share a region, and confirm `DATABASE_URL` is available to the web service.
- **Payment setup errors:** use paired Razorpay test credentials and the exact matching webhook secret. Live key IDs are rejected.
