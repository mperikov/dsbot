import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


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


intents = discord.Intents.default()
intents.guilds = True
intents.members = False
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

MESSAGE_LINK_RE = re.compile(
    r"^https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)$"
)


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


def _save_config(config: dict[str, dict[str, str | int]]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _get_guild_binding(guild_id: int) -> Optional[dict[str, str | int]]:
    config = _load_config()
    return config.get(str(guild_id))


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


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    logger.info("Current bindings file: %s", CONFIG_PATH.resolve())


@bot.event
async def setup_hook() -> None:
    await bot.tree.sync()
    logger.info("Slash commands synced")


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
        await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
        return

    binding = _get_guild_binding(interaction.guild.id)
    if not binding:
        await interaction.response.send_message("Привязка не настроена.", ephemeral=True)
        return

    role = interaction.guild.get_role(int(binding["role_id"]))
    role_view = role.mention if role else f"ID: {binding['role_id']}"
    await interaction.response.send_message(
        "Текущая привязка:\n"
        f"- message_id: `{binding['message_id']}`\n"
        f"- role: {role_view}\n"
        f"- emoji: `{binding['emoji']}`",
        ephemeral=True,
    )


@bind_reaction.error
@unbind_reaction.error
@reaction_bind_info.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        if interaction.response.is_done():
            await interaction.followup.send("Нужны права `Manage Roles`.", ephemeral=True)
        else:
            await interaction.response.send_message("Нужны права `Manage Roles`.", ephemeral=True)
        return

    logger.exception("Slash command error: %s", error)
    if interaction.response.is_done():
        await interaction.followup.send("Произошла ошибка при выполнении команды.", ephemeral=True)
    else:
        await interaction.response.send_message("Произошла ошибка при выполнении команды.", ephemeral=True)


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


if __name__ == "__main__":
    bot.run(TOKEN)
