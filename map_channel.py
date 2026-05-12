"""Канал Discord для координат DayZ на карте Chernarus: строки `"x y z", r` или `x y z r`, несколько строк за раз."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import discord
from PIL import Image, ImageChops, ImageDraw

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent
_DEFAULT_MAP_IMAGE = _REPO_ROOT / "assets" / "map" / "high-resolution-chernarus-map-4000x4000-v0-a2mn8bzx93gd1.png"

MAP_CHANNEL_CONFIG_PATH = Path(os.getenv("MAP_CHANNEL_CONFIG_PATH", "map_channel_config.json"))
MAP_MARKER_STYLE_PATH = Path(os.getenv("MAP_MARKER_STYLE_PATH", "map_marker_style.json"))
MAP_IMAGE_PATH = Path(os.getenv("MAP_IMAGE_PATH", str(_DEFAULT_MAP_IMAGE)))

# Строка из четырёх чисел через пробел (классический вариант).
PLAIN_XYZR_LINE_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*$",
)
# Как в игровом выводе: "x y z", r (ASCII-кавычки).
ASCII_QUOTED_XYZR_LINE_RE = re.compile(
    r'^\s*"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)"\s*,\s*(-?\d+(?:\.\d+)?)\s*$',
)
# Типографские «ёлочки» Unicode.
CURLY_QUOTED_XYZR_LINE_RE = re.compile(
    r"^\s*\u201c(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\u201d\s*,\s*(-?\d+(?:\.\d+)?)\s*$",
)


def load_map_channel_config() -> dict[str, int]:
    if not MAP_CHANNEL_CONFIG_PATH.exists():
        return {}

    with MAP_CHANNEL_CONFIG_PATH.open("r", encoding="utf-8") as f:
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


def save_map_channel_config(config: dict[str, int]) -> None:
    with MAP_CHANNEL_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_map_channel_id(guild_id: int) -> Optional[int]:
    config = load_map_channel_config()
    return config.get(str(guild_id))


def set_map_channel_id(guild_id: int, channel_id: int) -> None:
    config = load_map_channel_config()
    config[str(guild_id)] = channel_id
    save_map_channel_config(config)


def remove_map_channel_id(guild_id: int) -> bool:
    config = load_map_channel_config()
    existed = str(guild_id) in config
    if existed:
        del config[str(guild_id)]
        save_map_channel_config(config)
    return existed


def _clamp_byte(n: int) -> int:
    return max(0, min(255, n))


def _default_marker_rgba_from_env() -> tuple[int, int, int, int]:
    """Цвет по умолчанию, если для гильдии нет записи в map_marker_style.json."""
    try:
        r = _clamp_byte(int(os.getenv("MAP_MARKER_R", "255")))
        g = _clamp_byte(int(os.getenv("MAP_MARKER_G", "0")))
        b = _clamp_byte(int(os.getenv("MAP_MARKER_B", "0")))
        a = _clamp_byte(int(os.getenv("MAP_MARKER_ALPHA", "110")))
    except ValueError:
        r, g, b, a = 255, 0, 0, 110
    a = max(1, a)
    return r, g, b, a


def load_marker_style_config() -> dict[str, dict[str, int]]:
    if not MAP_MARKER_STYLE_PATH.exists():
        return {}
    try:
        with MAP_MARKER_STYLE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Invalid map marker style config: %s", MAP_MARKER_STYLE_PATH)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for gid, row in data.items():
        if not isinstance(row, dict):
            continue
        try:
            out[str(gid)] = {
                "r": _clamp_byte(int(row["r"])),
                "g": _clamp_byte(int(row["g"])),
                "b": _clamp_byte(int(row["b"])),
                "a": max(1, _clamp_byte(int(row["a"]))),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_marker_style_config(config: dict[str, dict[str, int]]) -> None:
    with MAP_MARKER_STYLE_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_marker_rgba(guild_id: int) -> tuple[int, int, int, int]:
    cfg = load_marker_style_config()
    row = cfg.get(str(guild_id))
    if row is not None:
        return row["r"], row["g"], row["b"], row["a"]
    return _default_marker_rgba_from_env()


def set_marker_rgba(guild_id: int, r: int, g: int, b: int, a: int) -> None:
    cfg = load_marker_style_config()
    cfg[str(guild_id)] = {
        "r": _clamp_byte(r),
        "g": _clamp_byte(g),
        "b": _clamp_byte(b),
        "a": max(1, _clamp_byte(a)),
    }
    save_marker_style_config(cfg)


def clear_marker_style(guild_id: int) -> bool:
    cfg = load_marker_style_config()
    key = str(guild_id)
    if key not in cfg:
        return False
    del cfg[key]
    save_marker_style_config(cfg)
    return True


def has_custom_marker_style(guild_id: int) -> bool:
    return str(guild_id) in load_marker_style_config()


def parse_xyzr_coords_line(text: str) -> Optional[tuple[float, float, float, float]]:
    s = text or ""
    for rx in (ASCII_QUOTED_XYZR_LINE_RE, CURLY_QUOTED_XYZR_LINE_RE, PLAIN_XYZR_LINE_RE):
        m = rx.match(s)
        if not m:
            continue
        try:
            return float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
        except ValueError:
            return None
    return None


def parse_xyzr_coords_from_message(text: str) -> list[tuple[float, float, float, float]]:
    """Каждая непустая строка (кроме #…): `\"x y z\", r` или `x y z r`."""
    rows: list[tuple[float, float, float, float]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        row = parse_xyzr_coords_line(line)
        if row is not None:
            rows.append(row)
    return rows


def _world_max_meters() -> float:
    raw = os.getenv("MAP_WORLD_MAX_METERS", "15360")
    try:
        v = float(raw)
        return v if v > 0 else 15360.0
    except ValueError:
        return 15360.0


def _flip_z() -> bool:
    return os.getenv("MAP_FLIP_Z", "1").strip().lower() in ("1", "true", "yes", "on")


def _map_edge_inset_px(width: int, height: int) -> int:
    """Отступ в пикселях от края PNG (белая рамка): координаты мира мапятся на внутренний прямоугольник."""
    try:
        inset = int(os.getenv("MAP_EDGE_INSET_PX", "14"))
    except ValueError:
        inset = 14
    inset = max(0, inset)
    max_inset = max(0, min(width, height) // 2 - 1)
    return min(inset, max_inset)


def _map_plot_rect(width: int, height: int) -> tuple[int, int, int]:
    """Возвращает (inset, inner_w, inner_h) — область карты без декоративной обводки."""
    inset = _map_edge_inset_px(width, height)
    iw = max(1, width - 2 * inset)
    ih = max(1, height - 2 * inset)
    return inset, iw, ih


def _radius_meters_to_pixels(r_m: float, width: int, height: int) -> int:
    """Радиус в метрах мира → пиксели (масштаб как у x/z, по внутренней области без рамки)."""
    world_max = _world_max_meters()
    inset, iw, ih = _map_plot_rect(width, height)
    r_px = r_m / world_max * iw
    ri = int(round(r_px))
    cap = min(iw, ih) // 2
    return max(3, min(cap, ri))


def _world_xz_to_image_pixels(x: float, z: float, width: int, height: int) -> tuple[int, int]:
    world_max = _world_max_meters()
    inset, iw, ih = _map_plot_rect(width, height)
    px = inset + x / world_max * iw
    if _flip_z():
        py = inset + (world_max - z) / world_max * ih
    else:
        py = inset + z / world_max * ih
    xi = int(round(px))
    yi = int(round(py))
    xi = max(0, min(width - 1, xi))
    yi = max(0, min(height - 1, yi))
    return xi, yi


def render_chernarus_map_with_markers(
    markers_xzr: list[tuple[float, float, float]],
    guild_id: int,
) -> io.BytesIO:
    """Полупрозрачные круги цвета гильдии; пересечения не усиливают заливку (объединение по маске)."""
    if not markers_xzr:
        raise ValueError("markers_xzr must not be empty")
    path = MAP_IMAGE_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Map image not found: {path.resolve()}")

    mr, mg, mb, alpha = get_marker_rgba(guild_id)
    base = Image.open(path).convert("RGBA")
    w, h = base.size

    union_mask = Image.new("L", (w, h), 0)
    for x, z, radius_meters in markers_xzr:
        r = _radius_meters_to_pixels(radius_meters, w, h)
        cx, cy = _world_xz_to_image_pixels(x, z, w, h)
        bbox = (cx - r, cy - r, cx + r, cy + r)
        circle_mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(circle_mask).ellipse(bbox, fill=255)
        union_mask = ImageChops.lighter(union_mask, circle_mask)

    def scale_a(p: int) -> int:
        return int(p * alpha / 255) if p else 0

    a_scaled = union_mask.point(scale_a, mode="L")

    r_ch = Image.new("L", (w, h), mr)
    g_ch = Image.new("L", (w, h), mg)
    b_ch = Image.new("L", (w, h), mb)
    layer = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_scaled))
    out = Image.alpha_composite(base, layer)

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def handle_map_coordinate_message(message: discord.Message) -> None:
    """Если канал настроен: строки `\"x y z\", r` или `x y z r` — одна карта со всеми кругами."""
    if message.guild is None:
        return
    map_channel_id = get_map_channel_id(message.guild.id)
    if map_channel_id is None or message.channel.id != map_channel_id:
        return
    rows = parse_xyzr_coords_from_message(message.content)
    if not rows:
        return
    markers_xzr: list[tuple[float, float, float]] = []
    for wx, _wy, wz, r_m in rows:
        if r_m <= 0:
            try:
                await message.channel.send(
                    "Радиус `r` в каждой строке должен быть больше нуля (метры на карте)."
                )
            except discord.HTTPException:
                logger.exception("Failed to send map radius validation message")
            return
        markers_xzr.append((wx, wz, r_m))
    try:
        image_buffer = await asyncio.to_thread(
            render_chernarus_map_with_markers,
            markers_xzr,
            message.guild.id,
        )
    except FileNotFoundError as exc:
        logger.error("Map channel: %s", exc)
        try:
            await message.channel.send(
                f"Файл карты не найден: `{MAP_IMAGE_PATH}`. Проверьте `MAP_IMAGE_PATH` в окружении."
            )
        except discord.HTTPException:
            logger.exception("Failed to send map missing-file notice")
    except OSError:
        logger.exception("Failed to open or render map image")
        try:
            await message.channel.send("Не удалось открыть или обработать изображение карты.")
        except discord.HTTPException:
            logger.exception("Failed to send map error notice")
    else:
        try:
            await message.channel.send(file=discord.File(fp=image_buffer, filename="chernarus_marker.png"))
        except discord.HTTPException:
            logger.exception("on_message: failed to send map marker image")
