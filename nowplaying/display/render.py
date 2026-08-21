"""The render loop — owns all drawing, runs on the main thread."""

import time
import logging
import threading
from datetime import datetime, time as dtime

from PIL import Image, ImageDraw
from rgbmatrix import RGBMatrix, graphics

from nowplaying import config
from nowplaying.display.state import State
from nowplaying.display.matrix import (
    load_fonts, text_width, compute_text_layout, wrap_two_lines, format_remaining,
)

log = logging.getLogger("plex-matrix")


def is_within_schedule() -> bool:
    """Return True if the current local time falls within the active window."""
    if not config.SCHEDULE_ENABLE:
        return True
    now = datetime.now().time().replace(second=0, microsecond=0)
    start, stop = config.SCHEDULE_START, config.SCHEDULE_STOP
    if start <= stop:
        # Normal window: e.g. 07:00 – 23:00 (stop=00:00 treated as end-of-day)
        # Special case: stop at midnight means active until end of day
        if stop == dtime(0, 0):
            return now >= start
        return start <= now < stop
    else:
        # Overnight window: e.g. 22:00 – 06:00
        return now >= start or now < stop


def make_paused_poster(img: Image.Image) -> Image.Image:
    """Dim the poster and stamp a centered pause glyph over it.

    Built once per session and cached — this is far too slow to run per frame.
    """
    out = img.point(lambda v: int(v * config.PAUSE_DIM))
    w, h = out.size
    bar_w = max(3, w // 9)
    bar_h = max(8, h // 3)
    gap   = max(2, w // 10)
    top   = (h - bar_h) // 2
    left  = (w - (2 * bar_w + gap)) // 2
    draw  = ImageDraw.Draw(out)
    for i in range(2):
        x0 = left + i * (bar_w + gap)
        draw.rectangle([x0, top, x0 + bar_w - 1, top + bar_h - 1], fill=config.PAUSE_FG)
    return out


def render_loop(matrix: RGBMatrix, state: State, stop: threading.Event):
    _font_big, _font_sm, _font_sub, _font_clk = load_fonts()
    title_y, sub_y, user_y, rem_y = compute_text_layout(
        [_font_big, _font_sub, _font_sm, _font_sm])
    log.info("Layout: title y=%d, subtitle y=%d, user y=%d, remaining y=%d",
             title_y, sub_y, user_y, rem_y)

    canvas = matrix.CreateFrameCanvas()
    white  = graphics.Color(220, 220, 220)
    clk_c  = graphics.Color(*config.IDLE_DIM)
    sub_c  = graphics.Color(160, 160, 160)
    user_c = graphics.Color(120, 120, 200)

    scroll_offset     = 0
    scroll_dir        = 1
    scroll_pause_until = 0.0
    last_session_key  = None
    last_frame        = time.monotonic()
    last_brightness   = config.BRIGHTNESS_NORMAL
    was_active        = True   # track transitions for log clarity

    while not stop.is_set():
        now = time.monotonic()
        dt = now - last_frame
        if dt < config.SCROLL_FRAME_MS / 1000:
            time.sleep((config.SCROLL_FRAME_MS / 1000) - dt)
        last_frame = time.monotonic()

        # ── Schedule gate ──────────────────────────────────────────────────
        active = is_within_schedule()
        if not active:
            if was_active:
                matrix.Clear()
                log.info("Schedule: outside window, display off (%s–%s)",
                         config.SCHEDULE_START.strftime("%H:%M"),
                         config.SCHEDULE_STOP.strftime("%H:%M"))
            was_active = False
            # Sleep longer while blanked — no need to spin at 20fps
            stop.wait(30)
            continue

        if not was_active:
            log.info("Schedule: inside window, display on")
        was_active = True

        # ── Brightness ─────────────────────────────────────────────────────
        state.maybe_cycle()
        with state.lock:
            should_dim = state.dim
        target_brightness = config.BRIGHTNESS_DIM if should_dim else config.BRIGHTNESS_NORMAL
        if target_brightness != last_brightness:
            matrix.brightness = target_brightness
            last_brightness = target_brightness
            log.info("Brightness → %d", target_brightness)

        # ── Draw ───────────────────────────────────────────────────────────
        canvas.Clear()
        current = state.current()

        if current is None:
            t  = time.strftime("%I:%M %p")
            x  = (128 - text_width(_font_clk, t)) // 2
            graphics.DrawText(canvas, _font_clk, x, 13, clk_c, t)
            msg = "Nothing playing"
            x2  = (128 - text_width(_font_sm, msg)) // 2
            graphics.DrawText(canvas, _font_sm, x2, 46, graphics.Color(60, 60, 60), msg)
        else:
            RX = 64
            sub_max_chars = 64 // 5

            title = current.title or "—"
            tw    = text_width(_font_big, title)
            if tw <= 64:
                graphics.DrawText(canvas, _font_big,
                                  RX + max(0, (64 - tw) // 2), title_y, white, title)
            else:
                if current.session_key != last_session_key:
                    scroll_offset      = 0
                    scroll_dir         = 1
                    scroll_pause_until = now + (config.SCROLL_PAUSE_MS / 1000)
                if now >= scroll_pause_until:
                    scroll_offset += config.SCROLL_PX_PER_FRAME * scroll_dir
                    if scroll_offset >= (tw - 64):
                        scroll_offset      = tw - 64
                        scroll_dir         = -1
                        scroll_pause_until = now + (config.SCROLL_PAUSE_MS / 1000)
                    elif scroll_offset <= 0:
                        scroll_offset      = 0
                        scroll_dir         = 1
                        scroll_pause_until = now + (config.SCROLL_PAUSE_MS / 1000)
                graphics.DrawText(canvas, _font_big, RX - scroll_offset, title_y, white, title)

            last_session_key = current.session_key

            if current.poster is not None:
                poster = current.poster
                if current.state == "paused":
                    if current.poster_paused is None:
                        current.poster_paused = make_paused_poster(poster)
                    poster = current.poster_paused
                canvas.SetImage(poster, 0, 0)
            else:
                for py in range(64):
                    for px in range(64):
                        if (px + py) % 8 == 0:
                            canvas.SetPixel(px, py, 30, 30, 30)

            sub_text = current.subtitle or ""
            sw = text_width(_font_sub, sub_text)
            if sw <= 64:
                graphics.DrawText(canvas, _font_sub,
                                  RX + max(0, (64 - sw) // 2), sub_y, sub_c, sub_text)
            else:
                # Two small lines fill exactly the block the single large line
                # would have occupied, so nothing collides with the row below.
                block_top = sub_y - _font_sub.baseline
                line1_y   = block_top + _font_sm.baseline
                line1, line2 = wrap_two_lines(sub_text, sub_max_chars)
                graphics.DrawText(canvas, _font_sm, RX + 1, line1_y, sub_c, line1)
                if line2:
                    graphics.DrawText(canvas, _font_sm, RX + 1,
                                      line1_y + _font_sm.height, sub_c, line2)

            user = (current.user or "")[:sub_max_chars]
            graphics.DrawText(canvas, _font_sm, RX + 1, user_y, user_c, user)

            remaining = format_remaining(current.duration_ms, current.view_offset_ms)
            if remaining:
                graphics.DrawText(canvas, _font_sm, RX + 1, rem_y,
                                  graphics.Color(*config.REMAIN_FG), remaining)

            bar_x0, bar_x1 = 65, 126
            bar_y0, bar_y1 = config.BAR_Y0, config.BAR_Y1
            for bx in range(bar_x0, bar_x1 + 1):
                for by in range(bar_y0, bar_y1 + 1):
                    canvas.SetPixel(bx, by, *config.PROGRESS_BG)
            fill = int((bar_x1 - bar_x0) * current.progress)
            for bx in range(bar_x0, bar_x0 + fill + 1):
                for by in range(bar_y0, bar_y1 + 1):
                    canvas.SetPixel(bx, by, *config.PROGRESS_FG)

            idx, n = state.cycle_position()
            if n > 1:
                for i in range(n):
                    dx    = 127 - (n - 1 - i) * 3
                    color = (200, 200, 200) if i == idx else (60, 60, 60)
                    canvas.SetPixel(dx, config.DOTS_Y, *color)
                    canvas.SetPixel(dx, config.DOTS_Y + 1, *color)

        canvas = matrix.SwapOnVSync(canvas)
