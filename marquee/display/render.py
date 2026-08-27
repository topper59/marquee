"""The render loop — owns all drawing, runs on the main thread."""

import time
import socket
import logging
import threading
from datetime import datetime, time as dtime

from PIL import Image, ImageDraw, ImageOps
from rgbmatrix import RGBMatrix, graphics

from marquee import config
from marquee.netmgr import local_ip
from marquee.display.state import State, DisplayMode
from marquee.display.matrix import (
    load_fonts, load_pil_title_font, text_width, compute_text_layout,
    wrap_two_lines, format_remaining,
)

log = logging.getLogger(__name__)

# One half is poster art, the other is text. `display.poster_side` swaps
# them; RX is the default text origin (poster left) and the fallback for
# code that has no config snapshot to hand.
RX = 64
TITLE_FG = (220, 220, 220)
AMBER = (229, 160, 13)
GRAY  = (150, 150, 150)
RED   = (200, 60, 50)


def _centered(canvas, font, y, color, text):
    x = max(0, (128 - text_width(font, text)) // 2)
    graphics.DrawText(canvas, font, x, y, graphics.Color(*color), text)


def draw_mode_screen(canvas, fonts, mode: DisplayMode, payload: dict,
                     accent: tuple = AMBER):
    """Full-screen status pages for setup/auth/reset. Called at ~2fps — a
    handful of DrawText calls, no caching needed.

    `accent` is the configured panel colour, so the setup and link screens
    match the display they are about to become. Errors stay red regardless.
    """
    _font_big, _font_sm, _font_sub, _font_clk = fonts

    if mode is DisplayMode.SETUP:
        _centered(canvas, _font_big, 12, accent, "Setup")
        _centered(canvas, _font_sm, 26, GRAY, "Join WiFi network:")
        _centered(canvas, _font_sm, 38, (235, 235, 235), payload.get("ssid", ""))
        _centered(canvas, _font_sm, 52, GRAY, "then visit " + payload.get("url", "10.42.0.1"))

    elif mode is DisplayMode.NEEDS_SETUP:
        # Online, but no Plex server chosen yet — point at the web UI. Both
        # the mDNS name and the raw IP are shown because .local resolution
        # fails on some networks and phones.
        _centered(canvas, _font_big, 14, accent, "Setup")
        _centered(canvas, _font_sm, 30, GRAY, "Open in a browser:")
        _centered(canvas, _font_sm, 42, (235, 235, 235), payload.get("host", ""))
        _centered(canvas, _font_sm, 54, GRAY, payload.get("ip", ""))

    elif mode is DisplayMode.CONNECTING:
        dots = "." * (1 + int(time.monotonic() * 2) % 3)
        _centered(canvas, _font_big, 12, accent, "WiFi")
        _centered(canvas, _font_sm, 32, GRAY, "Connecting to")
        _centered(canvas, _font_sm, 44, (235, 235, 235), payload.get("ssid", "") + dots)

    elif mode is DisplayMode.LINK_CODE:
        _centered(canvas, _font_sm, 10, GRAY, "Link your Plex account")
        # The code is the payload star — big font, letter-spaced by hand.
        code = payload.get("code", "")
        spaced = " ".join(code)
        _centered(canvas, _font_big, 34, accent, spaced)
        _centered(canvas, _font_sm, 52, (235, 235, 235), "enter at plex.tv/link")

    elif mode is DisplayMode.ERROR:
        _centered(canvas, _font_big, 12, RED, "Problem")
        line1, line2 = wrap_two_lines(payload.get("text", ""), 21)
        _centered(canvas, _font_sm, 30, GRAY, line1)
        if line2:
            _centered(canvas, _font_sm, 40, GRAY, line2)
        _centered(canvas, _font_sm, 54, GRAY, payload.get("hint", ""))

    elif mode is DisplayMode.INFO:
        y = 12
        for line in payload.get("lines", [])[:5]:
            _centered(canvas, _font_sm, y, (235, 235, 235), line)
            y += 11

    elif mode is DisplayMode.RESETTING:
        n = payload.get("seconds", 0)
        _centered(canvas, _font_big, 16, RED, f"Reset in {n}")
        _centered(canvas, _font_sm, 36, GRAY, "release button")
        _centered(canvas, _font_sm, 47, GRAY, "to cancel")


def _in_window(start: dtime, stop: dtime, now: dtime = None) -> bool:
    """Is `now` inside [start, stop)? Wraps around midnight when stop < start.

    Callers decide what equal endpoints mean — the two windows on this panel
    disagree, so that decision cannot live in here.
    """
    if now is None:
        now = datetime.now().time()
    now = now.replace(second=0, microsecond=0)
    if start <= stop:
        return start <= now < stop
    # Overnight window: e.g. 22:00 – 06:00, and 08:00 – 00:00 (end of day).
    return now >= start or now < stop


def is_within_schedule(start: dtime, stop: dtime, now: dtime = None) -> bool:
    """Is the display meant to be on? Equal endpoints mean always."""
    if start == stop:
        return True
    return _in_window(start, stop, now)


def is_within_dim_window(start: dtime, stop: dtime, now: dtime = None) -> bool:
    """Is the clock asking for the dim brightness? Equal endpoints mean never.

    The opposite default to is_within_schedule, and deliberately so: an
    unset schedule must leave the panel on, while an unset dim window must
    leave the brightness alone.
    """
    if start == stop:
        return False
    return _in_window(start, stop, now)


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


def _coverage_lut() -> list:
    """Map linear coverage 0..255 onto the panel value that emits it.

    The box filter in make_title_phases produces *coverage*: an edge covering
    40% of a column comes out at 0.4. Sending 0.4 straight to the panel is
    wrong, because the matrix library luminance-corrects every 8-bit value
    through the CIE1931 curve before it reaches the PWM. Two half-lit columns
    therefore emit about 44% of the light one fully lit column does, and a
    glyph stem visibly dims every time it slides off a whole pixel — which at
    40px/s is some twenty times a second, and is exactly the pulsing this
    undoes.

    So invert the curve the library is about to apply, and light comes out
    linear in coverage again. The colorize below scales the whole ramp by
    TITLE_FG, a constant factor, so it leaves the proportion intact.
    """
    lut = []
    for i in range(256):
        y = i / 255
        # Inverse of rgb-led-matrix's CIE1931 luminance table (framebuffer.cc):
        # y = x/902.3 below the toe, ((x + 16)/116)**3 above it, x in 0..100.
        x = y * 902.3 if y <= 8 / 902.3 else 116 * (y ** (1 / 3)) - 16
        lut.append(min(255, max(0, round(x / 100 * 255))))
    return lut


_COVERAGE_LUT = _coverage_lut()


def make_title_phases(text: str, pil_font, height: int,
                      color: tuple = TITLE_FG) -> list:
    """`text` pre-rendered at each horizontal sub-pixel phase.

    A title creeping across the panel can only ever be drawn on whole pixels
    by `graphics.DrawText`, so however fast the render loop runs it moves in
    1px lurches. These are the same string sampled at 0, 1/N, 2/N … of a pixel
    to the right: the strip is supersampled N× horizontally and box-filtered
    back down, so a glyph edge falling between two panel columns lights both
    in proportion and the motion reads as continuous. _COVERAGE_LUT is what
    keeps that partial column as bright as the whole one it came from.

    Built once per title and cached on the Session, like make_paused_poster —
    about a millisecond here against 0.02ms per frame for the crop.
    """
    sub = config.SCROLL_SUBPIXEL
    w   = int(pil_font.getlength(text))
    # One spare column on the right so the last phase still has a full
    # supersampled pixel to average over.
    base = Image.new("L", (w + 1, height))
    ImageDraw.Draw(base).text((0, 0), text, font=pil_font, fill=255)
    wide = base.resize(((w + 1) * sub, height), Image.NEAREST)
    phases = []
    for k in range(sub):
        window = wide.crop((k, 0, k + w * sub, height))
        mask   = window.resize((w, height), Image.BOX)
        # Coverage out of the filter, panel values into SetImage — without
        # this step the title pulses in brightness as it slides. See
        # _coverage_lut.
        mask   = mask.point(_COVERAGE_LUT)
        # Colorized here rather than per frame: SetImage wants RGB, and the
        # title colour never changes.
        phases.append(ImageOps.colorize(mask, (0, 0, 0), color))
    return phases


def render_loop(matrix: RGBMatrix, config_store, state: State, stop: threading.Event):
    from marquee.config import parse_hhmm

    _font_big, _font_sm, _font_sub, _font_clk = load_fonts()
    # None on any font it cannot compile; the scroll branch falls back to
    # whole-pixel DrawText in that case.
    _pil_title = load_pil_title_font()

    canvas = matrix.CreateFrameCanvas()
    white  = graphics.Color(*TITLE_FG)
    clk_c  = graphics.Color(*config.IDLE_DIM)
    sub_c  = graphics.Color(160, 160, 160)
    user_c = graphics.Color(120, 120, 200)

    # Float, so a sub-pixel speed accumulates instead of rounding to zero.
    scroll_offset     = 0.0
    scroll_dir        = 1
    scroll_pause_until = 0.0
    last_session_key  = None
    last_frame        = time.monotonic()
    last_brightness   = None
    was_active        = True   # track transitions for log clarity

    # Display settings are re-derived only when the config generation moves,
    # keeping the per-frame cost at one integer compare.
    cfg_gen = None
    cycle_seconds = brightness_normal = brightness_dim = None
    sched_start = sched_stop = dtime(0, 0)
    dim_start = dim_stop = dtime(0, 0)
    accent_rgb = AMBER
    remain_c = graphics.Color(*config.scale_rgb(accent_rgb, config.REMAIN_SCALE))
    idle_mode = "clock"
    clock_fmt = "%I:%M %p"
    # Origins of the two 64x64 halves. Everything below is drawn relative to
    # these rather than to hardcoded 0/64, so the swap is one assignment.
    poster_x, text_x = 0, RX
    scroll_px = config.SCROLL_SPEEDS["normal"] * config.SCROLL_FRAME_MS / 1000
    show_user = True
    title_y = sub_y = user_y = rem_y = 0
    needs_setup = False
    setup_addr = {"host": "", "ip": ""}
    addr_checked = 0.0

    while not stop.is_set():
        if cfg_gen != config_store.generation:
            cfg_gen = config_store.generation
            snapshot = config_store.get()
            disp = snapshot["display"]
            cycle_seconds     = disp["cycle_seconds"]
            brightness_normal = disp["brightness_normal"]
            brightness_dim    = disp["brightness_dim"]
            sched_start = parse_hhmm(disp["schedule_start"])
            sched_stop  = parse_hhmm(disp["schedule_stop"])
            dim_start   = parse_hhmm(disp["dim_start"])
            dim_stop    = parse_hhmm(disp["dim_stop"])
            idle_mode   = disp["idle_mode"]
            # px/sec → px/frame, converted here rather than per frame:
            # this block only re-runs when the config generation moves.
            scroll_px = (config.SCROLL_SPEEDS[disp["scroll_speed"]]
                         * config.SCROLL_FRAME_MS / 1000)
            # Dropping the user line means one fewer block to distribute, not
            # a gap where it used to be — compute_text_layout re-spreads what
            # is left over the same region.
            show_user = disp["show_user"]
            blocks = [_font_big, _font_sub, _font_sm, _font_sm]
            if show_user:
                title_y, sub_y, user_y, rem_y = compute_text_layout(blocks)
            else:
                title_y, sub_y, rem_y = compute_text_layout(blocks[:3])
                user_y = None
            log.info("Layout: title y=%d, subtitle y=%d, user y=%s, remaining y=%d",
                     title_y, sub_y, user_y if show_user else "hidden", rem_y)
            if disp["poster_side"] == "right":
                poster_x, text_x = RX, 0
            else:
                poster_x, text_x = 0, RX
            clock_fmt   = "%H:%M" if disp["clock_24h"] else "%I:%M %p"
            # Built here rather than per frame: graphics.Color allocates, and
            # this loop runs 20 times a second.
            accent_rgb  = config.hex_to_rgb(disp["accent"])
            remain_c    = graphics.Color(
                *config.scale_rgb(accent_rgb, config.REMAIN_SCALE))
            # Derived from the Plex URL rather than the `provisioned` flag, so
            # the panel starts working the moment a server is picked — and
            # says so again if that server is ever cleared.
            needs_setup = not snapshot["plex"]["url"]

        now = time.monotonic()
        dt = now - last_frame
        if dt < config.SCROLL_FRAME_MS / 1000:
            time.sleep((config.SCROLL_FRAME_MS / 1000) - dt)
        last_frame = time.monotonic()

        # ── Status pages (setup/link/reset) ────────────────────────────────
        # Drawn ahead of the schedule gate: someone actively setting up or
        # holding the reset button must see the panel respond regardless of
        # the display-off window. ~2fps is plenty for these.
        mode, payload = state.get_mode()
        if mode is DisplayMode.LINK_CODE and now > payload.get("expires_at", float("inf")):
            log.info("Link code expired — clearing the panel")
            state.clear_mode(DisplayMode.LINK_CODE)
            mode, payload = state.get_mode()
        if mode is DisplayMode.NORMAL and needs_setup:
            # Synthesized here rather than written into State: netmgr and the
            # reset button stay the only owners of the real mode.
            if now - addr_checked > 10:
                addr_checked = now
                setup_addr = {"host": f"{socket.gethostname()}.local",
                              "ip": local_ip()}
            mode, payload = DisplayMode.NEEDS_SETUP, setup_addr
        if mode is not DisplayMode.NORMAL:
            if target := (payload.get("brightness") or brightness_normal):
                if target != last_brightness:
                    matrix.brightness = target
                    last_brightness = target
            canvas.Clear()
            draw_mode_screen(canvas, (_font_big, _font_sm, _font_sub, _font_clk),
                             mode, payload, accent_rgb)
            canvas = matrix.SwapOnVSync(canvas)
            was_active = True
            stop.wait(0.5)
            continue

        # ── Schedule gate ──────────────────────────────────────────────────
        active = is_within_schedule(sched_start, sched_stop)
        if not active:
            if was_active:
                matrix.Clear()
                log.info("Schedule: outside window, display off (%s–%s)",
                         sched_start.strftime("%H:%M"),
                         sched_stop.strftime("%H:%M"))
            was_active = False
            # Sleep briefly while blanked — long enough to stop spinning at
            # 20fps, short enough that schedule edits apply promptly.
            stop.wait(5)
            continue

        if not was_active:
            log.info("Schedule: inside window, display on")
        was_active = True

        # ── Brightness ─────────────────────────────────────────────────────
        state.maybe_cycle(cycle_seconds)
        with state.lock:
            ha_dim = state.dim
        # Either source is enough. Home Assistant knows the TV is on; the dim
        # window is for the far more common case of no Home Assistant at all.
        should_dim = ha_dim or is_within_dim_window(dim_start, dim_stop)
        target_brightness = brightness_dim if should_dim else brightness_normal
        if target_brightness != last_brightness:
            matrix.brightness = target_brightness
            last_brightness = target_brightness
            log.info("Brightness → %d", target_brightness)

        # ── Draw ───────────────────────────────────────────────────────────
        canvas.Clear()
        current = state.current()

        if current is None:
            # An unreachable server and an idle one look identical from here,
            # but they are not: saying "Nothing playing" while Plex is down
            # is a lie that sends people looking at the wrong thing. This is
            # drawn inside the normal display rather than as a status page so
            # it still honours the schedule and the dim flag — a notice that
            # lights the panel at 3am would be worse than the lie.
            with state.lock:
                offline = state.plex_offline
                held = state.last_poster

            # An outage outranks the idle style, including "blank": the panel
            # going dark is exactly what someone would misread as "it broke",
            # and this is the screen that tells them which thing broke.
            if offline:
                t  = time.strftime(clock_fmt)
                x  = (128 - text_width(_font_clk, t)) // 2
                graphics.DrawText(canvas, _font_clk, x, 13, clk_c, t)
                msg = "Can't reach Plex"
                x2  = (128 - text_width(_font_sm, msg)) // 2
                graphics.DrawText(canvas, _font_sm, x2, 44,
                                  graphics.Color(*RED), msg)
                hint = "check the server"
                x3   = (128 - text_width(_font_sm, hint)) // 2
                graphics.DrawText(canvas, _font_sm, x3, 55,
                                  graphics.Color(70, 70, 70), hint)

            elif idle_mode == "blank":
                pass  # canvas is already cleared; swap it out dark

            elif idle_mode == "poster" and held is not None:
                # Drawn at full strength: the panel brightness already
                # carries the schedule, the dim window, and Home Assistant,
                # so a second dim here was just a knob nobody could reach.
                canvas.SetImage(held, poster_x, 0)
                t = time.strftime(clock_fmt)
                x = text_x + max(0, (64 - text_width(_font_clk, t)) // 2)
                graphics.DrawText(canvas, _font_clk, x, 36, clk_c, t)

            else:
                # "clock", and the fallback for "poster" before anything has
                # played — a device that just booted has no artwork to hold.
                t  = time.strftime(clock_fmt)
                x  = (128 - text_width(_font_clk, t)) // 2
                graphics.DrawText(canvas, _font_clk, x, 13, clk_c, t)
                msg = "Nothing playing"
                x2  = (128 - text_width(_font_sm, msg)) // 2
                graphics.DrawText(canvas, _font_sm, x2, 46,
                                  graphics.Color(60, 60, 60), msg)
        else:
            sub_max_chars = 64 // 5

            title = current.title or "—"
            tw    = text_width(_font_big, title)
            if tw <= 64:
                graphics.DrawText(canvas, _font_big,
                                  text_x + max(0, (64 - tw) // 2), title_y, white, title)
            else:
                if current.session_key != last_session_key:
                    scroll_offset      = 0.0
                    scroll_dir         = 1
                    scroll_pause_until = now + (config.SCROLL_PAUSE_MS / 1000)
                if _pil_title is not None and current.title_phases is None:
                    current.title_phases = make_title_phases(
                        title, _pil_title, _font_big.height)
                phases = current.title_phases
                # The strip's own width, so the bound and the crop can never
                # disagree about where the title ends.
                span = (phases[0].width if phases else tw) - 64
                if now >= scroll_pause_until:
                    scroll_offset += scroll_px * scroll_dir
                    if scroll_offset >= span:
                        scroll_offset      = float(span)
                        scroll_dir         = -1
                        scroll_pause_until = now + (config.SCROLL_PAUSE_MS / 1000)
                    elif scroll_offset <= 0:
                        scroll_offset      = 0.0
                        scroll_dir         = 1
                        scroll_pause_until = now + (config.SCROLL_PAUSE_MS / 1000)
                if phases:
                    # Whole pixels pick the window, the fraction picks the
                    # phase — together they address the title at 1/N of a
                    # pixel, which is the entire point of the exercise.
                    # divmod rather than two operations so the two can never
                    # be rounded inconsistently at a pixel boundary.
                    sx, k = divmod(int(scroll_offset * config.SCROLL_SUBPIXEL),
                                   config.SCROLL_SUBPIXEL)
                    # Clipped at the source, so unlike DrawText this cannot
                    # overrun into the poster half at all.
                    canvas.SetImage(
                        phases[k].crop((sx, 0, sx + 64, _font_big.height)),
                        text_x, title_y - _font_big.baseline)
                else:
                    # No PIL font: whole pixels, as before. Truncated only at
                    # the draw so the accumulator keeps its fraction. Floor,
                    # not round() — banker's ties make a crawl uneven.
                    graphics.DrawText(canvas, _font_big,
                                      text_x - int(scroll_offset),
                                      title_y, white, title)

            last_session_key = current.session_key

            # Drawn after the title on purpose: a scrolling title overruns
            # its own half, and the poster is what paints over the overrun.
            # That holds whichever side the poster is on.
            if current.poster is not None:
                poster = current.poster
                if current.state == "paused":
                    if current.poster_paused is None:
                        current.poster_paused = make_paused_poster(poster)
                    poster = current.poster_paused
                canvas.SetImage(poster, poster_x, 0)
            else:
                for py in range(64):
                    for px in range(64):
                        if (px + py) % 8 == 0:
                            canvas.SetPixel(poster_x + px, py, 30, 30, 30)

            sub_text = current.subtitle or ""
            sw = text_width(_font_sub, sub_text)
            if sw <= 64:
                graphics.DrawText(canvas, _font_sub,
                                  text_x + max(0, (64 - sw) // 2), sub_y, sub_c, sub_text)
            else:
                # Two small lines fill exactly the block the single large line
                # would have occupied, so nothing collides with the row below.
                block_top = sub_y - _font_sub.baseline
                line1_y   = block_top + _font_sm.baseline
                line1, line2 = wrap_two_lines(sub_text, sub_max_chars)
                graphics.DrawText(canvas, _font_sm, text_x + 1, line1_y, sub_c, line1)
                if line2:
                    graphics.DrawText(canvas, _font_sm, text_x + 1,
                                      line1_y + _font_sm.height, sub_c, line2)

            if show_user:
                user = (current.user or "")[:sub_max_chars]
                graphics.DrawText(canvas, _font_sm, text_x + 1, user_y, user_c, user)

            remaining = format_remaining(current.duration_ms,
                                         current.view_offset_ms)
            if remaining:
                graphics.DrawText(canvas, _font_sm, text_x + 1, rem_y,
                                  remain_c, remaining)

            bar_x0, bar_x1 = text_x + 1, text_x + 62
            bar_y0, bar_y1 = config.BAR_Y0, config.BAR_Y1
            for bx in range(bar_x0, bar_x1 + 1):
                for by in range(bar_y0, bar_y1 + 1):
                    canvas.SetPixel(bx, by, *config.PROGRESS_BG)
            fill = int((bar_x1 - bar_x0) * current.progress)
            for bx in range(bar_x0, bar_x0 + fill + 1):
                for by in range(bar_y0, bar_y1 + 1):
                    canvas.SetPixel(bx, by, *accent_rgb)

            idx, n = state.cycle_position()
            if n > 1:
                for i in range(n):
                    dx    = text_x + 63 - (n - 1 - i) * 3
                    color = (200, 200, 200) if i == idx else (60, 60, 60)
                    canvas.SetPixel(dx, config.DOTS_Y, *color)
                    canvas.SetPixel(dx, config.DOTS_Y + 1, *color)

        canvas = matrix.SwapOnVSync(canvas)
