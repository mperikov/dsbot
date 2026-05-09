from __future__ import annotations

import io
import json
import logging
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


logger = logging.getLogger(__name__)


CARD_WIDTH = 1100
CARD_HEIGHT = 420
PADDING_X = 42
PADDING_Y = 36


def _default_canvas() -> dict:
    return {
        "width": CARD_WIDTH,
        "height": CARD_HEIGHT,
        "show_base_image": False,
    }


def _merge_canvas(user: dict | None) -> dict:
    canvas = _default_canvas()
    if not user:
        return canvas
    for key in ("width", "height"):
        if key in user and user[key] is not None:
            try:
                v = int(user[key])
                if v > 0:
                    canvas[key] = v
            except (TypeError, ValueError):
                pass
    if user.get("show_base_image") is not None:
        canvas["show_base_image"] = bool(user["show_base_image"])
    return canvas


def ensure_killfeed_layout_config(layout_config_path: Path) -> None:
    if layout_config_path.exists():
        return

    layout_config_path.parent.mkdir(parents=True, exist_ok=True)
    default_layout = {
        "canvas": dict(_default_canvas()),
        "disable_all_shadows": False,
        "footer_colors": {
            "distance": [220, 235, 255, 255],
            "weapon": [255, 210, 160, 255],
        },
        "nickname_style": {"uppercase": False},
        "fonts": {"default": "assets/fonts/OpenSans-VF.ttf"},
        "victim": {
            "x": 270,
            "y": 118,
            "max_width": 780,
            "font_size": 42,
            "min_font_size": 22,
            "color": [245, 248, 255, 255],
            "shadow": True,
            "shadow_offset_x": 2,
            "shadow_offset_y": 2,
            "shadow_color": [0, 0, 0, 190],
            "background_enabled": True,
            "background_color": [10, 16, 28, 155],
            "background_padding_x": 16,
            "background_padding_y": 8,
            "background_radius": 10,
        },
        "killer": {
            "x": 270,
            "y": 188,
            "max_width": 780,
            "font_size": 42,
            "min_font_size": 22,
            "color": [255, 200, 200, 255],
            "shadow": True,
            "shadow_offset_x": 2,
            "shadow_offset_y": 2,
            "shadow_color": [0, 0, 0, 190],
            "background_enabled": True,
            "background_color": [10, 16, 28, 155],
            "background_padding_x": 16,
            "background_padding_y": 8,
            "background_radius": 10,
        },
        "weapon": {
            "x": 900,
            "y": 210,
            "max_width": 360,
            "max_height": 150,
            "stretch_to_max_width": False,
            "shadow": False,
            "shadow_offset_x": 4,
            "shadow_offset_y": 4,
            "shadow_color": [0, 0, 0, 150],
        },
        "footer_distance": {
            "enabled": True,
            "format": "{distance} m",
            "canvas_center_x": True,
            "text_align": "center",
            "y_anchor": "top",
            "margin_bottom": 52,
            "font_size": 24,
            "min_font_size": 14,
            "max_width": 1100,
            "use_weapon_accent_color": False,
            "shadow": False,
            "shadow_offset_x": 2,
            "shadow_offset_y": 2,
            "shadow_color": [0, 0, 0, 180],
        },
        "footer_weapon": {
            "enabled": True,
            "format": "{weapon}",
            "canvas_center_x": True,
            "text_align": "center",
            "y_anchor": "top",
            "margin_bottom": 28,
            "font_size": 22,
            "min_font_size": 14,
            "max_width": 1100,
            "use_weapon_accent_color": True,
            "shadow": False,
            "shadow_offset_x": 2,
            "shadow_offset_y": 2,
            "shadow_color": [0, 0, 0, 180],
        },
    }
    with layout_config_path.open("w", encoding="utf-8") as f:
        json.dump(default_layout, f, ensure_ascii=False, indent=2)


def _read_json_dict(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _normalize_weapon_token(name: str) -> str:
    cleaned = re.sub(r"[\[\]()]", " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _weapon_name_variants(weapon_name: str) -> list[str]:
    base = _normalize_weapon_token(weapon_name)
    if not base:
        return []
    upper = base.upper()
    hyphen = re.sub(r"\s+", "-", base.strip())
    spaced = re.sub(r"-+", " ", base.strip())
    compact = re.sub(r"[^A-Za-z0-9]+", "", base).upper()
    variants = [base, upper, hyphen, hyphen.upper(), spaced, spaced.upper(), compact]
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _resolve_weapon_image_path(weapon_name: str, weapons_dir: Path) -> Path | None:
    if not weapon_name or not weapons_dir.is_dir():
        return None

    stems: dict[str, Path] = {}
    try:
        for entry in weapons_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg"}:
                continue
            stems[entry.stem.lower()] = entry
    except OSError:
        return None

    for variant in _weapon_name_variants(weapon_name):
        key = variant.lower()
        if key in stems:
            return stems[key]
        key_hyphen = re.sub(r"\s+", "-", key)
        if key_hyphen in stems:
            return stems[key_hyphen]
        key_space = key.replace("-", " ")
        if key_space in stems:
            return stems[key_space]

    compact_needle = re.sub(r"[^a-z0-9]+", "", weapon_name.lower())
    if len(compact_needle) >= 2:
        for stem_lower, path in stems.items():
            stem_compact = re.sub(r"[^a-z0-9]+", "", stem_lower)
            if compact_needle == stem_compact or compact_needle in stem_compact or stem_compact in compact_needle:
                return path

    return None


def _default_layout_slots() -> dict[str, dict]:
    return {
        "victim": {
            "x": 270,
            "y": 118,
            "max_width": 780,
            "font_size": 42,
            "min_font_size": 22,
            "color": [245, 248, 255, 255],
            "shadow": True,
            "shadow_offset_x": 2,
            "shadow_offset_y": 2,
            "shadow_color": [0, 0, 0, 190],
            "background_enabled": True,
            "background_color": [10, 16, 28, 155],
            "background_padding_x": 16,
            "background_padding_y": 8,
            "background_radius": 10,
        },
        "killer": {
            "x": 270,
            "y": 188,
            "max_width": 780,
            "font_size": 42,
            "min_font_size": 22,
            "color": [255, 200, 200, 255],
            "shadow": True,
            "shadow_offset_x": 2,
            "shadow_offset_y": 2,
            "shadow_color": [0, 0, 0, 190],
            "background_enabled": True,
            "background_color": [10, 16, 28, 155],
            "background_padding_x": 16,
            "background_padding_y": 8,
            "background_radius": 10,
        },
        "weapon": {
            "x": 900,
            "y": 210,
            "max_width": 360,
            "max_height": 150,
            "stretch_to_max_width": False,
            "shadow": False,
            "shadow_offset_x": 4,
            "shadow_offset_y": 4,
            "shadow_color": [0, 0, 0, 150],
        },
        "footer_distance": {
            "enabled": True,
            "format": "{distance} m",
            "canvas_center_x": True,
            "text_align": "center",
            "y_anchor": "top",
            "margin_bottom": 52,
            "font_size": 24,
            "min_font_size": 14,
            "max_width": 1100,
            "use_weapon_accent_color": False,
            "shadow": False,
            "shadow_offset_x": 2,
            "shadow_offset_y": 2,
            "shadow_color": [0, 0, 0, 180],
        },
        "footer_weapon": {
            "enabled": True,
            "format": "{weapon}",
            "canvas_center_x": True,
            "text_align": "center",
            "y_anchor": "top",
            "margin_bottom": 28,
            "font_size": 22,
            "min_font_size": 14,
            "max_width": 1100,
            "use_weapon_accent_color": True,
            "shadow": False,
            "shadow_offset_x": 2,
            "shadow_offset_y": 2,
            "shadow_color": [0, 0, 0, 180],
        },
    }


def _merge_layout_slots(user: dict | None) -> dict[str, dict]:
    defaults = _default_layout_slots()
    if not user:
        return defaults
    merged: dict[str, dict] = {}
    for key in ("victim", "killer", "weapon", "footer_distance", "footer_weapon"):
        base_slot = dict(defaults[key])
        slot_raw = user.get(key)
        if isinstance(slot_raw, dict):
            for sk, sv in slot_raw.items():
                if sv is not None:
                    base_slot[sk] = sv
        merged[key] = base_slot
    return merged


def _load_killfeed_layout_bundle(
    layout_config_path: Path | None,
) -> tuple[dict[str, dict], dict, dict, dict]:
    """Returns (merged slots, nickname_style, fonts, canvas)."""
    raw_user = _read_json_dict(layout_config_path) if layout_config_path is not None else None
    if not isinstance(raw_user, dict):
        return _merge_layout_slots(None), {}, {}, _merge_canvas(None)

    canvas = _merge_canvas(raw_user.get("canvas") if isinstance(raw_user.get("canvas"), dict) else None)

    nickname_style: dict = {}
    ns = raw_user.get("nickname_style")
    if isinstance(ns, dict):
        nickname_style = dict(ns)

    fonts: dict = {}
    fonts_raw = raw_user.get("fonts")
    if isinstance(fonts_raw, dict):
        fonts = fonts_raw

    slot_keys = ("victim", "killer", "weapon", "footer_distance", "footer_weapon", "footer")
    elements = raw_user.get("elements")
    if isinstance(elements, dict):
        slot_payload = {k: elements.get(k) for k in slot_keys}
    else:
        slot_payload = {k: raw_user.get(k) for k in slot_keys}

    cleaned: dict[str, dict] = {}
    for key in slot_keys:
        val = slot_payload.get(key)
        if isinstance(val, dict):
            cleaned[key] = val

    if "footer_distance" not in cleaned and "footer_weapon" not in cleaned and "footer" in cleaned:
        fd, fw = _footer_slots_from_legacy(cleaned.pop("footer"))
        cleaned["footer_distance"] = fd
        cleaned["footer_weapon"] = fw
    else:
        cleaned.pop("footer", None)

    merged = _merge_layout_slots(cleaned)
    _apply_footer_text_colors(merged, raw_user, cleaned)
    if bool(raw_user.get("disable_all_shadows", False)):
        _disable_all_slot_shadows(merged)
    return merged, nickname_style, fonts, canvas


def _coerce_rgba_list(value: object) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    out = [int(value[0]), int(value[1]), int(value[2])]
    out.append(int(value[3]) if len(value) >= 4 else 255)
    return out


def _footer_slot_user_declared_color(cleaned: dict, slot_key: str) -> bool:
    d = cleaned.get(slot_key)
    return isinstance(d, dict) and "color" in d


def _apply_footer_text_colors(merged: dict[str, dict], raw_user: dict, cleaned: dict) -> None:
    """Apply ``footer_colors`` / root color keys when slots do not set ``color``."""
    fc = raw_user.get("footer_colors")
    if not isinstance(fc, dict):
        fc = {}
    dist_src = fc.get("distance")
    if dist_src is None:
        dist_src = raw_user.get("distance_text_color")
    weap_src = fc.get("weapon")
    if weap_src is None:
        weap_src = raw_user.get("weapon_text_color")

    if not _footer_slot_user_declared_color(cleaned, "footer_distance"):
        dcol = _coerce_rgba_list(dist_src)
        if dcol is not None:
            merged["footer_distance"] = dict(merged["footer_distance"])
            merged["footer_distance"]["color"] = dcol

    if not _footer_slot_user_declared_color(cleaned, "footer_weapon"):
        wcol = _coerce_rgba_list(weap_src)
        if wcol is not None:
            merged["footer_weapon"] = dict(merged["footer_weapon"])
            merged["footer_weapon"]["color"] = wcol


def _disable_all_slot_shadows(merged: dict[str, dict]) -> None:
    """Force-disable shadows in all known slots when global flag is enabled."""
    for key in ("victim", "killer", "weapon", "footer_distance", "footer_weapon"):
        slot = merged.get(key)
        if isinstance(slot, dict):
            slot["shadow"] = False


def _footer_slots_from_legacy(legacy: dict) -> tuple[dict, dict]:
    """Split old combined ``footer`` into distance + weapon slots."""
    fd = dict(legacy)
    fd["format"] = "{distance} m"
    fd.setdefault("text_align", "center")
    fd["canvas_center_x"] = True
    fd.setdefault("use_weapon_accent_color", False)
    fd.setdefault("y_anchor", "top")

    fw = dict(legacy)
    fw["format"] = "{weapon}"
    fw.setdefault("text_align", "center")
    fw["canvas_center_x"] = True
    fw.setdefault("use_weapon_accent_color", True)
    fw.setdefault("y_anchor", "top")
    try:
        mb = int(legacy.get("margin_bottom", 48))
    except (TypeError, ValueError):
        mb = 48
    fd["margin_bottom"] = mb + 22
    fw["margin_bottom"] = max(12, mb - 18)
    return fd, fw


def _find_system_font_file(filename: str) -> Path | None:
    """Map a bare font file name (e.g. segoeui.ttf) to an OS font path."""
    name = Path(filename).name
    if Path(name).suffix.lower() not in {".ttf", ".otf", ".ttc"}:
        return None
    windir = os.environ.get("WINDIR")
    if windir:
        fonts = Path(windir) / "Fonts"
        if fonts.is_dir():
            direct = fonts / name
            if direct.is_file():
                return direct
            target = name.lower()
            try:
                for entry in fonts.iterdir():
                    if entry.is_file() and entry.name.lower() == target:
                        return entry
            except OSError:
                pass
    for folder in (
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/liberation"),
        Path("/usr/share/fonts/TTF"),
    ):
        try:
            cand = folder / name
            if cand.is_file():
                return cand
        except OSError:
            continue
    for folder in (Path("/Library/Fonts"), Path.home() / "Library/Fonts"):
        try:
            cand = folder / name
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _resolve_font_file(candidate: str, assets_dir: Path | None) -> Path | None:
    trimmed = candidate.strip()
    if not trimmed:
        return None
    p = Path(trimmed).expanduser()
    if p.is_file():
        return p
    if assets_dir is not None:
        ap = assets_dir / trimmed
        if ap.is_file():
            return ap
        ap2 = assets_dir.parent / trimmed if assets_dir.name else None
        if ap2 and ap2.is_file():
            return ap2
    cwd_p = Path.cwd() / trimmed
    if cwd_p.is_file():
        return cwd_p
    sys_font = _find_system_font_file(trimmed)
    if sys_font is not None:
        return sys_font
    # Times New Roman is not redistributable; use bundled Tinos (OFL) if layout asks for times.ttf.
    if Path(trimmed).name.lower() == "times.ttf":
        for bundled in (
            (assets_dir / "fonts" / "Tinos-Regular.ttf") if assets_dir is not None else None,
            Path.cwd() / "assets" / "fonts" / "Tinos-Regular.ttf",
        ):
            if bundled is not None and bundled.is_file():
                return bundled
    return None


def _font_candidates_for_slot(slot: dict | None, fonts_config: dict) -> list[str]:
    out: list[str] = []
    if slot:
        for key in ("font_path", "font"):
            val = slot.get(key)
            if isinstance(val, str) and val.strip():
                out.append(val.strip())
    default_font = fonts_config.get("default") or fonts_config.get("primary")
    if isinstance(default_font, str) and default_font.strip():
        out.append(default_font.strip())
    fallbacks = fonts_config.get("fallback_paths") or fonts_config.get("paths") or []
    if isinstance(fallbacks, list):
        for item in fallbacks:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
    env_font = os.getenv("KILLFEED_FONT_PATH")
    if env_font:
        out.append(env_font)
    seen: set[str] = set()
    unique: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _load_text_font_at_size(
    size: int,
    candidates: list[str],
    assets_dir: Path | None,
    variation_axes: list[float] | None = None,
) -> ImageFont.ImageFont:
    for candidate in candidates:
        path = _resolve_font_file(candidate, assets_dir)
        if path is None:
            continue
        try:
            font = ImageFont.truetype(str(path), size=size)
            _apply_font_variation(font, variation_axes)
            return font
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=float(size))
    except TypeError:
        return ImageFont.load_default()


def _fit_font_size_with_candidates(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    max_width: int,
    initial_size: int,
    min_size: int,
    candidates: list[str],
    assets_dir: Path | None,
    variation_axes: list[float] | None = None,
) -> ImageFont.ImageFont:
    for size in range(initial_size, min_size - 1, -2):
        font = _load_text_font_at_size(size, candidates, assets_dir, variation_axes)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        if text_width <= max_width:
            return font
    return _load_text_font_at_size(min_size, candidates, assets_dir, variation_axes)


def _weapon_accent_color(weapon_name: str) -> tuple[int, int, int, int]:
    weapon_upper = weapon_name.upper()
    if any(tag in weapon_upper for tag in ("M82", "SVD", "M70", "MOSIN")):
        return (255, 96, 96, 255)
    if any(tag in weapon_upper for tag in ("AK", "M4", "FAL", "SA58")):
        return (255, 170, 70, 255)
    if any(tag in weapon_upper for tag in ("MP5", "UMP", "SKORPION")):
        return (135, 220, 255, 255)
    return (178, 138, 255, 255)


def ensure_killfeed_template(base_image_path: Path, layout_config_path: Path | None = None) -> None:
    if base_image_path.exists():
        return

    canvas = _merge_canvas(None)
    if layout_config_path is not None:
        raw = _read_json_dict(layout_config_path)
        if isinstance(raw, dict) and isinstance(raw.get("canvas"), dict):
            canvas = _merge_canvas(raw["canvas"])

    base_image_path.parent.mkdir(parents=True, exist_ok=True)
    w, h = int(canvas["width"]), int(canvas["height"])
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    image.save(base_image_path, format="PNG", optimize=True)


def _build_killfeed_canvas(base_image_path: Path | None, canvas: dict) -> Image.Image:
    w = int(canvas["width"])
    h = int(canvas["height"])
    use_file = bool(canvas.get("show_base_image", False))

    if use_file and base_image_path and base_image_path.exists():
        with Image.open(base_image_path) as opened:
            image = opened.convert("RGBA")
        if image.size != (w, h):
            image = image.resize((w, h), Image.Resampling.LANCZOS)
        return image

    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def _truncate_nickname_display(text: str, nickname_style: dict | None, slot: dict | None) -> str:
    """Shorten nick for drawing only; ``max_display_length`` counts the final string including ellipsis."""
    style = nickname_style if isinstance(nickname_style, dict) else {}
    slot_d = slot if isinstance(slot, dict) else {}
    raw = slot_d.get("max_display_length")
    if raw is None:
        raw = style.get("max_display_length")
    if raw is None:
        return text
    try:
        max_len = int(raw)
    except (TypeError, ValueError):
        return text
    if max_len <= 0 or len(text) <= max_len:
        return text
    ell_raw = slot_d.get("nickname_ellipsis", slot_d.get("ellipsis"))
    if ell_raw is None:
        ell_raw = style.get("nickname_ellipsis", style.get("ellipsis"))
    ell = "..." if ell_raw is None else str(ell_raw)
    room = max_len - len(ell)
    if room < 1:
        return text[:max_len]
    return text[:room] + ell


def _to_rgba(color: list[int] | tuple[int, ...] | None, fallback: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if color is None:
        return fallback
    if len(color) == 3:
        r, g, b = color
        return int(r), int(g), int(b), 255
    if len(color) >= 4:
        r, g, b, a = color[:4]
        return int(r), int(g), int(b), int(a)
    return fallback


def _apply_font_variation(font: ImageFont.ImageFont, axes: list[float] | None) -> None:
    if not axes:
        return
    setter = getattr(font, "set_variation_by_axes", None)
    if setter is None:
        return
    try:
        setter(axes)
    except (OSError, ValueError, TypeError):
        pass


def _resolve_font_variation_axes(
    slot: dict | None,
    fonts_config: dict | None,
    nickname_style: dict | None = None,
) -> list[float] | None:
    """Axes for variable fonts (e.g. Open Sans VF: ``wght``). Slot overrides ``nickname_style`` and ``fonts``."""
    fc = fonts_config if isinstance(fonts_config, dict) else {}
    if slot and isinstance(slot.get("font_variation_axes"), list) and slot["font_variation_axes"]:
        try:
            return [float(x) for x in slot["font_variation_axes"]]
        except (TypeError, ValueError):
            pass

    weight: int | None = None
    ns = nickname_style if isinstance(nickname_style, dict) else None

    def _weight_from_dict(d: dict | None) -> int | None:
        if not d:
            return None
        if d.get("bold") is True:
            return 700
        if d.get("font_weight") is not None:
            try:
                return int(d["font_weight"])
            except (TypeError, ValueError):
                return None
        return None

    weight = _weight_from_dict(slot if isinstance(slot, dict) else None)
    if weight is None:
        weight = _weight_from_dict(ns)
    if weight is None:
        weight = _weight_from_dict(fc)

    if weight is not None:
        w = max(1, min(1000, int(weight)))
        return [float(w)]
    return None


def _draw_text_with_optional_stroke(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    slot: dict,
) -> None:
    """Optional ``text_stroke_width`` / ``text_stroke_color`` thicken non-variable fonts."""
    sw_raw = slot.get("text_stroke_width")
    extra: dict = {}
    if sw_raw is not None:
        try:
            sw = int(sw_raw)
            if sw > 0:
                extra["stroke_width"] = sw
                extra["stroke_fill"] = _to_rgba(slot.get("text_stroke_color"), (0, 0, 0, 200))
        except (TypeError, ValueError):
            pass
    draw.text(xy, text, font=font, fill=fill, **extra)


def _weapon_sprite_shadow_layer(
    weapon_rgba: Image.Image, shadow_color: tuple[int, int, int, int]
) -> Image.Image:
    """Silhouette in ``shadow_color``; alpha is weapon alpha scaled by shadow A."""
    alpha = weapon_rgba.getchannel("A")
    r, g, b, sa = shadow_color
    r, g, b = int(r), int(g), int(b)
    sa = max(0, min(255, int(sa)))
    w, h = weapon_rgba.size
    if sa == 0:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mult_table = [min(255, int(i * sa / 255)) for i in range(256)]
    new_a = alpha.point(mult_table)
    return Image.merge(
        "RGBA",
        (
            Image.new("L", (w, h), r),
            Image.new("L", (w, h), g),
            Image.new("L", (w, h), b),
            new_a,
        ),
    )


def _paste_weapon_centered(base: Image.Image, weapon: Image.Image, slot: dict) -> None:
    cx = int(slot.get("x", 900))
    cy = int(slot.get("y", 210))
    mw = int(slot.get("max_width", 300))
    mh = int(slot.get("max_height", 130))
    w, h = weapon.size
    if w <= 0 or h <= 0 or mw <= 0 or mh <= 0:
        return
    weapon_rgba = weapon.convert("RGBA") if weapon.mode != "RGBA" else weapon
    if bool(slot.get("stretch_to_max_width", False)):
        nw = max(1, mw)
        nh_prop = max(1, int(round(h * (nw / w))))
        if nh_prop <= mh:
            nh = nh_prop
        else:
            nh = max(1, mh)
        weapon_resized = weapon_rgba.resize((nw, nh), Image.Resampling.LANCZOS)
    else:
        scale = min(mw / w, mh / h)
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        weapon_resized = weapon_rgba.resize((nw, nh), Image.Resampling.LANCZOS)
    left = cx - nw // 2
    top = cy - nh // 2
    if bool(slot.get("shadow", False)):
        shadow_ox = int(slot.get("shadow_offset_x", 4))
        shadow_oy = int(slot.get("shadow_offset_y", 4))
        shadow_rgba = _to_rgba(slot.get("shadow_color"), (0, 0, 0, 160))
        shadow_layer = _weapon_sprite_shadow_layer(weapon_resized, shadow_rgba)
        base.alpha_composite(shadow_layer, (left + shadow_ox, top + shadow_oy))
    base.alpha_composite(weapon_resized, (left, top))


def _draw_name_by_slot(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    text: str,
    slot: dict,
    default_uppercase: bool = False,
    fonts_config: dict | None = None,
    assets_dir: Path | None = None,
    nickname_style: dict | None = None,
) -> None:
    fonts_config = fonts_config if isinstance(fonts_config, dict) else {}
    uppercase = bool(slot.get("uppercase", default_uppercase))
    if uppercase:
        text = text.upper()
    text = _truncate_nickname_display(text, nickname_style, slot)

    x = int(slot.get("x", 0))
    y = int(slot.get("y", 0))
    max_width = int(slot.get("max_width", 650))
    font_size = int(slot.get("font_size", 42))
    min_font_size = int(slot.get("min_font_size", 18))
    shadow = bool(slot.get("shadow", True))
    shadow_ox = int(slot.get("shadow_offset_x", 2))
    shadow_oy = int(slot.get("shadow_offset_y", 2))
    color = _to_rgba(slot.get("color"), (245, 248, 255, 255))
    shadow_color = _to_rgba(slot.get("shadow_color"), (0, 0, 0, 190))
    bg_enabled = bool(slot.get("background_enabled", True))
    bg_color = _to_rgba(slot.get("background_color"), (10, 16, 28, 155))
    bg_padding_x = int(slot.get("background_padding_x", 16))
    bg_padding_y = int(slot.get("background_padding_y", 8))
    bg_radius = int(slot.get("background_radius", 10))

    candidates = _font_candidates_for_slot(slot, fonts_config)
    variation_axes = _resolve_font_variation_axes(slot, fonts_config, nickname_style)
    font = _fit_font_size_with_candidates(
        draw,
        text,
        max_width=max_width,
        initial_size=font_size,
        min_size=min_font_size,
        candidates=candidates,
        assets_dir=assets_dir,
        variation_axes=variation_axes,
    )
    text_bbox_at_origin = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox_at_origin[2] - text_bbox_at_origin[0]
    text_height = text_bbox_at_origin[3] - text_bbox_at_origin[1]
    text_x = x - text_width // 2
    text_y = y - text_height // 2

    if bg_enabled:
        text_bbox = draw.textbbox((text_x, text_y), text, font=font)
        left, top, right, bottom = text_bbox
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(
            (
                left - bg_padding_x,
                top - bg_padding_y,
                right + bg_padding_x,
                bottom + bg_padding_y,
            ),
            radius=bg_radius,
            fill=bg_color,
        )
        image.alpha_composite(overlay)
    if shadow:
        draw.text((text_x + shadow_ox, text_y + shadow_oy), text, font=font, fill=shadow_color)
    _draw_text_with_optional_stroke(
        draw,
        (text_x, text_y),
        text,
        font=font,
        fill=color,
        slot=slot,
    )


def _draw_footer_slot(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    slot: dict,
    fonts_config: dict | None,
    assets_dir: Path | None,
    format_vars: dict[str, str],
    weapon_for_accent: str,
) -> None:
    if not bool(slot.get("enabled", True)):
        return

    fmt = str(slot.get("format", ""))
    try:
        text = fmt.format(**format_vars)
    except (KeyError, ValueError):
        text = next(iter(format_vars.values()), "")

    max_w = int(slot.get("max_width", image.size[0]))
    max_width = min(max_w, max(32, image.size[0] - 8))
    font_size = int(slot.get("font_size", 26))
    min_font_size = int(slot.get("min_font_size", 16))
    shadow = bool(slot.get("shadow", False))
    shadow_ox = int(slot.get("shadow_offset_x", 2))
    shadow_oy = int(slot.get("shadow_offset_y", 2))
    shadow_color = _to_rgba(slot.get("shadow_color"), (0, 0, 0, 180))

    use_accent = bool(slot.get("use_weapon_accent_color", False))
    if use_accent and slot.get("color") is None:
        fill_color: tuple[int, int, int, int] = _weapon_accent_color(weapon_for_accent)
    else:
        fill_color = _to_rgba(slot.get("color"), (220, 239, 255, 255))

    candidates = _font_candidates_for_slot(slot, fonts_config if isinstance(fonts_config, dict) else {})
    variation_axes = _resolve_font_variation_axes(slot, fonts_config if isinstance(fonts_config, dict) else None)
    clip_h_raw = slot.get("clip_max_height")
    try:
        clip_h = int(clip_h_raw) if clip_h_raw is not None else None
    except (TypeError, ValueError):
        clip_h = None

    if clip_h is not None and clip_h > 0:
        font = _load_text_font_at_size(min_font_size, candidates, assets_dir, variation_axes)
        for size in range(font_size, min_font_size - 1, -2):
            ftry = _load_text_font_at_size(size, candidates, assets_dir, variation_axes)
            bbox_try = draw.textbbox((0, 0), text, font=ftry)
            tw = bbox_try[2] - bbox_try[0]
            th = bbox_try[3] - bbox_try[1]
            if tw <= max_width and th <= clip_h:
                font = ftry
                break
    else:
        font = _fit_font_size_with_candidates(
            draw,
            text,
            max_width=max_width,
            initial_size=font_size,
            min_size=min_font_size,
            candidates=candidates,
            assets_dir=assets_dir,
            variation_axes=variation_axes,
        )
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    anchor = str(slot.get("text_align", "left")).lower()
    if slot.get("x") is not None:
        try:
            x_ref = int(slot["x"])
        except (TypeError, ValueError):
            x_ref = image.size[0] // 2 if bool(slot.get("canvas_center_x", False)) else PADDING_X
    elif bool(slot.get("canvas_center_x", False)):
        x_ref = image.size[0] // 2
    else:
        x_ref = int(slot.get("x", PADDING_X))

    y_anchor = str(slot.get("y_anchor", "top")).lower()
    if slot.get("y") is not None:
        try:
            y_raw = int(slot["y"])
        except (TypeError, ValueError):
            y_raw = None
    else:
        y_raw = None

    if y_raw is not None:
        if y_anchor == "center":
            text_y = y_raw - text_h // 2
        elif y_anchor in ("bottom", "baseline"):
            text_y = y_raw - text_h
        else:
            text_y = y_raw
    else:
        margin_bottom = int(slot.get("margin_bottom", 48))
        text_y = int(image.size[1]) - margin_bottom - text_h

    if anchor == "center":
        text_x = x_ref - text_w // 2
    elif anchor == "right":
        text_x = x_ref - text_w
    else:
        text_x = x_ref

    if shadow:
        draw.text((text_x + shadow_ox, text_y + shadow_oy), text, font=font, fill=shadow_color)
    _draw_text_with_optional_stroke(
        draw,
        (text_x, text_y),
        text,
        font=font,
        fill=fill_color,
        slot=slot,
    )


def _format_distance_whole_meters(distance_meters: str) -> str:
    s = str(distance_meters).strip().replace(",", ".")
    try:
        return str(int(round(float(s))))
    except ValueError:
        return str(distance_meters).strip()


def render_killfeed_card(
    *,
    player_name: str,
    killer_name: str,
    weapon_name: str,
    distance_meters: str,
    base_image_path: Path | None = None,
    layout_config_path: Path | None = None,
    assets_dir: Path | None = None,
    weapons_dir: Path | None = None,
) -> io.BytesIO:
    layout, nickname_style, fonts, canvas = _load_killfeed_layout_bundle(layout_config_path)

    image = _build_killfeed_canvas(base_image_path, canvas)
    draw = ImageDraw.Draw(image)
    uppercase_all = bool(nickname_style.get("uppercase", False))

    _draw_name_by_slot(
        image,
        draw,
        text=player_name,
        slot=layout["victim"],
        default_uppercase=uppercase_all,
        fonts_config=fonts,
        assets_dir=assets_dir,
        nickname_style=nickname_style,
    )
    _draw_name_by_slot(
        image,
        draw,
        text=killer_name,
        slot=layout["killer"],
        default_uppercase=uppercase_all,
        fonts_config=fonts,
        assets_dir=assets_dir,
        nickname_style=nickname_style,
    )

    resolved_weapons_dir = (
        weapons_dir
        if weapons_dir is not None
        else (assets_dir if assets_dir is not None else Path("assets")) / "weapons"
    )
    weapon_path = _resolve_weapon_image_path(weapon_name, resolved_weapons_dir)
    weapon_sprite_ok = False
    if weapon_path is not None:
        try:
            with Image.open(weapon_path) as weapon_img:
                _paste_weapon_centered(image, weapon_img, layout["weapon"])
                weapon_sprite_ok = True
        except OSError:
            logger.warning("Failed to open weapon image: %s", weapon_path)
    distance_display = _format_distance_whole_meters(distance_meters)
    fmt_vars = {"distance": distance_display, "weapon": weapon_name}
    _draw_footer_slot(
        image,
        draw,
        slot=layout["footer_distance"],
        fonts_config=fonts,
        assets_dir=assets_dir,
        format_vars=fmt_vars,
        weapon_for_accent=weapon_name,
    )
    if not weapon_sprite_ok and bool(layout["footer_weapon"].get("enabled", True)):
        _draw_footer_slot(
            image,
            draw,
            slot=layout["footer_weapon"],
            fonts_config=fonts,
            assets_dir=assets_dir,
            format_vars=fmt_vars,
            weapon_for_accent=weapon_name,
        )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output
