"""Matrix construction, fonts, and text-measurement/layout helpers."""

import os
import logging
import tempfile

from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

from nowplaying import config

log = logging.getLogger("plex-matrix")


def build_matrix(mcfg: dict, brightness: int) -> RGBMatrix:
    """Construct the matrix from the config's `matrix` section.

    RGBMatrixOptions are constructor-only — changing any of them requires a
    service restart, which the settings UI arranges.
    """
    opts = RGBMatrixOptions()
    opts.rows = mcfg["rows"]
    opts.cols = mcfg["cols"]
    opts.chain_length = 1
    opts.parallel = 1
    opts.hardware_mapping = mcfg["hardware_mapping"]
    opts.gpio_slowdown = mcfg["gpio_slowdown"]
    opts.brightness = brightness
    opts.pwm_bits = mcfg["pwm_bits"]
    opts.pwm_lsb_nanoseconds = mcfg["pwm_lsb_nanoseconds"]
    opts.limit_refresh_rate_hz = mcfg["limit_refresh_rate_hz"]
    opts.drop_privileges = False
    opts.show_refresh_rate = False
    opts.scan_mode = 0
    return RGBMatrix(options=opts)


_pil_title_font = None
_pil_title_tried = False


def load_pil_title_font():
    """The title font again, compiled for PIL. None if that is not possible.

    PIL reads the same BDF the panel library does, and the two agree on width
    to the pixel — checked on the Pi across proportional strings, so the
    scroll bounds computed from `text_width` stay valid for a PIL-rendered
    strip. What PIL adds is supersampling and box filtering, which is the only
    way to put a glyph edge between two panel columns; `graphics.DrawText`
    lands on whole pixels only.

    Compiled once into a temp dir because `ImageFont.load` wants a file. A
    failure here is not fatal — the caller falls back to DrawText and the
    title scrolls in whole pixels, exactly as it used to.
    """
    global _pil_title_font, _pil_title_tried
    if _pil_title_tried:
        return _pil_title_font
    _pil_title_tried = True
    try:
        from PIL import ImageFont, BdfFontFile
        d = tempfile.mkdtemp(prefix="nowplaying-font-")
        with open(config.FONT_BIG, "rb") as fp:
            BdfFontFile.BdfFontFile(fp).save(os.path.join(d, "title"))
        _pil_title_font = ImageFont.load(os.path.join(d, "title.pil"))
        log.info("Sub-pixel title scrolling enabled (%s)", config.FONT_BIG)
    except Exception as exc:
        log.warning("No PIL build of %s (%s) — titles will scroll in whole "
                    "pixels", config.FONT_BIG, exc)
        _pil_title_font = None
    return _pil_title_font


def load_fonts():
    title   = graphics.Font(); title.LoadFont(config.FONT_BIG)
    sub     = graphics.Font(); sub.LoadFont(config.FONT_SM)
    sub_big = graphics.Font(); sub_big.LoadFont(config.FONT_SUB)
    clk     = graphics.Font(); clk.LoadFont(config.FONT_CLK)
    return title, sub, sub_big, clk


def wrap_two_lines(text: str, max_chars: int) -> tuple[str, str]:
    """Greedy-wrap `text` into at most two lines of `max_chars`.

    Words are placed in order and never revisited: once a word overflows line
    one, everything after it belongs to line two. Revisiting is what used to
    reorder subtitles — a short word following a long one would jump back up to
    fill the gap ("S04E03 Got" / "Richmond's T"). Anything that will not fit in
    two lines is cut short with an ellipsis.
    """
    lines = ["", ""]
    idx = 0
    for word in text.split():
        candidate = f"{lines[idx]} {word}".strip()
        if len(candidate) <= max_chars:
            lines[idx] = candidate
        elif idx == 0:
            idx = 1
            lines[idx] = word[:max_chars]
        else:
            lines[1] = lines[1][:max_chars - 1].rstrip() + "…"
            break
    return lines[0], lines[1]


def format_remaining(duration_ms: float, view_offset_ms: float) -> str:
    """Time left as '1h04m left' / '42m left' / '38s left'.

    Empty when the server reports no duration, which is normal for live TV.
    """
    if duration_ms <= 0:
        return ""
    secs = max(0, int((duration_ms - view_offset_ms) / 1000))
    hours, rem = divmod(secs, 3600)
    mins, s = divmod(rem, 60)
    if hours:
        return f"{hours}h{mins:02d}m left"
    if mins:
        return f"{mins}m left"
    return f"{s}s left"


def text_width(font, text: str) -> int:
    """Pixel width of `text` in `font`.

    Sums per-glyph advances rather than assuming a fixed cell, so proportional
    faces (helvR12) measure correctly and monospace ones still come out exact.
    """
    return sum(max(0, font.CharacterWidth(ord(ch))) for ch in text)


def compute_text_layout(fonts, bottom: int = None) -> list[int]:
    """Spread each font's text block evenly down the text region.

    Returns one baseline per font. Driven by real font metrics so that swapping
    a font rebalances the panel on its own, rather than needing the baselines
    to be re-tuned by hand.
    """
    if bottom is None:
        bottom = config.TEXT_REGION_BOTTOM
    total = sum(f.height for f in fonts)
    slots = len(fonts) + 1
    gap, extra = divmod(max(0, bottom - total), slots)
    baselines, y = [], 0
    for i, f in enumerate(fonts):
        # Hand the leftover pixels to the topmost gaps rather than dropping
        # them, so the block stays centred in the region.
        y += gap + (1 if i < extra else 0)
        baselines.append(y + f.baseline)
        y += f.height
    return baselines
