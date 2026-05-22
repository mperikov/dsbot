"""Канал Discord только для ссылок на YouTube-видео: остальное удаляется, на валидные — 👍 и 👎."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import discord

logger = logging.getLogger(__name__)

YOUTUBE_CHANNEL_CONFIG_PATH = Path(
    os.getenv("YOUTUBE_CHANNEL_CONFIG_PATH", "youtube_channel_config.json")
)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_THUMBS_UP = "\N{THUMBS UP SIGN}"
_THUMBS_DOWN = "\N{THUMBS DOWN SIGN}"


def load_youtube_channel_config() -> dict[str, int]:
    if not YOUTUBE_CHANNEL_CONFIG_PATH.exists():
        return {}

    with YOUTUBE_CHANNEL_CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        return {}

    result: dict[str, int] = {}
    for guild_id, channel_id in data.items():
        try:
            result[str(guild_id)] = int(channel_id)
        except (TypeError, ValueError):
            continue
    return result


def save_youtube_channel_config(config: dict[str, int]) -> None:
    with YOUTUBE_CHANNEL_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_youtube_channel_id(guild_id: int) -> Optional[int]:
    config = load_youtube_channel_config()
    return config.get(str(guild_id))


def set_youtube_channel_id(guild_id: int, channel_id: int) -> None:
    config = load_youtube_channel_config()
    config[str(guild_id)] = channel_id
    save_youtube_channel_config(config)


def remove_youtube_channel_id(guild_id: int) -> bool:
    config = load_youtube_channel_config()
    existed = str(guild_id) in config
    if existed:
        del config[str(guild_id)]
        save_youtube_channel_config(config)
    return existed


def _video_id_from_youtube_url(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "youtu.be":
        video_id = (parsed.path or "").strip("/").split("/")[0]
        if _VIDEO_ID_RE.fullmatch(video_id or ""):
            return video_id
        return None

    if host not in ("youtube.com", "m.youtube.com", "music.youtube.com"):
        return None

    path = (parsed.path or "").lower().rstrip("/")
    segments = [s for s in path.split("/") if s]

    if path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if video_id and _VIDEO_ID_RE.fullmatch(video_id):
            return video_id
        return None

    if len(segments) >= 2 and segments[0] in ("shorts", "embed", "live"):
        video_id = segments[1]
        if _VIDEO_ID_RE.fullmatch(video_id):
            return video_id

    return None


def is_youtube_video_url(text: str) -> bool:
    """True, если строка целиком — ссылка на одно YouTube-видео."""
    return _video_id_from_youtube_url(text) is not None


def message_is_youtube_video_link(content: str) -> bool:
    stripped = (content or "").strip()
    if not stripped or "\n" in stripped:
        return False
    return is_youtube_video_url(stripped)


async def handle_youtube_channel_message(message: discord.Message) -> None:
    """Удаляет невалидные сообщения; на ссылку YouTube ставит 👍 и 👎."""
    if message.guild is None:
        return

    youtube_channel_id = get_youtube_channel_id(message.guild.id)
    if youtube_channel_id is None or message.channel.id != youtube_channel_id:
        return

    if message_is_youtube_video_link(message.content):
        try:
            await message.add_reaction(_THUMBS_UP)
            await message.add_reaction(_THUMBS_DOWN)
        except discord.HTTPException:
            logger.exception("Failed to add YouTube vote reactions on message %s", message.id)
        return

    try:
        await message.delete()
        logger.info(
            "Deleted non-YouTube message %s in guild %s channel %s",
            message.id,
            message.guild.id,
            message.channel.id,
        )
    except discord.Forbidden:
        logger.warning(
            "No permission to delete message %s in YouTube channel %s",
            message.id,
            message.channel.id,
        )
    except discord.HTTPException:
        logger.exception("Failed to delete non-YouTube message %s", message.id)
