from __future__ import annotations

import math
import os
from io import BytesIO
from pathlib import Path
from datetime import timezone
from urllib.parse import urlencode, urlparse

from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaPhoto
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from bot.models import Market, MarketStatus


CANVAS_WIDTH = 1200
CANVAS_HEIGHT = 960
CARD_MARGIN = 42
CARD_RADIUS = 42

NAVY = (14, 35, 61)
NAVY_SOFT = (64, 83, 107)
TEXT = (20, 33, 52)
MUTED = (104, 121, 143)
MUTED_LIGHT = (184, 197, 210)
WHITE = (255, 255, 255)
PANEL = (248, 252, 255)
PANEL_2 = (236, 246, 251)
LINE = (218, 230, 240)
TRACK = (226, 235, 243)
TRACK_MUTED = (212, 222, 232)
ACCENT = (44, 163, 228)
ACCENT_2 = (78, 204, 204)
GOOD = (74, 190, 120)
GOLD = (244, 193, 67)
BAD = (176, 191, 207)

ARIAL = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
ARIAL_BOLD = Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")
ARIAL_ITALIC = Path("/System/Library/Fonts/Supplemental/Arial Italic.ttf")
ARIAL_BOLD_ITALIC = Path("/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf")

STATUS_LABELS = {
    MarketStatus.ACTIVE: "OPEN",
    MarketStatus.CLOSED: "CLOSED",
    MarketStatus.RESOLVED: "RESOLVED",
    MarketStatus.CANCELLED: "CANCELLED",
    MarketStatus.DISPUTED: "DISPUTED",
}


def build_market_card_caption(market: Market) -> str:
    label = STATUS_LABELS.get(market.status, market.status.value.upper())
    if market.status == MarketStatus.RESOLVED and market.winning_option is not None:
        winning_label = _safe_option_label(market, market.winning_option)
        return f"Poolr market #{market.id} · {label}: {winning_label}"
    return f"Poolr market #{market.id} · {label}"


def build_market_card_photo(
    market: Market,
    pool_by_option: dict[int, int],
) -> BufferedInputFile:
    image = render_market_card_image(market, pool_by_option)
    return BufferedInputFile(image, filename=f"market-{market.id}.png")


def build_market_card_media(
    market: Market,
    pool_by_option: dict[int, int],
    photo_url: str | None = None,
) -> InputMediaPhoto:
    return InputMediaPhoto(
        media=photo_url or build_market_card_photo(market, pool_by_option),
        caption=build_market_card_caption(market),
    )


def build_market_card_image_url(
    public_base_url: str,
    market: Market,
    pool_by_option: dict[int, int] | None = None,
) -> str:
    version_parts = [market.status.value]
    if market.winning_option is not None:
        version_parts.append(str(market.winning_option))
    if pool_by_option is not None:
        version_parts.append(str(sum(pool_by_option.values())))

    query = urlencode({"v": "-".join(version_parts)})
    return f"{public_base_url.rstrip('/')}/api/market/{market.id}/card.png?{query}"


def resolve_public_base_url(webhook_url: str | None = None) -> str | None:
    raw_url = webhook_url or os.getenv("PUBLIC_BASE_URL") or os.getenv("WEBHOOK_URL")
    if not raw_url:
        return None
    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return parsed._replace(path="", params="", query="", fragment="").geturl()


async def send_market_card_photo(
    bot: Bot,
    chat_id: int,
    market: Market,
    pool_by_option: dict[int, int],
    reply_markup,
    fallback_text: str | None = None,
) -> object:
    try:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=build_market_card_photo(market, pool_by_option),
            caption=build_market_card_caption(market),
            reply_markup=reply_markup,
        )
    except Exception as exc:
        if fallback_text is None:
            raise
        return await bot.send_message(
            chat_id=chat_id,
            text=fallback_text,
            reply_markup=reply_markup,
        )


async def update_market_card_photo(
    bot: Bot,
    *,
    market: Market,
    pool_by_option: dict[int, int],
    reply_markup,
    chat_id: int | None = None,
    message_id: int | None = None,
    inline_message_id: str | None = None,
    photo_url: str | None = None,
    fallback_text: str | None = None,
) -> None:
    if inline_message_id and photo_url is None:
        public_base_url = resolve_public_base_url()
        if public_base_url is not None:
            photo_url = build_market_card_image_url(public_base_url, market, pool_by_option)
    media = build_market_card_media(market, pool_by_option, photo_url=photo_url)
    try:
        if inline_message_id:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=media,
                reply_markup=reply_markup,
            )
            return
        if message_id is None or chat_id is None:
            raise ValueError("chat_id and message_id are required for chat message edits")
        await bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=media,
            reply_markup=reply_markup,
        )
        return
    except Exception:
        if fallback_text is None:
            raise
        if inline_message_id:
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=fallback_text,
                reply_markup=reply_markup,
            )
            return
        if message_id is None or chat_id is None:
            raise
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=fallback_text,
            reply_markup=reply_markup,
        )


def render_market_card_image(
    market: Market,
    pool_by_option: dict[int, int],
) -> bytes:
    total_pool = sum(pool_by_option.values())
    option_count = max(1, len(market.options))
    status_label = STATUS_LABELS.get(market.status, market.status.value.upper())
    measure_canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), "white")
    measure_draw = ImageDraw.Draw(measure_canvas)

    inner_left = CARD_MARGIN + 36
    inner_right = CANVAS_WIDTH - CARD_MARGIN - 36
    cursor_y = CARD_MARGIN + 34

    brand_font = _load_font(24, bold=False)
    market_font = _load_font(30, bold=True)
    status_font = _load_font(24, bold=True)
    question_font, question_lines = _fit_question_font(measure_draw, market.question, inner_right - inner_left)
    metric_label_font = _load_font(18, bold=True)
    metric_value_font = _load_font(29, bold=True)
    if option_count >= 5:
        option_label_font = _load_font(21, bold=True)
        option_value_font = _load_font(18, bold=True)
        row_height = 72
        row_gap = 10
    elif option_count == 4:
        option_label_font = _load_font(22, bold=True)
        option_value_font = _load_font(19, bold=True)
        row_height = 78
        row_gap = 12
    else:
        option_label_font = _load_font(23, bold=True)
        option_value_font = _load_font(20, bold=True)
        row_height = 94
        row_gap = 14
    footer_font = _load_font(22, bold=True)

    question_spacing = max(8, int(question_font.size * 0.14))
    question_top = CARD_MARGIN + 150
    question_text = "\n".join(question_lines)
    question_bbox = measure_draw.multiline_textbbox(
        (inner_left, question_top),
        question_text,
        font=question_font,
        spacing=question_spacing,
    )
    cursor_y = question_bbox[3] + 16

    metric_width = math.floor((inner_right - inner_left - 24) / 3)
    metrics_height = 96
    metric_boxes = [
        (inner_left, cursor_y, inner_left + metric_width, cursor_y + metrics_height),
        (inner_left + metric_width + 12, cursor_y, inner_left + metric_width * 2 + 12, cursor_y + metrics_height),
        (inner_right - metric_width, cursor_y, inner_right, cursor_y + metrics_height),
    ]
    metrics = [
        ("POOL", f"{total_pool} Stars"),
        ("MIN STAKE", f"{market.min_bet} Stars"),
        ("DEADLINE", _format_deadline(market.deadline)),
    ]
    for box, (label, value) in zip(metric_boxes, metrics, strict=True):
        value_lines = _wrap_text(measure_draw, value, metric_value_font, box[2] - box[0] - 36)
        _draw_multiline_text(measure_draw, value_lines, metric_value_font, (box[0] + 18, box[1] + 38), TEXT, 2)

    cursor_y += metrics_height + 22

    options_header_font = _load_font(22, bold=True)
    options_header_y = cursor_y
    options_block_height = option_count * row_height + max(0, option_count - 1) * row_gap
    required_height = options_header_y + 34 + options_block_height + 20 + 54 + CARD_MARGIN + 24
    canvas_height = max(CANVAS_HEIGHT, required_height)
    background = _build_background(canvas_height)
    shadow = Image.new("RGBA", background.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    card_box = (
        CARD_MARGIN,
        CARD_MARGIN,
        CANVAS_WIDTH - CARD_MARGIN,
        canvas_height - CARD_MARGIN,
    )
    shadow_draw.rounded_rectangle(
        (card_box[0] + 12, card_box[1] + 18, card_box[2] + 12, card_box[3] + 18),
        radius=CARD_RADIUS + 6,
        fill=(0, 14, 28, 110),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))

    canvas = Image.alpha_composite(background, shadow)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(card_box, radius=CARD_RADIUS, fill=PANEL, outline=(255, 255, 255, 220), width=2)

    # Repaint the measured content onto the final canvas.
    _draw_pill(draw, (inner_left, CARD_MARGIN + 34, inner_left + 184, CARD_MARGIN + 76), PANEL_2, TEXT, "via @pooolr_bot", brand_font)
    _draw_pill(
        draw,
        (inner_right - 192, CARD_MARGIN + 34, inner_right, CARD_MARGIN + 76),
        _status_color(market.status),
        WHITE,
        status_label,
        status_font,
    )
    draw.text((inner_left, CARD_MARGIN + 96), f"Poolr market #{market.id}", font=market_font, fill=TEXT)
    draw.multiline_text((inner_left, question_top), question_text, font=question_font, fill=TEXT, spacing=question_spacing)
    for box, (label, value) in zip(metric_boxes, metrics, strict=True):
        draw.rounded_rectangle(box, radius=24, fill=WHITE, outline=LINE, width=2)
        draw.text((box[0] + 18, box[1] + 14), label, font=metric_label_font, fill=MUTED)
        value_lines = _wrap_text(draw, value, metric_value_font, box[2] - box[0] - 36)
        _draw_multiline_text(draw, value_lines, metric_value_font, (box[0] + 18, box[1] + 38), TEXT, 2)
    draw.text((inner_left, options_header_y), "Options", font=options_header_font, fill=NAVY)
    cursor_y = options_header_y + 34

    max_bar_width = inner_right - inner_left - 540
    bar_left = inner_left + 350
    bar_right = bar_left + max_bar_width

    for index, option in enumerate(market.options):
        option_pool = pool_by_option.get(index, 0)
        pct = int(round((option_pool / total_pool) * 100)) if total_pool else 0
        top = cursor_y + index * (row_height + row_gap)
        bottom = top + row_height
        is_winner = market.status == MarketStatus.RESOLVED and market.winning_option == index
        row_fill = (243, 250, 255) if not is_winner else (242, 250, 239)
        row_outline = (219, 231, 240) if not is_winner else (201, 228, 193)
        draw.rounded_rectangle((inner_left, top, inner_right, bottom), radius=24, fill=row_fill, outline=row_outline, width=2)

        label = f"{index + 1}. {option}"
        draw.text((inner_left + 18, top + 16), label, font=option_label_font, fill=TEXT)
        draw.text((inner_left + 18, top + 50), f"{option_pool} Stars", font=option_value_font, fill=MUTED)

        track_top = top + 32
        track_bottom = track_top + 26
        track_fill = TRACK if total_pool else TRACK_MUTED
        draw.rounded_rectangle((bar_left, track_top, bar_right, track_bottom), radius=13, fill=track_fill)

        fill_ratio = (option_pool / total_pool) if total_pool else 0.0
        fill_width = max(0, int((bar_right - bar_left) * fill_ratio))
        if fill_width > 0:
            fill_color = GOLD if is_winner else (GOOD if market.status == MarketStatus.RESOLVED else ACCENT)
            draw.rounded_rectangle((bar_left, track_top, bar_left + fill_width, track_bottom), radius=13, fill=fill_color)
        elif market.status == MarketStatus.RESOLVED and is_winner:
            draw.rounded_rectangle((bar_left, track_top, bar_left + 8, track_bottom), radius=13, fill=GOLD)

        right_anchor = inner_right - 18
        draw.text(
            (right_anchor - _text_width(draw, f"{pct}%", option_label_font), top + 15),
            f"{pct}%",
            font=option_label_font,
            fill=NAVY_SOFT if not is_winner else (103, 134, 82),
        )
        draw.text(
            (right_anchor - _text_width(draw, f"{option_pool} Stars", option_value_font), top + 50),
            f"{option_pool} Stars",
            font=option_value_font,
            fill=MUTED if not is_winner else (99, 127, 78),
        )

    cursor_y += option_count * (row_height + row_gap) - row_gap
    cursor_y += 20

    footer_text = _footer_text(market)
    footer_box = (inner_left, cursor_y, inner_right, cursor_y + 54)
    draw.rounded_rectangle(footer_box, radius=20, fill=WHITE, outline=LINE, width=2)
    draw.text((footer_box[0] + 20, footer_box[1] + 15), footer_text, font=footer_font, fill=NAVY)

    buffer = BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _build_background(height: int) -> Image.Image:
    background = Image.new("RGBA", (CANVAS_WIDTH, height), (7, 28, 49, 255))
    draw = ImageDraw.Draw(background)
    top_color = (12, 45, 73)
    bottom_color = (28, 158, 194)
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line((0, y, CANVAS_WIDTH, y), fill=_mix(top_color, bottom_color, t))

    overlay = Image.new("RGBA", (CANVAS_WIDTH, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.ellipse((-220, -120, 560, 560), fill=(122, 216, 255, 70))
    overlay_draw.ellipse((780, -140, 1330, 390), fill=(255, 255, 255, 56))
    overlay_draw.ellipse((650, 550, 1180, 1080), fill=(255, 221, 130, 44))
    overlay_draw.ellipse((-120, 520, 360, 980), fill=(79, 210, 196, 36))
    overlay = overlay.filter(ImageFilter.GaussianBlur(40))
    return Image.alpha_composite(background, overlay)


def _footer_text(market: Market) -> str:
    if market.status == MarketStatus.ACTIVE:
        return "Tap a button below to bet"
    if market.status == MarketStatus.CLOSED:
        return "Betting closed. Waiting for creator resolution"
    if market.status == MarketStatus.RESOLVED:
        if market.winning_option is not None:
            return f"Winner: {_safe_option_label(market, market.winning_option)}"
        return "Resolved"
    if market.status == MarketStatus.CANCELLED:
        return "Cancelled. Stakes refunded"
    if market.status == MarketStatus.DISPUTED:
        return "Under dispute review"
    return market.status.value.replace("_", " ").title()


def _status_color(status: MarketStatus) -> tuple[int, int, int]:
    if status == MarketStatus.ACTIVE:
        return ACCENT
    if status == MarketStatus.CLOSED:
        return NAVY_SOFT
    if status == MarketStatus.RESOLVED:
        return GOOD
    if status == MarketStatus.CANCELLED:
        return BAD
    if status == MarketStatus.DISPUTED:
        return GOLD
    return ACCENT_2


def _safe_option_label(market: Market, option_index: int) -> str:
    if 0 <= option_index < len(market.options):
        return market.options[option_index]
    return "Unknown"


def _format_deadline(deadline) -> str:
    return deadline.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _load_font(size: int, bold: bool = False, italic: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = ARIAL
    if bold and italic:
        font_path = ARIAL_BOLD_ITALIC
    elif bold:
        font_path = ARIAL_BOLD
    elif italic:
        font_path = ARIAL_ITALIC
    try:
        return ImageFont.truetype(str(font_path), size=size)
    except Exception:
        return ImageFont.load_default()


def _mix(color_a: tuple[int, int, int], color_b: tuple[int, int, int], t: float) -> tuple[int, int, int, int]:
    return tuple(
        int(round(component_a + (component_b - component_a) * t))
        for component_a, component_b in zip(color_a, color_b, strict=True)
    ) + (255,)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue

        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if _text_width(draw, candidate, font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _fit_question_font(draw: ImageDraw.ImageDraw, text: str, max_width: int):
    for size in range(64, 38, -2):
        font = _load_font(size, bold=True)
        lines = _wrap_text(draw, text, font, max_width)
        if len(lines) <= 3:
            return font, lines
    font = _load_font(38, bold=True)
    return font, _wrap_text(draw, text, font, max_width)


def _draw_multiline_text(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font,
    position: tuple[int, int],
    fill: tuple[int, int, int],
    spacing: int,
) -> int:
    x, y = position
    line_height = _line_height(draw, font)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height + spacing
    return y


def _line_height(draw: ImageDraw.ImageDraw, font) -> int:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return bbox[3] - bbox[1]


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int],
    text_fill: tuple[int, int, int],
    text: str,
    font,
) -> None:
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=fill)
    width = _text_width(draw, text, font)
    height = _line_height(draw, font)
    x = box[0] + (box[2] - box[0] - width) / 2
    y = box[1] + (box[3] - box[1] - height) / 2 - 1
    draw.text((x, y), text, font=font, fill=text_fill)
