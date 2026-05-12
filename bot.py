import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import map_channel
from killfeed_renderer import (
    _format_distance_whole_meters,
    ensure_killfeed_layout_config,
    ensure_killfeed_template,
    render_killfeed_card,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reaction-role-bot")

load_dotenv()


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable '{name}' is required.")
    return value


TOKEN = _get_required_env("BOT_TOKEN")
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "reaction_role_config.json"))
STATUS_CONFIG_PATH = Path(os.getenv("STATUS_CONFIG_PATH", "status_channel_config.json"))
KILLFEED_CONFIG_PATH = Path(os.getenv("KILLFEED_CONFIG_PATH", "killfeed_channel_config.json"))
KILLFEED_BASE_IMAGE_PATH = Path(os.getenv("KILLFEED_BASE_IMAGE_PATH", "assets/killfeed_base.png"))
KILLFEED_LAYOUT_PATH = Path(os.getenv("KILLFEED_LAYOUT_PATH", "killfeed_layout.json"))
KILLFEED_ASSETS_DIR = Path(os.getenv("KILLFEED_ASSETS_DIR", "assets"))
KILLFEED_WEAPONS_DIR = Path(os.getenv("KILLFEED_WEAPONS_DIR", str(KILLFEED_ASSETS_DIR / "weapons")))
KILLFEED_WEAPON_GROUPS_PATH = Path(os.getenv("KILLFEED_WEAPON_GROUPS_PATH", "killfeed_weapon_groups.json"))
SYNC_GUILD_ID = os.getenv("SYNC_GUILD_ID")


intents = discord.Intents.default()
intents.guilds = True
intents.members = False
intents.reactions = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
STATUS_TIMEOUT_SECONDS = 120
_last_players_update_monotonic: Optional[float] = None

MESSAGE_LINK_RE = re.compile(
    r"^https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)$"
)
SERVER_STARTED_RE = re.compile(
    r"Server\s+.+?\s+successfully started,\s*player connect enabled",
    re.IGNORECASE,
)
KILLFEED_RE = re.compile(
    r"\*{0,2}Player Activity:\*{0,2}\s*(?P<activity_summary>.*?)\s+Player:\s*\((?P<player>[^)]+)\).*?"
    r"killed by:\s*Player:\s*\((?P<killer>[^)]+)\).*?"
    r"with\s*\[(?P<gun>[^\]]+)\]\s*from\s*\[(?P<distance>[^\]]+)\]\s*meters",
    re.IGNORECASE | re.DOTALL,
)

# Steam community profile / vanity URLs as typically pasted in Player Activity.
STEAM_PROFILE_URL_RE = re.compile(
    r"https?://steamcommunity\.com/(?:profiles/\d+|id/[^\s\)\]<>]+)",
    re.IGNORECASE,
)


def _discord_markdown_link_escape_label(label: str) -> str:
    r"""Escape [, ], \\ for use inside Discord markdown [label](url)."""
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _steam_profile_urls_in_order(text: str) -> list[str]:
    """Unique Steam URLs in left-to-right order (from Player Activity block)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in STEAM_PROFILE_URL_RE.finditer(text or ""):
        u = m.group(0).rstrip("/")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _name_with_optional_steam_link(display: str, url: str | None) -> str:
    r"""Use [nick](<link>) when a Steam URL exists; else plain text (e.g. Discord mentions)."""
    d = display.strip()
    if not url:
        return d
    return f"[{_discord_markdown_link_escape_label(d)}](<{url}>)"


def _normalize_emoji_string(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("<:") or cleaned.startswith("<a:"):
        inner = cleaned[2:-1] if cleaned.startswith("<:") else cleaned[3:-1]
        parts = inner.split(":")
        if len(parts) == 2:
            return f"{parts[0]}:{parts[1]}"
    return cleaned


def _emoji_matches(payload_emoji: discord.PartialEmoji, configured_emoji: str) -> bool:
    configured = _normalize_emoji_string(configured_emoji)
    payload_repr = str(payload_emoji)

    if configured == payload_repr:
        return True

    if payload_emoji.id is not None:
        short = f"{payload_emoji.name}:{payload_emoji.id}"
        return configured == short

    return False


def _load_config() -> dict[str, dict[str, str | int]]:
    if not CONFIG_PATH.exists():
        return {}

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        return {}
    return data


def _load_status_config() -> dict[str, int]:
    if not STATUS_CONFIG_PATH.exists():
        return {}

    with STATUS_CONFIG_PATH.open("r", encoding="utf-8") as f:
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


def _save_status_config(config: dict[str, int]) -> None:
    with STATUS_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _load_killfeed_config() -> dict[str, dict[str, int]]:
    if not KILLFEED_CONFIG_PATH.exists():
        return {}

    with KILLFEED_CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        return {}

    result: dict[str, dict[str, int]] = {}
    for guild_id, channels in data.items():
        if not isinstance(channels, dict):
            continue

        source_channel_id = channels.get("source_channel_id")
        target_channel_id = channels.get("target_channel_id")
        try:
            result[str(guild_id)] = {
                "source_channel_id": int(source_channel_id),
                "target_channel_id": int(target_channel_id),
            }
        except (TypeError, ValueError):
            continue
    return result


def _normalize_weapon_key(weapon_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", weapon_name.lower())


def ensure_killfeed_weapon_groups_config(config_path: Path) -> None:
    if config_path.exists():
        return
    default_cfg = {
        "default_base_image": str(KILLFEED_BASE_IMAGE_PATH).replace("\\", "/"),
        "groups": [
            {
                "name": "sniper",
                "base_image": "assets/killfeed_base.png",
                "weapons": ["M82", "AX50", "DVL-10", "GM6-LYNX"],
            }
        ],
    }
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(default_cfg, f, ensure_ascii=False, indent=2)


def _resolve_group_base_image(path_value: str, groups_path: Path) -> Path:
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    return (groups_path.parent / candidate).resolve()


def _load_weapon_group_base_images(config_path: Path) -> tuple[Path, dict[str, Path]]:
    default_base = KILLFEED_BASE_IMAGE_PATH
    mapping: dict[str, Path] = {}
    if not config_path.exists():
        return default_base, mapping
    try:
        with config_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Invalid killfeed weapon groups config: %s", config_path)
        return default_base, mapping
    if not isinstance(raw, dict):
        return default_base, mapping

    default_raw = raw.get("default_base_image")
    if isinstance(default_raw, str) and default_raw.strip():
        default_base = _resolve_group_base_image(default_raw.strip(), config_path)

    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, list):
        return default_base, mapping

    for group in groups_raw:
        if not isinstance(group, dict):
            continue
        base_raw = group.get("base_image")
        weapons_raw = group.get("weapons")
        if not isinstance(base_raw, str) or not base_raw.strip() or not isinstance(weapons_raw, list):
            continue
        base_path = _resolve_group_base_image(base_raw.strip(), config_path)
        for weapon in weapons_raw:
            if not isinstance(weapon, str) or not weapon.strip():
                continue
            key = _normalize_weapon_key(weapon)
            if key:
                mapping[key] = base_path
    return default_base, mapping


def _killfeed_base_image_for_weapon(weapon_name: str) -> Path:
    default_base, mapping = _load_weapon_group_base_images(KILLFEED_WEAPON_GROUPS_PATH)
    key = _normalize_weapon_key(weapon_name)
    chosen = mapping.get(key, default_base)
    if chosen.exists():
        return chosen
    logger.warning("Killfeed base image not found, fallback to default: %s", chosen)
    return KILLFEED_BASE_IMAGE_PATH


def _save_killfeed_config(config: dict[str, dict[str, int]]) -> None:
    with KILLFEED_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _save_config(config: dict[str, dict[str, str | int]]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _get_guild_binding(guild_id: int) -> Optional[dict[str, str | int]]:
    config = _load_config()
    return config.get(str(guild_id))


def _get_status_channel_id(guild_id: int) -> Optional[int]:
    config = _load_status_config()
    return config.get(str(guild_id))


def _set_status_channel_id(guild_id: int, channel_id: int) -> None:
    config = _load_status_config()
    config[str(guild_id)] = channel_id
    _save_status_config(config)


def _remove_status_channel_id(guild_id: int) -> bool:
    config = _load_status_config()
    existed = str(guild_id) in config
    if existed:
        del config[str(guild_id)]
        _save_status_config(config)
    return existed


def _get_killfeed_channels(guild_id: int) -> Optional[dict[str, int]]:
    config = _load_killfeed_config()
    return config.get(str(guild_id))


def _set_killfeed_channels(guild_id: int, source_channel_id: int, target_channel_id: int) -> None:
    config = _load_killfeed_config()
    config[str(guild_id)] = {
        "source_channel_id": source_channel_id,
        "target_channel_id": target_channel_id,
    }
    _save_killfeed_config(config)


def _remove_killfeed_channels(guild_id: int) -> bool:
    config = _load_killfeed_config()
    existed = str(guild_id) in config
    if existed:
        del config[str(guild_id)]
        _save_killfeed_config(config)
    return existed


def _set_guild_binding(guild_id: int, message_id: int, role_id: int, emoji: str) -> None:
    config = _load_config()
    config[str(guild_id)] = {
        "message_id": message_id,
        "role_id": role_id,
        "emoji": _normalize_emoji_string(emoji),
    }
    _save_config(config)


def _remove_guild_binding(guild_id: int) -> bool:
    config = _load_config()
    existed = str(guild_id) in config
    if existed:
        del config[str(guild_id)]
        _save_config(config)
    return existed


async def _resolve_member(payload: discord.RawReactionActionEvent) -> Optional[discord.Member]:
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return None

    try:
        return await guild.fetch_member(payload.user_id)
    except discord.NotFound:
        return None


async def _get_role(payload: discord.RawReactionActionEvent, role_id: int) -> Optional[discord.Role]:
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return None
    return guild.get_role(role_id)


def _get_payload_binding(payload: discord.RawReactionActionEvent) -> Optional[dict[str, str | int]]:
    if payload.guild_id is None:
        return None
    binding = _get_guild_binding(payload.guild_id)
    if not binding:
        return None
    return binding


def _parse_message_reference(message_ref: str) -> tuple[Optional[int], Optional[int]]:
    cleaned = message_ref.strip()
    if cleaned.isdigit():
        return None, int(cleaned)

    match = MESSAGE_LINK_RE.match(cleaned)
    if not match:
        return None, None

    guild_id, channel_id, message_id = match.groups()
    if guild_id == "@me":
        return None, None
    return int(channel_id), int(message_id)


async def _safe_send_interaction_message(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except discord.NotFound:
        logger.warning("Interaction expired before response could be sent.")
    except discord.HTTPException as exc:
        # 40060 means the interaction has already been acknowledged.
        if exc.code == 40060:
            logger.warning("Interaction already acknowledged before response send.")
            return
        raise


@bot.event
async def on_ready() -> None:
    global _last_players_update_monotonic
    try:
        await bot.change_presence(activity=discord.Game(name="offline"))
    except discord.HTTPException:
        logger.exception("Failed to set default bot status to offline")
    _last_players_update_monotonic = None
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    logger.info("Current bindings file: %s", CONFIG_PATH.resolve())


@bot.event
async def setup_hook() -> None:
    ensure_killfeed_layout_config(KILLFEED_LAYOUT_PATH)
    ensure_killfeed_weapon_groups_config(KILLFEED_WEAPON_GROUPS_PATH)
    ensure_killfeed_template(KILLFEED_BASE_IMAGE_PATH, KILLFEED_LAYOUT_PATH)
    default_base, grouped = _load_weapon_group_base_images(KILLFEED_WEAPON_GROUPS_PATH)
    ensure_killfeed_template(default_base, KILLFEED_LAYOUT_PATH)
    for grouped_base in set(grouped.values()):
        ensure_killfeed_template(grouped_base, KILLFEED_LAYOUT_PATH)

    if not _status_timeout_watcher.is_running():
        _status_timeout_watcher.start()

    if SYNC_GUILD_ID and SYNC_GUILD_ID.isdigit():
        guild = discord.Object(id=int(SYNC_GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        logger.info("Slash commands synced to guild %s (%s commands)", guild.id, len(synced))
        return

    if SYNC_GUILD_ID:
        logger.warning("SYNC_GUILD_ID is set but invalid: %s", SYNC_GUILD_ID)

    synced = await bot.tree.sync()
    logger.info("Global slash commands synced (%s commands)", len(synced))


@bot.tree.command(name="set_status_channel", description="Set status source channel for bot presence")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_status_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    _set_status_channel_id(interaction.guild.id, channel.id)
    await interaction.response.send_message(
        f"Канал статуса установлен: {channel.mention}. "
        "Буду обновлять статус по значению после `Players:`.",
        ephemeral=True,
    )


@bot.tree.command(name="unset_status_channel", description="Disable status channel tracking")
@app_commands.checks.has_permissions(manage_guild=True)
async def unset_status_channel(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    removed = _remove_status_channel_id(interaction.guild.id)
    if removed:
        await interaction.response.send_message("Отслеживание статус-канала отключено.", ephemeral=True)
        return

    await interaction.response.send_message("Статус-канал не был настроен.", ephemeral=True)


@bot.tree.command(name="status_channel_info", description="Show configured status source channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def status_channel_info(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    channel_id = _get_status_channel_id(interaction.guild.id)
    if channel_id is None:
        await interaction.response.send_message("Статус-канал не настроен.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(channel_id)
    channel_view = channel.mention if isinstance(channel, discord.TextChannel) else f"ID: {channel_id}"
    await interaction.response.send_message(
        f"Текущий статус-канал: {channel_view}",
        ephemeral=True,
    )


@bot.tree.command(name="set_killfeed_channels", description="Set killfeed source and output channels")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_killfeed_channels(
    interaction: discord.Interaction,
    source_channel: discord.TextChannel,
    output_channel: discord.TextChannel,
) -> None:
    if interaction.guild is None:
        await _safe_send_interaction_message(interaction, "Эта команда работает только на сервере.")
        return

    _set_killfeed_channels(interaction.guild.id, source_channel.id, output_channel.id)
    await _safe_send_interaction_message(
        interaction,
        f"Killfeed настроен: источник {source_channel.mention}, вывод {output_channel.mention}.",
    )


@bot.tree.command(name="unset_killfeed_channels", description="Disable killfeed parsing")
@app_commands.checks.has_permissions(manage_guild=True)
async def unset_killfeed_channels(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await _safe_send_interaction_message(interaction, "Эта команда работает только на сервере.")
        return

    removed = _remove_killfeed_channels(interaction.guild.id)
    if removed:
        await _safe_send_interaction_message(interaction, "Killfeed отслеживание отключено.")
        return

    await _safe_send_interaction_message(interaction, "Killfeed не был настроен.")


@bot.tree.command(name="killfeed_info", description="Show configured killfeed channels")
@app_commands.checks.has_permissions(manage_guild=True)
async def killfeed_info(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await _safe_send_interaction_message(interaction, "Эта команда работает только на сервере.")
        return

    channels = _get_killfeed_channels(interaction.guild.id)
    if channels is None:
        await _safe_send_interaction_message(interaction, "Killfeed не настроен.")
        return

    source = interaction.guild.get_channel(channels["source_channel_id"])
    target = interaction.guild.get_channel(channels["target_channel_id"])
    source_view = source.mention if isinstance(source, discord.TextChannel) else f"ID: {channels['source_channel_id']}"
    target_view = target.mention if isinstance(target, discord.TextChannel) else f"ID: {channels['target_channel_id']}"
    await _safe_send_interaction_message(
        interaction,
        "Текущие killfeed-каналы:\n"
        f"- источник: {source_view}\n"
        f"- вывод: {target_view}",
    )


@bot.tree.command(name="set_map_channel", description="Канал для координат на карте (строки «\"x y z\", r» или x y z r)")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_map_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    map_channel.set_map_channel_id(interaction.guild.id, channel.id)
    await interaction.response.send_message(
        f"Канал карты установлен: {channel.mention}. "
        "Отправляйте строки вида `\"x y z\", r` (как в логах) или `x y z r`; "
        "r — радиус в метрах. Пустые строки и `#` в начале строки игнорируются.",
        ephemeral=True,
    )


@bot.tree.command(name="unset_map_channel", description="Отключить канал координат на карте")
@app_commands.checks.has_permissions(manage_guild=True)
async def unset_map_channel(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    removed = map_channel.remove_map_channel_id(interaction.guild.id)
    if removed:
        await interaction.response.send_message("Канал карты отключён.", ephemeral=True)
        return

    await interaction.response.send_message("Канал карты не был настроен.", ephemeral=True)


@bot.tree.command(name="map_channel_info", description="Показать настроенный канал для координат на карте")
@app_commands.checks.has_permissions(manage_guild=True)
async def map_channel_info(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    channel_id = map_channel.get_map_channel_id(interaction.guild.id)
    if channel_id is None:
        await interaction.response.send_message("Канал карты не настроен.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(channel_id)
    channel_view = channel.mention if isinstance(channel, discord.TextChannel) else f"ID: {channel_id}"
    await interaction.response.send_message(f"Текущий канал карты: {channel_view}", ephemeral=True)


@bot.tree.command(name="set_map_marker_style", description="Цвет и альфа заливки кругов на карте для этого сервера")
@app_commands.describe(
    r="Красный (0–255)",
    g="Зелёный (0–255)",
    b="Синий (0–255)",
    a="Альфа (1–255, меньше — прозрачнее)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def set_map_marker_style(
    interaction: discord.Interaction,
    r: app_commands.Range[int, 0, 255],
    g: app_commands.Range[int, 0, 255],
    b: app_commands.Range[int, 0, 255],
    a: app_commands.Range[int, 1, 255],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    map_channel.set_marker_rgba(interaction.guild.id, r, g, b, a)
    await interaction.response.send_message(
        f"Цвет маркеров карты: RGBA({r}, {g}, {b}, {a}). Сохранено для этого сервера.",
        ephemeral=True,
    )


@bot.tree.command(name="unset_map_marker_style", description="Сбросить цвет маркеров карты (брать из переменных окружения)")
@app_commands.checks.has_permissions(manage_guild=True)
async def unset_map_marker_style(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    removed = map_channel.clear_marker_style(interaction.guild.id)
    if removed:
        await interaction.response.send_message(
            "Своя палитра сброшена. Используются `MAP_MARKER_R/G/B/ALPHA` из окружения (или встроенные значения по умолчанию).",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("Для этого сервера не было отдельной настройки цвета.", ephemeral=True)


@bot.tree.command(name="map_marker_style_info", description="Текущий RGBA маркеров карты для этого сервера")
@app_commands.checks.has_permissions(manage_guild=True)
async def map_marker_style_info(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    rgba = map_channel.get_marker_rgba(interaction.guild.id)
    custom = map_channel.has_custom_marker_style(interaction.guild.id)
    src = "настройка команды `/set_map_marker_style`" if custom else "переменные окружения / значения по умолчанию"
    await interaction.response.send_message(
        f"Маркеры карты: **RGBA** `{rgba[0]}, {rgba[1]}, {rgba[2]}, {rgba[3]}`\nИсточник: {src}.",
        ephemeral=True,
    )


@bot.tree.command(name="bind_reaction", description="Bind reaction role for this server")
@app_commands.checks.has_permissions(manage_roles=True)
async def bind_reaction(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message_ref: str,
    role: discord.Role,
    emoji: str,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    link_channel_id, parsed_message_id = _parse_message_reference(message_ref)
    if parsed_message_id is None:
        await interaction.response.send_message(
            "`message_ref` должен быть ID сообщения или ссылкой вида "
            "`https://discord.com/channels/<guild>/<channel>/<message>`.",
            ephemeral=True,
        )
        return

    target_channel = channel
    if link_channel_id is not None:
        linked_channel = interaction.guild.get_channel(link_channel_id)
        if not isinstance(linked_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Канал из ссылки не найден или не является текстовым.",
                ephemeral=True,
            )
            return
        target_channel = linked_channel

    try:
        message = await target_channel.fetch_message(parsed_message_id)
    except discord.NotFound:
        await interaction.response.send_message("Сообщение не найдено в выбранном канале.", ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.response.send_message("Нет доступа к этому каналу/сообщению.", ephemeral=True)
        return

    _set_guild_binding(interaction.guild.id, message.id, role.id, emoji)
    await interaction.response.send_message(
        f"Готово. Отслеживаю реакцию `{_normalize_emoji_string(emoji)}` на сообщении "
        f"{message.jump_url} и выдаю роль {role.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="unbind_reaction", description="Disable reaction role for this server")
@app_commands.checks.has_permissions(manage_roles=True)
async def unbind_reaction(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    removed = _remove_guild_binding(interaction.guild.id)
    if removed:
        await interaction.response.send_message("Привязка реакции удалена.", ephemeral=True)
        return

    await interaction.response.send_message("Привязка не была настроена.", ephemeral=True)


@bot.tree.command(name="reaction_bind_info", description="Show current reaction-role binding")
@app_commands.checks.has_permissions(manage_roles=True)
async def reaction_bind_info(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await _safe_send_interaction_message(interaction, "Эта команда работает только на сервере.")
        return

    binding = _get_guild_binding(interaction.guild.id)
    if not binding:
        await _safe_send_interaction_message(interaction, "Привязка не настроена.")
        return

    role = interaction.guild.get_role(int(binding["role_id"]))
    role_view = role.mention if role else f"ID: {binding['role_id']}"
    await _safe_send_interaction_message(
        interaction,
        "Текущая привязка:\n"
        f"- message_id: `{binding['message_id']}`\n"
        f"- role: {role_view}\n"
        f"- emoji: `{binding['emoji']}`",
    )


@bind_reaction.error
@unbind_reaction.error
@reaction_bind_info.error
@set_status_channel.error
@unset_status_channel.error
@status_channel_info.error
@set_killfeed_channels.error
@unset_killfeed_channels.error
@killfeed_info.error
@set_map_channel.error
@unset_map_channel.error
@map_channel_info.error
@set_map_marker_style.error
@unset_map_marker_style.error
@map_marker_style_info.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await _safe_send_interaction_message(
            interaction,
            "Нужны права `Manage Roles` или `Manage Server` для этой команды.",
        )
        return

    logger.exception("Slash command error: %s", error)
    await _safe_send_interaction_message(interaction, "Произошла ошибка при выполнении команды.")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if bot.user and payload.user_id == bot.user.id:
        return

    binding = _get_payload_binding(payload)
    if not binding:
        return

    if payload.message_id != int(binding["message_id"]):
        return
    if not _emoji_matches(payload.emoji, str(binding["emoji"])):
        return

    role = await _get_role(payload, int(binding["role_id"]))
    member = await _resolve_member(payload)
    if role is None or member is None:
        return
    if role in member.roles:
        return

    try:
        await member.add_roles(role, reason="User added tracked reaction")
        logger.info("Added role '%s' to %s", role.name, member)
    except discord.Forbidden:
        logger.warning("Missing permissions to add role '%s' to %s", role.name, member)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    binding = _get_payload_binding(payload)
    if not binding:
        return

    if payload.message_id != int(binding["message_id"]):
        return
    if not _emoji_matches(payload.emoji, str(binding["emoji"])):
        return

    role = await _get_role(payload, int(binding["role_id"]))
    member = await _resolve_member(payload)
    if role is None or member is None:
        return
    if role not in member.roles:
        return

    try:
        await member.remove_roles(role, reason="User removed tracked reaction")
        logger.info("Removed role '%s' from %s", role.name, member)
    except discord.Forbidden:
        logger.warning("Missing permissions to remove role '%s' from %s", role.name, member)


@bot.event
async def on_message(message: discord.Message) -> None:
    global _last_players_update_monotonic
    logger.info(
        "on_message: author=%s guild=%s channel=%s content=%r",
        message.author.id,
        message.guild.id if message.guild else None,
        message.channel.id if hasattr(message.channel, "id") else None,
        message.content,
    )

    if bot.user and message.author.id == bot.user.id:
        logger.info("on_message: skipped own bot message")
        return
    if message.guild is None:
        logger.info("on_message: skipped because message is not from guild")
        return

    status_channel_id = _get_status_channel_id(message.guild.id)
    if status_channel_id is not None and message.channel.id == status_channel_id:
        if SERVER_STARTED_RE.search(message.content):
            logger.info("on_message: detected server started message")
            try:
                await bot.change_presence(activity=discord.Game(name="online"))
                _last_players_update_monotonic = time.monotonic()
                logger.info(
                    "Updated bot status from guild %s channel %s to online",
                    message.guild.id,
                    message.channel.id,
                )
            except discord.HTTPException:
                logger.exception("Failed to update bot status to online")
        else:
            players_match = re.search(r"Players:\s*\**([0-9]+/[0-9]+)\**", message.content)
            if players_match is None:
                logger.info("on_message: no Players pattern in message message.content=%r", message.content)
            else:
                players_value = players_match.group(1)
                logger.info("on_message: extracted players value=%s", players_value)
                try:
                    await bot.change_presence(activity=discord.Game(name=players_value))
                    _last_players_update_monotonic = time.monotonic()
                    logger.info(
                        "Updated bot status from guild %s channel %s to players=%s",
                        message.guild.id,
                        message.channel.id,
                        players_value,
                    )
                except discord.HTTPException:
                    logger.exception("Failed to update bot status to players=%s", players_value)
    else:
        logger.info(
            "on_message: status tracking skipped (configured=%s actual=%s)",
            status_channel_id,
            message.channel.id,
        )

    killfeed_channels = _get_killfeed_channels(message.guild.id)
    if killfeed_channels and message.channel.id == killfeed_channels["source_channel_id"]:
        killfeed_match = KILLFEED_RE.search(message.content)
        if killfeed_match is None:
            logger.info("on_message: killfeed message skipped, format mismatch")
        else:
            target_channel = message.guild.get_channel(killfeed_channels["target_channel_id"])
            if not isinstance(target_channel, discord.TextChannel):
                logger.warning(
                    "on_message: killfeed output channel invalid (%s)",
                    killfeed_channels["target_channel_id"],
                )
            else:
                player_name = killfeed_match.group("player").strip()
                killer_name = killfeed_match.group("killer").strip()
                gun_name = killfeed_match.group("gun").strip()
                distance = killfeed_match.group("distance").strip()
                activity_summary = killfeed_match.group("activity_summary").strip()
                steam_urls = _steam_profile_urls_in_order(activity_summary)
                victim_url = steam_urls[0] if len(steam_urls) > 0 else None
                killer_url = steam_urls[1] if len(steam_urls) > 1 else None
                victim_out = _name_with_optional_steam_link(player_name, victim_url)
                killer_out = _name_with_optional_steam_link(killer_name, killer_url)
                distance_display = _format_distance_whole_meters(distance)
                base_image_for_weapon = _killfeed_base_image_for_weapon(gun_name)
                try:
                    image_buffer = render_killfeed_card(
                        player_name=player_name,
                        killer_name=killer_name,
                        weapon_name=gun_name,
                        distance_meters=distance,
                        base_image_path=base_image_for_weapon,
                        layout_config_path=KILLFEED_LAYOUT_PATH,
                        assets_dir=KILLFEED_ASSETS_DIR,
                        weapons_dir=KILLFEED_WEAPONS_DIR,
                    )
                    await target_channel.send(
                        content=(
                            f"{killer_out} killed {victim_out} with {gun_name} "
                            f"from {distance_display} meters"
                        ),
                        file=discord.File(fp=image_buffer, filename="killfeed.png"),
                    )
                    logger.info(
                        "on_message: killfeed forwarded to channel %s",
                        target_channel.id,
                    )
                except discord.HTTPException:
                    logger.exception("on_message: failed to forward killfeed message")

    await map_channel.handle_map_coordinate_message(message)

    await bot.process_commands(message)


@tasks.loop(seconds=30)
async def _status_timeout_watcher() -> None:
    global _last_players_update_monotonic

    if _last_players_update_monotonic is None:
        return

    if time.monotonic() - _last_players_update_monotonic < STATUS_TIMEOUT_SECONDS:
        return

    try:
        await bot.change_presence(activity=discord.Game(name="offline"))
        logger.info("No Players updates for %s seconds, status set to offline", STATUS_TIMEOUT_SECONDS)
    except discord.HTTPException:
        logger.exception("Failed to set offline status after timeout")
    finally:
        _last_players_update_monotonic = None


@_status_timeout_watcher.before_loop
async def _before_status_timeout_watcher() -> None:
    await bot.wait_until_ready()


if __name__ == "__main__":
    bot.run(TOKEN)
