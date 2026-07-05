# YouTube AI Moderator

Production-ready FastAPI service for moderating YouTube live chat with local spam scoring, Gemini JSON moderation, YouTube Live Streaming API actions, a Bootstrap admin dashboard, SQLite persistence, WebSocket live updates, Docker support, and deployment-friendly configuration.

## What it does

- Finds the active livestream and live chat for the authenticated channel.
- Polls YouTube live chat, reconnects after failures, and stores every message.
- Scores spam locally for repeated messages, flooding, links, scams, invites, emoji spam, caps, punctuation, profanity, and custom keywords.
- Sends suspicious messages to Gemini and requires strict JSON decisions.
- Applies allow, warn, delete, timeout, or ban actions using YouTube APIs when `AUTO_MODERATE=true`.
- Tracks users, warnings, timeouts, bans, spam history, AI history, logs, audit events, API usage, health, and analytics.
- Provides dashboard, live chat, moderation queue, logs, settings, statistics, user lookup, analytics, CSV export, SQLite backup, restore, Discord notifications, and WebSocket updates.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the generated secret in `SECRET_KEY`, set `ADMIN_PASSWORD`, then run:

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000 for the public site, then sign in at `/login` with `ADMIN_USERNAME` and `ADMIN_PASSWORD`.

The app runs without Google credentials so you can inspect the dashboard locally. Live moderation starts after Gemini and YouTube credentials are configured.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Signed session cookie secret. Change before deployment. |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Initial admin account created on first boot. |
| `DATABASE_URL` | Defaults to `sqlite:///./moderator.db`. |
| `GEMINI_API_KEY` | Gemini API key. |
| `GEMINI_MODEL` | Defaults to `gemini-3.5-flash`. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | OAuth web client credentials. |
| `GOOGLE_REFRESH_TOKEN` | Optional refresh token. The UI OAuth flow can store one for you. |
| `CHANNEL_ID` | Optional channel fallback used when `liveBroadcasts.list` finds no active broadcast. |
| `YOUTUBE_SETUP_TOKEN` | Secret token for the friend-facing `/auth/login` YouTube authorization link. |
| `DISCORD_WEBHOOK` | Optional Discord webhook for alerts. |
| `AUTO_MODERATE` | Apply YouTube actions automatically. Set `false` for review-only mode. |

## Google Cloud setup

1. Create or select a Google Cloud project.
2. Enable the **YouTube Data API v3**.
3. Configure the OAuth consent screen.
4. Create an OAuth 2.0 **Web application** client.
5. Add this authorized redirect URI:

```text
http://localhost:8000/auth/youtube/callback
http://localhost:8000/auth/callback
```

For production, replace localhost with your deployed `BASE_URL`.

Required scopes:

```text
https://www.googleapis.com/auth/youtube.force-ssl
https://www.googleapis.com/auth/youtube.readonly
```

Add the client ID and client secret to `.env`, start the app, sign in, and click **OAuth** or **Connect YouTube**.

## Friend YouTube authorization

If the YouTube channel belongs to a friend, do not ask for their Google password. Use the built-in friend authorization route:

1. Regenerate your Google OAuth client secret if it was shared in chat, screenshots, or logs.
2. Set these values in `.env`:

```env
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-new-google-client-secret
BASE_URL=https://your-deployed-app.example
YOUTUBE_SETUP_TOKEN=make-a-long-random-token
```

3. Add this redirect URI in Google Cloud:

```text
https://your-deployed-app.example/auth/callback
```

4. Send your friend only this link:

```text
https://your-deployed-app.example/auth/login?token=make-a-long-random-token
```

After your friend signs in and approves access, the app stores the refresh token and their YouTube channel ID in SQLite. The live-chat worker uses that stored channel automatically, so your friend does not need to send a refresh token manually.

For local testing with localhost, use:

```text
http://localhost:8000/auth/callback
http://localhost:8000/auth/login?token=make-a-long-random-token
```

## Gemini setup

1. Create a Gemini API key in Google AI Studio.
2. Add it to `.env` as `GEMINI_API_KEY`.
3. Keep `GEMINI_MODEL=gemini-3.5-flash` unless you intentionally choose another stable Flash model.

Gemini is only called for suspicious messages by default. Tune `AI min score`, thresholds, and filters in Settings.

## Running with Docker

```bash
cp .env.example .env
docker compose up --build
```

Production startup command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

## Render

Recommended: deploy with the included `render.yaml` Blueprint.

1. Push this folder to GitHub.
2. In Render, choose **New → Blueprint** and select the repo.
3. Render will create a web service with:

- Build command: `pip install -r requirements.txt`
- Start command: `python run.py`
- Health check: `/health`
- Persistent disk mounted at `/data`
- SQLite database at `sqlite:////data/moderator.db`

Set these Render environment variables after the service is created:

- `BASE_URL`: your Render URL, for example `https://comment-guardian.onrender.com`
- `ADMIN_PASSWORD`
- `GEMINI_API_KEY`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- optional `DISCORD_WEBHOOK`

Render generates `SECRET_KEY` and `YOUTUBE_SETUP_TOKEN` automatically from `render.yaml`.

Manual Web Service setup also works:

- Build command: `pip install -r requirements.txt`
- Start command: `python run.py`
- Add all environment variables in the Render dashboard.
- Set `BASE_URL` to the Render service URL.

Use a persistent disk if you keep SQLite. Without a disk, Render restarts can erase the database. For higher traffic, move `DATABASE_URL` to Postgres and keep the SQLAlchemy models unchanged.

## Railway

Create a Python service from this repo/folder and set:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Add environment variables in Railway. Set `BASE_URL` to the public Railway domain.

## Replit

Install dependencies from `requirements.txt`, add secrets in Replit Secrets, and run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Set `BASE_URL` to the public Replit URL before starting YouTube OAuth.

## Linux VPS

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv nginx
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Put Nginx or Caddy in front of Uvicorn for TLS, set `SECURE_COOKIES=true`, and use a process manager such as systemd.

## Moderation behavior

Default local thresholds:

- Score `<20`: allow
- Score `20-39`: warn
- Score `40-70`: delete
- Score `>70`: timeout
- Severe hate speech, threats, scams, or phishing from Gemini can escalate to timeout or ban.
- Repeated offenders escalate from warn to delete to timeout to ban.

YouTube actions:

- `delete`: `liveChat/messages.delete`
- `timeout`: delete message, then temporary `liveChat/bans.insert`
- `ban`: permanent `liveChat/bans.insert`
- `warn`: stored locally, optionally sent to chat when `SEND_WARNING_MESSAGES=true`

## Security notes

- Change `SECRET_KEY` and `ADMIN_PASSWORD`.
- Use HTTPS and `SECURE_COOKIES=true` in production.
- Keep `.env`, SQLite databases, logs, and backups out of source control.
- The admin UI uses signed sessions, PBKDF2 password hashing, CSRF checks, input validation, and rate limiting.
- API keys and OAuth tokens are never rendered in templates.

## Troubleshooting

- **No livestream found**: verify the channel is live, the OAuth account owns or moderates the channel, and `CHANNEL_ID` is correct.
- **OAuth redirect mismatch**: `BASE_URL` must match the Google Cloud authorized redirect URI exactly.
- **403 from YouTube**: confirm the YouTube Data API v3 is enabled and the OAuth scopes include `youtube.force-ssl`.
- **Gemini skipped**: set `GEMINI_API_KEY`; lower `AI min score` if messages are not suspicious enough.
- **SQLite locked**: avoid frequent backup/restore during active live streams; use Postgres for high write volume.
- **Dashboard loads but no live messages**: check `/health`, logs, and the connection pill in the navbar.

## Verified API references

- Gemini model guidance: https://ai.google.dev/gemini-api/docs/models
- Gemini 3.5 Flash: https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash
- YouTube live chat list polling: https://developers.google.com/youtube/v3/live/docs/liveChatMessages/list
- YouTube live broadcasts and `snippet.liveChatId`: https://developers.google.com/youtube/v3/live/docs/liveBroadcasts
- YouTube live chat bans: https://developers.google.com/youtube/v3/live/docs/liveChatBans/insert
