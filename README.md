# Twitch Translator Bot

A deployable Twitch chat translator that listens in one channel and reposts viewer messages in English using a separate bot account.

For local development, copy `.env.example` to `.env` in the project root and fill in the Twitch values you want to use. The app now loads that file automatically on startup.

## Goal

This project is set up for:

- Streamer channel: `missbrainglitch`
- Bot account: `missbrainglitchbot`

That means the bot logs into Twitch as `missbrainglitchbot`, joins `missbrainglitch` chat, and posts English translations there.

## Features

- FastAPI web app for deployment on Render
- Twitch OAuth login flow for the bot account
- Real callback endpoint for Twitch app registration
- Health endpoint for deployment checks
- Token validation and refresh endpoints
- Translation filtering to reduce spam in real streams
- Cooldown handling so the bot does not flood chat

## Files

- `app.py`: main FastAPI app and Twitch chat bot logic
- `chat_translator.py`: app entrypoint for Render
- `requirements.txt`: Python dependencies
- `render.yaml`: Render service definition
- `.env.example`: environment variable template

## Deploy On Render

1. Create a new Web Service from this repository.
2. Add the environment variables from `.env.example`.
3. Set `TARGET_CHANNEL=missbrainglitch`.
4. Set `TWITCH_REDIRECT_URI` to:

   `https://YOUR-RENDER-SERVICE.onrender.com/auth/twitch/callback`

5. In the Twitch Developer Console, register an app and use the exact same redirect URI.
6. Open:

   `https://YOUR-RENDER-SERVICE.onrender.com/auth/twitch/login`

7. Log in as `missbrainglitchbot` and approve access.

## Run Locally

1. Create a `.env` file from `.env.example`.
2. Install dependencies with `pip install -r requirements.txt`.
3. Start the app with `python chat_translator.py`.
4. Open `http://127.0.0.1:8000/` in your browser.

If you do not set Twitch credentials yet, the web app still starts and the health page will report that OAuth is not ready.

## Important Environment Variables

```env
TWITCH_CLIENT_ID=your_twitch_client_id
TWITCH_CLIENT_SECRET=your_twitch_client_secret
TWITCH_REDIRECT_URI=https://your-render-service.onrender.com/auth/twitch/callback
TWITCH_BOT_TOKEN=your_bot_access_token
TWITCH_REFRESH_TOKEN=your_bot_refresh_token
TARGET_CHANNEL=missbrainglitch
BOT_PREFIX=!
TARGET_LANGUAGE=en
IGNORE_COMMANDS=true
IGNORE_URLS=true
MIN_MESSAGE_LENGTH=3
MAX_MESSAGE_LENGTH=350
SEND_COOLDOWN_SECONDS=1.5
TRANSLATE_TIMEOUT_SECONDS=8
MAX_PARALLEL_TRANSLATIONS=3
IGNORED_USERS=nightbot,streamlabs,streamelements,soundalerts,missbrainglitchbot
```

## Endpoints

- `/`: basic app page
- `/health`: deployment and bot status
- `/auth/twitch/login`: starts Twitch OAuth
- `/auth/twitch/callback`: Twitch OAuth callback
- `/auth/twitch/validate`: validates the current saved token
- `/auth/twitch/refresh`: refreshes an expired saved token
