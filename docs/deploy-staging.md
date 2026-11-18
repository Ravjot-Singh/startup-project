# Staging on free tiers (Render + Neon + cron-job.org)

Staging runs with `WHATSAPP_PROVIDER=dryrun`: messages are logged on the Reminders page, nothing is sent.

## Why three services

| Service | Free tier | Job |
|---|---|---|
| Render | Web service, sleeps after 15 min idle | Runs the app |
| Neon | 0.5 GB Postgres | Database (Render's free disk is wiped on every restart) |
| cron-job.org | Unlimited jobs, down to 1 per minute | Calls `POST /jobs/tick` every 10 min. This wakes the app and runs the due reminders. |

## 1. Database (Neon)
1. Sign up at neon.tech and create a project (region: Singapore, closest to India).
2. Copy the **connection string**, e.g. `postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
   - The app turns `postgresql://` into the psycopg driver URL automatically.

## 2. App (Render)
1. Push this repo to GitHub.
2. In Render, go to **New → Blueprint** and pick the repo. It reads `render.yaml`.
3. Fill in the values marked `sync: false`:
   - `DATABASE_URL`: the Neon string.
   - `ADMIN_USERNAME`, `ADMIN_PASSWORD` (10+ characters), `BUSINESS_NAME`, `BUSINESS_UPI_ID`.
4. Render generates `SESSION_SECRET` and `TICK_TOKEN` for you. Copy `TICK_TOKEN` from the service's Environment tab for step 3.
5. Deploy. On first start the app creates the tables and seeds the default reminder rules.
   - With `APP_ENV=production` it **refuses to start** if a secret is weak or missing, and the log says which one.
6. Open `https://<service>.onrender.com/healthz`. It should return `{"ok": true}`.

## 3. Wake-up cron (cron-job.org)
1. Create a job:
   - **URL:** `https://<service>.onrender.com/jobs/tick`
   - **Schedule:** every 10 minutes
   - **Request method:** POST (under Advanced)
   - **Header:** `X-Tick-Token: <TICK_TOKEN from Render>`
   - **Timeout:** 30 s or more. A sleeping free service takes 30-60 s to wake, and the first call after sleep may time out; the next one works.
2. Check the job history. Responses look like:
   - `{"reminders":"waiting until 10:00"}`
   - `{"reminders":"ran","sent":3,...}`
   - `{"reminders":"paused"}`

A tick is safe to run any number of times: each customer gets each reminder at most once a day.

## 4. Check it works
- Log in, import `sample_data/outstanding_sample.csv`, and wait for a tick after the run hour (10:00 IST by default).
- The Reminders page should list the dry-run messages.
- Render logs show a `Tick ...` line on every run.

## Run the tests against Postgres yourself
```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/reminders_test python -m pytest -q
```
Use an **empty, throw-away** database: the tests drop and recreate every table. CI does this on every push (`.github/workflows/ci.yml`).
