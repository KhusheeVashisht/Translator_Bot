import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from deep_translator import GoogleTranslator
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from twitchio.ext import commands


LOG = logging.getLogger("twitch_translator")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

BASE_DIR = Path(__file__).resolve().parent


def load_local_env(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        if not name or name in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[name] = value


load_local_env(BASE_DIR / ".env")
TOKENS_PATH = BASE_DIR / "twitch_tokens.json"
STATE_PATH = BASE_DIR / "oauth_state.json"
SCOPES = ["chat:read", "chat:edit"]
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
PROXY_ENV_VARS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "GIT_HTTP_PROXY",
    "GIT_HTTPS_PROXY",
]


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    twitch_client_id: str = os.getenv("TWITCH_CLIENT_ID", "").strip()
    twitch_client_secret: str = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
    twitch_redirect_uri: str = os.getenv("TWITCH_REDIRECT_URI", "").strip()
    bot_access_token: str = os.getenv("TWITCH_BOT_TOKEN", "").strip()
    bot_refresh_token: str = os.getenv("TWITCH_REFRESH_TOKEN", "").strip()
    bot_channel: str = os.getenv("TARGET_CHANNEL", "missbrainglitch").strip().lower()
    bot_prefix: str = os.getenv("BOT_PREFIX", "!").strip() or "!"
    target_language: str = os.getenv("TARGET_LANGUAGE", "en").strip().lower()
    ignore_commands: bool = env_bool("IGNORE_COMMANDS", True)
    ignore_urls: bool = env_bool("IGNORE_URLS", True)
    translate_english_to_english: bool = env_bool("TRANSLATE_ENGLISH_TO_ENGLISH", False)
    min_message_length: int = int(os.getenv("MIN_MESSAGE_LENGTH", "3"))
    max_message_length: int = int(os.getenv("MAX_MESSAGE_LENGTH", "350"))
    send_cooldown_seconds: float = float(os.getenv("SEND_COOLDOWN_SECONDS", "1.5"))
    translate_timeout_seconds: float = float(os.getenv("TRANSLATE_TIMEOUT_SECONDS", "8"))
    max_parallel_translations: int = int(os.getenv("MAX_PARALLEL_TRANSLATIONS", "3"))
    ignored_users: set[str] = field(
        default_factory=lambda: {
            user.strip().lower()
            for user in os.getenv(
                "IGNORED_USERS",
                "nightbot,streamlabs,streamelements,soundalerts,missbrainglitchbot",
            ).split(",")
            if user.strip()
        }
    )

    @property
    def oauth_ready(self) -> bool:
        return bool(
            self.twitch_client_id and self.twitch_client_secret and self.twitch_redirect_uri
        )


settings = Settings()


def normalize_access_token(token: str) -> str:
    return token.removeprefix("oauth:").strip()


@contextmanager
def cleared_proxy_env() -> Any:
    saved = {name: os.environ.get(name) for name in PROXY_ENV_VARS}
    try:
        for name in PROXY_ENV_VARS:
            os.environ.pop(name, None)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOG.warning("JSON file is invalid: %s", path)
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        return read_json(self.path)

    def save(self, payload: dict[str, Any]) -> None:
        payload["saved_at"] = int(time.time())
        write_json(self.path, payload)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def access_token(self) -> str | None:
        token = normalize_access_token(self.load().get("access_token", ""))
        return token or None


token_store = TokenStore(TOKENS_PATH)


class TranslationService:
    def __init__(self, target_language: str) -> None:
        self.target_language = target_language
        self._translator = GoogleTranslator(source="auto", target=target_language)
        self._semaphore = asyncio.Semaphore(settings.max_parallel_translations)
        self._last_sent_at = 0.0
        self._cache: dict[str, tuple[float, str]] = {}

    async def translate(self, text: str) -> str | None:
        normalized = " ".join(text.split())
        cached = self._cache.get(normalized)
        now = time.time()
        if cached and now - cached[0] < 600:
            return cached[1]

        async with self._semaphore:
            translated = await asyncio.wait_for(
                asyncio.to_thread(self._translate_without_proxy, normalized),
                timeout=settings.translate_timeout_seconds,
            )

        if translated:
            self._cache[normalized] = (now, translated)
        return translated

    def _translate_without_proxy(self, text: str) -> str | None:
        with cleared_proxy_env():
            return self._translator.translate(text)

    async def wait_for_send_window(self) -> None:
        delta = time.time() - self._last_sent_at
        remaining = settings.send_cooldown_seconds - delta
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_sent_at = time.time()


translator_service = TranslationService(settings.target_language)


class TranslatorBot(commands.Bot):
    def __init__(self, token: str) -> None:
        super().__init__(
            token=token,
            prefix=settings.bot_prefix,
            initial_channels=[settings.bot_channel],
        )
        self.connected_at: float | None = None
        self.last_error: str | None = None
        self.last_translation: dict[str, Any] | None = None

    async def event_ready(self) -> None:
        self.connected_at = time.time()
        LOG.info("Bot connected as %s in #%s", getattr(self, "nick", "unknown"), settings.bot_channel)

    async def event_message(self, message) -> None:
        if message.echo:
            return

        author = getattr(message.author, "name", "") or ""
        content = (message.content or "").strip()
        if not self.should_translate(author=author, content=content):
            return

        try:
            translated = await translator_service.translate(content)
            if not translated:
                return
            if not settings.translate_english_to_english and translated.lower() == content.lower():
                return

            response = f"[EN] {author}: {translated}"
            await translator_service.wait_for_send_window()
            await message.channel.send(response)
            self.last_error = None
            self.last_translation = {
                "author": author,
                "original": content,
                "translated": translated,
                "timestamp": int(time.time()),
            }
        except asyncio.TimeoutError:
            self.last_error = "Translation timed out."
            LOG.warning("Translation timed out for user=%s", author)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            LOG.exception("Translation failed")

    @staticmethod
    def should_translate(*, author: str, content: str) -> bool:
        author = author.strip().lower()
        if not author or not content:
            return False
        if author in settings.ignored_users:
            return False
        if settings.ignore_commands and content.startswith(settings.bot_prefix):
            return False
        if settings.ignore_urls and URL_RE.search(content):
            return False
        if len(content) < settings.min_message_length:
            return False
        if len(content) > settings.max_message_length:
            return False
        return True


class BotManager:
    def __init__(self) -> None:
        self.bot: TranslatorBot | None = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.last_start_error: str | None = None

    async def ensure_started(self) -> None:
        async with self.lock:
            if self.task and not self.task.done():
                return

            token = await self._get_start_token()
            if not token:
                self.last_start_error = "No bot token is available yet."
                LOG.warning(self.last_start_error)
                return

            self.bot = TranslatorBot(token=token)
            self.task = asyncio.create_task(self._run_bot(self.bot))

    async def restart(self) -> None:
        async with self.lock:
            await self._stop_locked()
        await self.ensure_started()

    async def _stop_locked(self) -> None:
        if self.bot is not None:
            try:
                await self.bot.close()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Error while closing bot: %s", exc)
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Background bot task ended with error: %s", exc)
        self.bot = None
        self.task = None

    async def _run_bot(self, bot: TranslatorBot) -> None:
        try:
            await bot.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_start_error = f"{type(exc).__name__}: {exc}"
            LOG.exception("Bot crashed")

    async def _get_start_token(self) -> str | None:
        file_payload = token_store.load()
        access_token = normalize_access_token(file_payload.get("access_token") or "")
        if access_token:
            try:
                await validate_token(access_token)
                return access_token
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Saved access token did not validate: %s", exc)

        env_token = normalize_access_token(settings.bot_access_token)
        if env_token:
            try:
                await validate_token(env_token)
                return env_token
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Configured access token did not validate: %s", exc)

        refresh_token = (file_payload.get("refresh_token") or "").strip() or settings.bot_refresh_token
        if refresh_token and settings.oauth_ready:
            try:
                refreshed = await refresh_access_token(refresh_token)
                validation = await validate_token(refreshed["access_token"])
                refreshed["validation"] = validation
                token_store.save(refreshed)
                return refreshed["access_token"]
            except Exception as exc:  # noqa: BLE001
                self.last_start_error = f"Refresh failed: {type(exc).__name__}: {exc}"
                LOG.warning("Refresh token flow failed: %s", exc)

        return env_token or None

    def status(self) -> dict[str, Any]:
        bot = self.bot
        return {
            "channel": settings.bot_channel,
            "task_running": bool(self.task and not self.task.done()),
            "connected_nick": getattr(bot, "nick", None) if bot else None,
            "connected_at": getattr(bot, "connected_at", None) if bot else None,
            "last_error": (bot.last_error if bot else None) or self.last_start_error,
            "last_translation": getattr(bot, "last_translation", None) if bot else None,
            "token_file_present": TOKENS_PATH.exists(),
        }


bot_manager = BotManager()
app = FastAPI(title="Twitch Chat Translator Bot")


def build_authorize_url() -> str:
    if not settings.oauth_ready:
        raise HTTPException(
            status_code=500,
            detail="Set TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, and TWITCH_REDIRECT_URI first.",
        )

    state = secrets.token_urlsafe(24)
    write_json(STATE_PATH, {"state": state, "created_at": int(time.time())})
    params = {
        "client_id": settings.twitch_client_id,
        "redirect_uri": settings.twitch_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "force_verify": "true",
    }
    return f"https://id.twitch.tv/oauth2/authorize?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": settings.twitch_client_id,
                "client_secret": settings.twitch_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.twitch_redirect_uri,
            },
        )
        response.raise_for_status()
        return response.json()


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.twitch_client_id,
                "client_secret": settings.twitch_client_secret,
            },
        )
        response.raise_for_status()
        return response.json()


async def validate_token(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {access_token}"},
        )
        response.raise_for_status()
        return response.json()


@app.on_event("startup")
async def startup_event() -> None:
    await bot_manager.ensure_started()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    status = bot_manager.status()
    login_url = "/auth/twitch/login" if settings.oauth_ready else "Set TWITCH_* env vars first"
    return f"""
    <html>
      <head><title>Twitch Chat Translator Bot</title></head>
      <body style="font-family: Arial, sans-serif; max-width: 820px; margin: 40px auto; line-height: 1.5;">
        <h1>Twitch Chat Translator Bot</h1>
        <p>This app listens to Twitch chat and reposts non-English messages in English.</p>
        <p><strong>Target channel:</strong> {settings.bot_channel}</p>
        <p><strong>Bot running:</strong> {status["task_running"]}</p>
        <p><strong>Connected nickname:</strong> {status["connected_nick"] or "not connected yet"}</p>
        <p><a href="{login_url}">Authorize Twitch Bot Account</a></p>
        <p><a href="/health">Health JSON</a> | <a href="/auth/twitch/validate">Validate Saved Token</a> | <a href="/auth/twitch/refresh">Refresh Saved Token</a></p>
      </body>
    </html>
    """


def build_health_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "app": "twitch-chat-translator-bot",
        "oauth_ready": settings.oauth_ready,
        "redirect_uri": settings.twitch_redirect_uri,
        "bot_status": bot_manager.status(),
    }


@app.api_route("/health", methods=["GET", "HEAD"], response_model=None)
async def health(request: Request) -> dict[str, Any] | Response:
    if request.method == "HEAD":
        return Response(status_code=200)
    return build_health_payload()


@app.get("/auth/twitch/login")
async def twitch_login() -> RedirectResponse:
    return RedirectResponse(build_authorize_url(), status_code=302)


@app.get("/auth/twitch/callback", response_class=HTMLResponse)
async def twitch_callback(code: str | None = None, state: str | None = None, error: str | None = None) -> str:
    if error:
        raise HTTPException(status_code=400, detail=f"Twitch OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth code or state.")

    saved_state = read_json(STATE_PATH).get("state")
    if not saved_state or saved_state != state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch.")

    token_payload = await exchange_code_for_token(code)
    access_token = token_payload["access_token"]
    validation = await validate_token(access_token)
    token_payload["validation"] = validation
    token_store.save(token_payload)
    await bot_manager.restart()

    login = validation.get("login", "unknown")
    scopes = ", ".join(validation.get("scopes", []))
    return f"""
    <html>
      <head><title>Twitch Authorization Complete</title></head>
      <body style="font-family: Arial, sans-serif; max-width: 820px; margin: 40px auto; line-height: 1.5;">
        <h1>Authorization Complete</h1>
        <p>The bot token has been saved and the bot restart was triggered.</p>
        <p><strong>Authorized Twitch account:</strong> {login}</p>
        <p><strong>Scopes:</strong> {scopes or "none returned"}</p>
        <p><strong>Target channel:</strong> {settings.bot_channel}</p>
        <p><a href="/health">Open health status</a></p>
      </body>
    </html>
    """


@app.get("/auth/twitch/validate")
async def twitch_validate() -> dict[str, Any]:
    token = token_store.access_token() or normalize_access_token(settings.bot_access_token)
    if not token:
        raise HTTPException(status_code=404, detail="No saved bot token found.")
    return await validate_token(token)


@app.get("/auth/twitch/refresh")
async def twitch_refresh() -> dict[str, Any]:
    payload = token_store.load()
    refresh_token = (payload.get("refresh_token") or "").strip() or settings.bot_refresh_token
    if not refresh_token:
        raise HTTPException(status_code=404, detail="No refresh token found.")
    if not settings.oauth_ready:
        raise HTTPException(status_code=500, detail="OAuth client settings are not configured.")

    refreshed = await refresh_access_token(refresh_token)
    validation = await validate_token(refreshed["access_token"])
    refreshed["validation"] = validation
    token_store.save(refreshed)
    await bot_manager.restart()
    return {
        "ok": True,
        "login": validation.get("login"),
        "scopes": validation.get("scopes", []),
        "target_channel": settings.bot_channel,
    }
