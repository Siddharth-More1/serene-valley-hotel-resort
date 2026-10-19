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
https://YOUR_RENDER_HOST/payments/webhook/
```

Use the exact webhook secret configured in Razorpay. Test payments do not collect real money. The project verifies the signature and fetches provider order/payment details before recording success.

## Render deployment

No `render.yaml` is required; configure the web service and database in the Render Dashboard. Use a managed PostgreSQL database and an S3-compatible bucket for media. Render's default web-service filesystem is ephemeral, so do not use local SQLite or local `media/` storage in production. The media bucket remains private and Django emits signed image URLs.

1. Push this project to a Git provider. In Render, create a **PostgreSQL** database. Choose a region and keep it in the same region as the web service.
2. Create a **Web Service** from the repository. Set **Root Directory** to `hotel_resort_management`, runtime to Python, and configure:

   - **Build Command:** `pip install -r requirements.txt && python manage.py collectstatic --no-input`
   - **Start Command:** `gunicorn resort_project.wsgi:application --bind 0.0.0.0:$PORT`

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
   | `DJANGO_HSTS_SECONDS` | `31536000` (default; HTTPS only) |

   Before switching traffic, copy existing files under local `media/` into the bucket with the same relative object keys (for example, `media/rooms/example.png` becomes `rooms/example.png`). This preserves image paths already stored in the database.

   Render supplies `RENDER_EXTERNAL_HOSTNAME`; settings add it to `ALLOWED_HOSTS` and its HTTPS origin to CSRF trusted origins. Include custom domains explicitly in `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` after adding them in Render.

4. Run database migrations before opening the service to customers. From the Render service Shell, run:

   ```sh
   python manage.py migrate --noinput
   python manage.py createsuperuser
   ```

   The second command prompts securely for account details. For paid Render web services, set `python manage.py migrate --noinput` as the pre-deploy command so migrations run before each release. Review migrations before applying them to a database with existing data.

   Render does not provide dashboard Shell or pre-deploy commands for Free web services. For a Free service, run `migrate` and `createsuperuser` from a trusted local machine configured with the database's **External Database URL** and the required production environment variables, or use a paid web-service plan for the dashboard workflow. Do not place manager passwords in the build or start command.

5. Check the deployment logs and visit `/admin/`. Configure the Razorpay webhook URL above in the **Test Mode** dashboard. Confirm a test booking and test payment end to end before changing any payment configuration. Do not switch to live credentials without a separate review and approval.

### Render settings that intentionally fail closed

When `DJANGO_DEBUG=false`, startup requires a secret key, a production hostname, `DATABASE_URL`, complete S3 media credentials, and Razorpay test credentials plus a webhook secret. This prevents a deployment from silently using the local SQLite database, ephemeral media directory, an insecure default key, or live Razorpay credentials.

## Troubleshooting

- **`ImproperlyConfigured` for secret/host/database/media/payment settings:** check that the named environment variables are set on the Render web service, then rebuild/redeploy. Do not print or paste their values into logs or support messages.
- **`DisallowedHost`:** add the hostname (without `https://`) to `DJANGO_ALLOWED_HOSTS`, or verify Render's `RENDER_EXTERNAL_HOSTNAME` is present.
- **CSRF 403 after adding a custom domain:** add its full HTTPS origin to `DJANGO_CSRF_TRUSTED_ORIGINS`, including scheme and port if nonstandard.
- **Static assets missing:** review the build log for `collectstatic` errors and confirm the Render build command runs from this directory.
- **Room images missing:** check the S3 bucket name, region/endpoint, access-key permissions, and Render environment variables. Keep the bucket private; the app signs media URLs.
- **Database connection errors:** use the Render PostgreSQL **Internal Database URL**, ensure the database and service share a region, and confirm `DATABASE_URL` is available to the web service.
- **Payment setup errors:** use paired Razorpay test credentials and the exact matching webhook secret. Live key IDs are rejected.
