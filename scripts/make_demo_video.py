"""Post-production for the demo video of one staged call. Runs locally: ffmpeg + Pillow, no network, no Gnani, no Gemini.

    python scripts/make_demo_video.py --call <id> --recording <mp4> --music build     # render into data/video/
    python scripts/make_demo_video.py --call <id> --recording <mp4> --plan            # print the offset, cuts and caption/toast times only

What it does, in order:
  1. cuts the screen recording to the call (no speed-ups; only silences where nobody speaks are shortened),
  2. crops away the browser's tab and address bars and keeps the page itself,
  3. puts it on a 1080x1350 (4:5) canvas with a small STAGED DEMO mark, an evidence counter, and English subtitles,
  4. adds a 3 s title card and a 3 s end card, and writes an H.264 + AAC mp4 with quiet synthesized background music.

Nothing about a call is placed by hand. From data/calls/<id>/ and the recording's own audio it works out:
  - the offset between the call log's clock and the recording's (the six Uncle line starts in the log are matched to the sound onsets in the audio),
  - the cuts (quiet stretches of 1.6 s or more that do not end at an Uncle line; his waiting time is never shortened),
  - the subtitle windows (Uncle: events.jsonl; caller: transcripts.jsonl arrival time, latency and audio length, snapped to the sound),
  - the green banners and the evidence dots (the first turn in turns.jsonl whose findings contain each item, at Uncle's line start in events.jsonl),
  - the full UPI card shown beside the page (value, badges and quote from that finding in findings.json).
Only the English wording is written by hand: ENGLISH_CALLER (by transcript text) and ENGLISH_UNCLE (by Uncle's line). A line with no English
stops the script and names the line, so nothing is shown that was not translated.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # for mirage.extract (its digit words), also under python -I
SRC = ROOT / "recording" / "Mirage_ Rajesh Uncle - Google Chrome 2026-11-04 12-49-47.mp4"
CALL_ID = "20261104_124958_c42d"
OUT = ROOT / "data" / "video"
WORK = OUT / "work"

OFFSET_S = 0.0           # recording time = call-log time + this; set by configure() from the sound
FPS = 30
W, H = 1080, 1350
STRIP_H, UI_X, UI_Y, UI_W, UI_H = 116, 16, 140, 1048, 966
CAP_Y = 1128                              # the caption zone runs from here to the bottom
CAP_H = H - CAP_Y
CROP = (538, 210, 1336, 1230)             # x, y, w, h of the page inside the 2448x1440 recording (no tab bar, no address bar, no staged-data banner, no scrollbar)
TITLE_S, END_S = 3.0, 3.0
FADE_S = 0.2

KEEP: list[tuple[float, float]] = []                 # call-log seconds kept; set by configure()
CAPTIONS: list[tuple] = []                            # (speaker, start, end, prefix, text, kind, continues); set by configure()
EVIDENCE: list[tuple] = []                            # (call-log second, label, value, short name); set by configure()
UPI_CARD: dict = {}                                   # the stored UPI finding as the dashboard card shows it; set by configure()
CALLER, UNCLE = "caller", "uncle"

# The only hand-written part: English for each line. Keys are the transcript text without its final "।", and Uncle's logged line.
ENGLISH_CALLER = {
    "नमस्ते, मैं CBI से बोल रहा हूँ": "Hello, I'm calling from the CBI.",
    "आपके नाम के parcel में drugs मिले हैं": "We found drugs in a parcel in your name.",
    "यह digital arrest का matter है। आपको अभी line पर रहना होगा": "This is a digital arrest matter. You must stay on the line right now.",
    "Verification के लिए refundable security deposit भेजना होगा": "For verification, you must send a refundable security deposit.",
    "UPI ID है verify officer one": "The UPI ID is verify officer one...",
    "Two three at OK axis": "...two three, at OK axis.",
}
# A sentence that ends in digit words: (words to show before the digits or None, label shown above the digits)
ENGLISH_CALLER_LEAD = {
    "case number है": (None, "Case number is"),
    "मेरा direct number लिख लीजिए। phone number है": ("Write down my direct number. Phone number is zero...", "Phone number"),
}
ENGLISH_UNCLE = {
    "<coughs> Haan ji boliye beta, main sun raha hoon.": "(coughs) Yes, go ahead, son. I'm listening.",
    "Police? CBI? Arre baap re, maine toh kuch galat nahin kiya.": "Police? CBI? Oh my God! I haven't done anything wrong.",
    "Achha, ye likha: 8 2 6. Aage ka phir se boliye beta?": "Okay, this is what I wrote: 8 2 6. Say the rest again, son?",
    "Haan, 0 0 0, 0 3... bas yahin tak likha. Baaki dobara bataiye.": "Yes: 0 0 0, 0 3... that's all I wrote down. Tell me the rest again.",
    "Main pensioner hoon beta, par kar dunga. Kis account mein bhejun?": "I'm a pensioner, son, but I will pay. Which account should I send it to?",
    "Mujhe sunai kam deta hai, account number thoda dheere bataiye na beta.": "I don't hear well. Say the account number a little slower, son.",
}
KIND_NAMES = {"case": ("CASE NUMBER", "CASE NUMBER"), "phone": ("PHONE NUMBER", "PHONE"), "vpa": ("UPI ID", "UPI ID"),
              "account": ("ACCOUNT NUMBER", "ACCOUNT"), "ifsc": ("IFSC", "IFSC")}

# ------------------------------------------------------------------ palette and type (fonts that ship with Windows)
INK = (12, 12, 10)
CREAM = (240, 233, 214)
MUTED = (150, 144, 126)
AMBER = (232, 181, 58)
BLUE = (141, 184, 255)
GREEN = (116, 214, 142)
PANEL = (22, 21, 17)
EDGE = (58, 54, 42)
FONTS = Path("C:/Windows/Fonts")


def font(name: str, size: int, axes: list[float] | None = None) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONTS / name), size)
    if axes:
        f.set_variation_by_axes(axes)
    return f


def bahn(size: int, weight: int = 400, width: int = 100):
    return font("bahnschrift.ttf", size, [weight, width])


def sitka(size: int, weight: int = 700, optical: int = 27):
    return font("SitkaVF-Italic.ttf", size, [optical, weight])


def mono(size: int, weight: int = 600):
    return font("CascadiaMono.ttf", size, [weight])


def tracked_width(text: str, f, tr: float) -> float:
    return sum(f.getlength(c) + tr for c in text) - tr if text else 0.0


def tracked(d: ImageDraw.ImageDraw, x: float, y: float, text: str, f, fill, tr: float = 0.0, anchor: str = "ls") -> float:
    for c in text:
        d.text((x, y), c, font=f, fill=fill, anchor=anchor)
        x += f.getlength(c) + tr
    return x


def ease_out(p: float) -> float:
    p = min(max(p, 0.0), 1.0)
    return 1 - (1 - p) ** 3


def ramp(t: float, a: float, b: float) -> float:
    return min(max((t - a) / (b - a), 0.0), 1.0) if b > a else float(t >= a)


# ------------------------------------------------------------------ time mapping: call-log seconds -> seconds inside the cut video
def cut_offsets() -> list[tuple[float, float, float]]:
    out, acc = [], 0.0
    for a, b in KEEP:
        out.append((a, b, acc))
        acc += b - a
    return out


def main_length() -> float:
    return sum(b - a for a, b in KEEP)


def to_main(t: float, *, end: bool = False) -> float:
    """Where call-log second t falls in the cut video. A time inside a removed stretch moves to its nearer kept edge (start of the next
    piece for a start time, end of the previous piece for an end time)."""
    segs = cut_offsets()
    for a, b, acc in segs:
        if a <= t <= b:
            return acc + (t - a)
    if t < segs[0][0]:
        return 0.0
    prev = None
    for a, b, acc in segs:
        if t < a:
            return (prev[2] + prev[1] - prev[0]) if end and prev else acc
        prev = (a, b, acc)
    return main_length()


def caption_windows() -> list[dict]:
    out = []
    for i, (who, a, b, prefix, text, kind, cont) in enumerate(CAPTIONS):
        s, e = to_main(a), to_main(b, end=True)
        assert e > s, (a, b)
        nxt_cont = i + 1 < len(CAPTIONS) and CAPTIONS[i + 1][6] and CAPTIONS[i + 1][0] == who
        out.append(dict(who=who, s=s, e=e, prefix=prefix, text=text, kind=kind, fade_in=not cont, fade_out=not nxt_cont))
    return out


def evidence_windows() -> list[dict]:
    return [dict(s=to_main(t), label=label, value=value, short=short) for t, label, value, short in EVIDENCE]


# ------------------------------------------------------------------ background, ui mask and ring
def make_background(shadow: bool = True) -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    glow = np.exp(-(((xx - W / 2) / 620) ** 2 + ((yy - 640) / 700) ** 2))
    img = np.zeros((H, W, 3), np.float32) + np.array(INK, np.float32)
    img += glow[..., None] * np.array([26, 20, 6], np.float32)
    img += np.random.default_rng(7).normal(0, 1.4, (H, W, 1)).astype(np.float32)
    bg = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    if not shadow:
        return bg
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((UI_X + 2, UI_Y + 14, UI_X + UI_W - 2, UI_Y + UI_H + 10), 26, fill=190)
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    bg.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), shadow)
    return bg


def make_ui_mask_and_ring() -> tuple[Image.Image, Image.Image]:
    s = 4
    big = Image.new("L", (UI_W * s, UI_H * s), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, UI_W * s - 1, UI_H * s - 1), 22 * s, fill=255)
    mask = big.resize((UI_W, UI_H), Image.LANCZOS)
    ring = Image.new("RGBA", (UI_W * s, UI_H * s), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle((0, 0, UI_W * s - 1, UI_H * s - 1), 22 * s, outline=EDGE + (255,), width=2 * s)
    return mask, ring.resize((UI_W, UI_H), Image.LANCZOS)


# ------------------------------------------------------------------ top strip: brand, STAGED mark, evidence counter, toast
class Strip:
    def __init__(self, bg: Image.Image):
        self.base = bg.crop((0, 0, W, STRIP_H)).convert("RGB")
        d = ImageDraw.Draw(self.base)
        d.text((30, 58), "Mirage", font=sitka(50, 700, 27), fill=CREAM, anchor="ls")
        x = 30 + sitka(50, 700, 27).getlength("Mirage") + 4
        d.ellipse((x, 50, x + 8, 58), fill=AMBER)
        tracked(d, x + 26, 56, "SCAM-BAITING VOICE AGENT", bahn(15, 600), MUTED, 2.2)
        label, f = "STAGED DEMO · SYNTHETIC DATA", bahn(16, 700)
        tw = tracked_width(label, f, 1.6)
        x0 = UI_X + UI_W - tw - 40
        d.rounded_rectangle((x0, 20, UI_X + UI_W, 58), 19, outline=AMBER, width=2, fill=(34, 27, 8))
        tracked(d, x0 + 20, 45, label, f, AMBER, 1.6)
        self.legend = "CALLER: ROLE-PLAYED   ·   UNCLE: THE AI"
        self.row_bg = self.base.crop((0, 66, W, 112))
        self.cache: dict = {}

    def frame(self, t: float, wins: list[dict]) -> Image.Image:
        n = sum(1 for w in wins if t >= w["s"] + 0.1)
        toast = next((w for w in wins if w["s"] <= t < w["s"] + 3.0), None)
        key = (n, None if toast is None else (wins.index(toast), round(ramp(t - toast["s"], 0, 0.28), 2), round(ramp(toast["s"] + 3.0 - t, 0, 0.25), 2)))
        if key in self.cache:
            return self.cache[key]
        img = self.base.copy()
        d = ImageDraw.Draw(img)
        if toast is None:
            for i in range(len(wins)):
                cx = UI_X + 18 + i * 26
                if i < n:
                    d.ellipse((cx - 8, 81, cx + 8, 97), fill=GREEN)
                else:
                    d.ellipse((cx - 8, 81, cx + 8, 97), outline=EDGE, width=2)
            tracked(d, UI_X + 26 * len(wins) + 22, 95, f"EVIDENCE  {n} / {len(wins)}", bahn(16, 700), CREAM if n else MUTED, 2)
            f = bahn(14, 600)
            tracked(d, UI_X + UI_W - tracked_width(self.legend, f, 1.4), 94, self.legend, f, MUTED, 1.4)
        else:
            p_in, p_out = ramp(t - toast["s"], 0, 0.28), ramp(toast["s"] + 3.0 - t, 0, 0.25)
            layer = Image.new("RGBA", (W, STRIP_H), (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            f1, f2 = bahn(16, 700), mono(24, 700)
            w1, w2 = tracked_width(toast["label"], f1, 2), f2.getlength(toast["value"])
            wp = int(46 + w1 + 22 + w2 + 22)
            x0 = UI_X - (1 - ease_out(p_in)) * (wp + 40)
            ld.rounded_rectangle((x0, 66, x0 + wp, 108), 21, fill=(16, 44, 26, 255), outline=GREEN + (255,), width=2)
            ld.ellipse((x0 + 18, 80, x0 + 34, 96), fill=GREEN + (255,))
            tracked(ld, x0 + 46, 94, toast["label"], f1, GREEN + (255,), 2)
            ld.text((x0 + 46 + w1 + 22, 95), toast["value"], font=f2, fill=CREAM + (255,), anchor="ls")
            if p_out < 1:
                layer.putalpha(layer.getchannel("A").point(lambda v: int(v * p_out)))
            img.paste(self.row_bg, (0, 66))
            img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        self.cache[key] = img
        return img


# ------------------------------------------------------------------ caption zone
def wrap(text: str, f, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split(" "):
        trial = (cur + " " + word).strip()
        if f.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + [cur]


def caption_card(c: dict) -> Image.Image:
    img = Image.new("RGBA", (W, CAP_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = AMBER if c["who"] == CALLER else BLUE
    d.rounded_rectangle((UI_X, 12, UI_X + UI_W, CAP_H - 22), 24, fill=PANEL + (238,), outline=EDGE + (255,), width=2)
    label = "CALLER" if c["who"] == CALLER else "UNCLE · THE AI"
    f = bahn(16, 700)
    wl = tracked_width(label, f, 2.2)
    d.rounded_rectangle((UI_X + 26, 28, UI_X + 26 + wl + 52, 62), 17, fill=tuple(int(v * 0.22) for v in col) + (255,), outline=col + (255,), width=2)
    d.ellipse((UI_X + 42, 40, UI_X + 52, 50), fill=col + (255,))
    tracked(d, UI_X + 62, 51, label, f, col + (255,), 2.2)
    fill = CREAM if c["who"] == CALLER else (222, 232, 250)
    if c["kind"] == "digits":
        d.text((UI_X + 30, 112), c["prefix"], font=bahn(34, 400), fill=MUTED + (255,), anchor="ls")
        fm = mono(64, 700)
        d.text((UI_X + 30, 178), c["text"], font=fm, fill=CREAM + (255,), anchor="ls")
    else:
        ft = bahn(37, 450)
        lines = wrap(c["text"], ft, UI_W - 60)
        y0 = 108 if len(lines) == 1 else 104
        for i, ln in enumerate(lines[:2]):
            d.text((UI_X + 30, y0 + i * 48), ln, font=ft, fill=fill + (255,), anchor="ls")
    return img


class Captions:
    def __init__(self, bg: Image.Image, wins: list[dict], total: float):
        self.base = bg.crop((0, CAP_Y, W, H)).convert("RGB")
        ImageDraw.Draw(self.base).rounded_rectangle((UI_X, 12, UI_X + UI_W, CAP_H - 22), 24, fill=(15, 14, 11), outline=(36, 33, 26), width=2)   # an empty panel between lines
        self.wins, self.total = wins, total
        self.cards = [caption_card(c) for c in wins]
        self.track = Image.new("RGB", (W, 4), (36, 33, 26))
        self.resting: Image.Image | None = None

    def frame(self, t: float) -> Image.Image:
        img = self.base.copy()
        cur = next((i for i, c in enumerate(self.wins) if c["s"] <= t < c["e"] + 0.12), None)
        if cur is not None:
            c = self.wins[cur]
            a_in = ramp(t - c["s"], 0, 0.14) if c["fade_in"] else 1.0
            a_out = ramp(c["e"] + 0.12 - t, 0, 0.12) if c["fade_out"] else 1.0
            a = min(a_in, a_out)
            card = self.cards[cur]
            dy = round((1 - ease_out(a_in)) * 12) if c["fade_in"] else 0
            if a < 1:
                card = card.copy()
                card.putalpha(card.getchannel("A").point(lambda v: int(v * a)))
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            layer.paste(card, (0, dy))
            img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        d = ImageDraw.Draw(img)
        d.rectangle((0, CAP_H - 4, W, CAP_H), fill=(36, 33, 26))
        d.rectangle((0, CAP_H - 4, int(W * min(t / self.total, 1.0)), CAP_H), fill=AMBER)
        return img


# ------------------------------------------------------------------ title and end cards
def shimmer(img: Image.Image, t: float, a: float = 1.0) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for k in range(11):
        y0 = 330 + k * 66
        pts = [(x, y0 + 7 * math.sin(x / 95 + t * 2.3 + k * 0.8) + 3 * math.sin(x / 31 - t * 3.1 + k)) for x in range(0, W + 20, 20)]
        d.line(pts, fill=AMBER + (int(26 * a),), width=2)
    img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"))


def centered(d, y: float, text: str, f, fill, tr: float = 0.0) -> None:
    tracked(d, (W - tracked_width(text, f, tr)) / 2, y, text, f, fill, tr)


def stamp_layer(scale: float, alpha: float, big: bool) -> Image.Image:
    f = bahn(38 if big else 22, 700)
    label = "STAGED DEMO · SYNTHETIC DATA"
    tr = 3.0 if big else 1.8
    wt = tracked_width(label, f, tr)
    pad = 36 if big else 22
    w, h = int(wt + pad * 2), (84 if big else 52)
    layer = Image.new("RGBA", (w + 20, h + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((10, 10, 10 + w, 10 + h), 12, outline=AMBER + (255,), width=4 if big else 3, fill=(34, 27, 8, 255))
    d.rounded_rectangle((16, 16, 4 + w, 4 + h), 8, outline=AMBER + (110,), width=1)
    tracked(d, 10 + pad, 10 + h / 2 + (13 if big else 8), label, f, AMBER + (255,), tr)
    if scale != 1.0:
        layer = layer.resize((int(layer.width * scale), int(layer.height * scale)), Image.LANCZOS)
    layer.putalpha(layer.getchannel("A").point(lambda v: int(v * alpha)))
    return layer


def title_frame(bg: Image.Image, t: float) -> Image.Image:
    img = bg.copy()
    shimmer(img, t, ramp(t, 0.0, 0.5))
    d = ImageDraw.Draw(img)
    a = ramp(t, 0.1, 0.45)
    if a > 0:
        col = tuple(int(INK[i] + (MUTED[i] - INK[i]) * a) for i in range(3))
        centered(d, 520 + (1 - ease_out(a)) * 10, "The caller thinks he is talking to a pensioner.", bahn(31, 350), col, 0.6)
    # wordmark, wiped in from the left with a soft edge, rising a few pixels
    fm = sitka(232, 700, 27)
    p = ease_out(ramp(t, 0.22, 0.85))
    if p > 0:
        layer = Image.new("RGBA", (W, 330), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        wmk = "Mirage"
        wx = (W - fm.getlength(wmk)) / 2 - 8
        ld.text((wx, 250 + (1 - p) * 26), wmk, font=fm, fill=CREAM + (255,), anchor="ls")
        ld.ellipse((wx + fm.getlength(wmk) + 8, 224, wx + fm.getlength(wmk) + 38, 254), fill=AMBER + (255,))
        edge = int(-120 + p * (W + 240))
        grad = np.clip((edge - np.arange(W)) / 120.0, 0, 1)
        mask = (np.tile(grad, (330, 1)) * 255).astype(np.uint8)
        a_ch = np.minimum(np.array(layer.getchannel("A")), mask)
        layer.putalpha(Image.fromarray(a_ch))
        full = Image.new("RGBA", img.size, (0, 0, 0, 0))
        full.paste(layer, (0, 560))
        img.paste(Image.alpha_composite(img.convert("RGBA"), full).convert("RGB"))
    d = ImageDraw.Draw(img)
    lw = 40 + 400 * ease_out(ramp(t, 0.6, 1.15))
    d.line(((W - lw) / 2, 884, (W + lw) / 2, 884), fill=AMBER, width=3)
    a = ramp(t, 0.75, 1.1)
    if a > 0:
        col = tuple(int(INK[i] + (CREAM[i] - INK[i]) * a) for i in range(3))
        centered(d, 962 + (1 - ease_out(a)) * 14, "a scam-baiting voice agent", bahn(56, 330), col, 1.0)
    s = ramp(t, 1.0, 1.3)
    if s > 0:
        layer = stamp_layer(1.0 + 0.16 * (1 - ease_out(s)), min(1.0, s * 1.6), True).rotate(-2.5, resample=Image.BICUBIC, expand=True)
        img.paste(layer, ((W - layer.width) // 2, 1110 - layer.height // 2), layer)
    return img


def end_frame(bg: Image.Image, t: float) -> Image.Image:
    img = bg.copy()
    shimmer(img, t + 4.0, 0.7)
    d = ImageDraw.Draw(img)

    def fade(a, b):
        return ramp(t, a, b)

    def mix(c, a):
        return tuple(int(INK[i] + (c[i] - INK[i]) * a) for i in range(3))

    a = fade(0.0, 0.3)
    d.text((W / 2, 250), "Mirage", font=sitka(84, 700, 27), fill=mix(CREAM, a), anchor="ms")
    rows = [(0.25, "LISTENING", "Gnani Prisma v3.0", 420), (0.7, "UNCLE'S VOICE", "Gnani Timbre v3.0", 640)]
    for t0, tag, name, y in rows:
        a = fade(t0, t0 + 0.35)
        dy = (1 - ease_out(a)) * 18
        centered(d, y + dy, tag, bahn(26, 700), mix(AMBER, a), 6)
        centered(d, y + 100 + dy, name, bahn(80, 380), mix(CREAM, a), 0.5)
        d.line((W / 2 - 40, y + 140 + dy, W / 2 + 40, y + 140 + dy), fill=mix(EDGE, a), width=2)
    a = fade(1.3, 1.7)
    names = " · ".join(e[3] for e in EVIDENCE)
    fn = bahn(20, 700)
    x = (W - (len(EVIDENCE) * 26 + 20 + tracked_width(names, fn, 2.4))) / 2
    for i in range(len(EVIDENCE)):
        d.ellipse((x + i * 26, 962 + (1 - ease_out(a)) * 12, x + i * 26 + 16, 978 + (1 - ease_out(a)) * 12), fill=mix(GREEN, a))
    tracked(d, x + len(EVIDENCE) * 26 + 20, 977 + (1 - ease_out(a)) * 12, names, fn, mix(MUTED, a), 2.4)
    s = fade(1.7, 2.1)
    if s > 0:
        layer = stamp_layer(1.0, s, False)
        img.paste(layer, ((W - layer.width) // 2, 1090), layer)
    out = ramp(t, END_S - 0.25, END_S)
    if out > 0:
        img = Image.blend(img, bg.copy().point(lambda v: int(v * 0.3)), out * 0.7)
    return img


# ------------------------------------------------------------------ the full UPI card (the recording cuts it off at the bottom of the window)
POP_X, POP_Y, POP_W, POP_H = UI_X + 12, UI_Y + 361, 677, 232      # sits exactly over the page's "Voice test" card, which never changes in a call
POP_UI_BOX = (12, 361, 12 + POP_W, 361 + POP_H)                    # the same box in the cropped page picture
POP_FROM_S = 0.35        # seconds after the evidence toast starts


def wrap_px(text: str, f, width: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split(" "):
        trial = (cur + " " + word).strip()
        if f.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + [cur]


def upi_card_image() -> Image.Image:
    """The dashboard's card for the stored UPI finding, with the part the recording cuts off at the bottom of the window: the badges that
    wrap onto a second row, any guess list, and the quote. Everything comes from UPI_CARD, which comes from findings.json; the card is
    centred in the box of the page's "Voice test" card, which it covers."""
    k = 3
    img = Image.new("RGBA", (POP_W * k, POP_H * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, POP_W * k - 1, POP_H * k - 1), 18 * k, fill=(31, 31, 27, 255), outline=AMBER + (210,), width=2 * k)
    fv, fc, fn, fq = font("CascadiaMono.ttf", 32 * k, [700]), font("segoeui.ttf", 17 * k), font("segoeui.ttf", 18 * k), font("segoeui.ttf", 16 * k)
    rows: list[tuple] = [("title", 20), ("value", 40)]
    chip_rows: list[list[tuple[str, bool, float]]] = [[]]
    x = 0.0
    for text, warn in UPI_CARD["chips"]:
        w = fc.getlength(text) / k + 30
        if x + w > POP_W - 48 and chip_rows[-1]:
            chip_rows.append([])
            x = 0.0
        chip_rows[-1].append((text, warn, w))
        x += w + 10
    rows += [("chips", 34)] * len(chip_rows)
    note_lines = wrap_px(UPI_CARD["note"], fn, (POP_W - 48) * k) if UPI_CARD["note"] else []
    rows += [("note", 25)] * len(note_lines)
    rows += [("quote", 24)]
    total = sum(h for _, h in rows) + 6 * (len(rows) - 1)
    y = (POP_H - total) / 2
    chip_i = note_i = 0
    for kind, h in rows:
        if kind == "title":
            tracked(d, 24 * k, (y + 15) * k, "UPI ID", font("segoeuib.ttf", 14 * k), (160, 158, 148, 255), 1.4 * k)
        elif kind == "value":
            d.text((24 * k, (y + 33) * k), UPI_CARD["value"], font=fv, fill=(240, 240, 235, 255), anchor="ls")
        elif kind == "chips":
            cx = 24.0
            for text, warn, w in chip_rows[chip_i]:
                col = (206, 118, 118, 255) if warn else (92, 90, 82, 255)
                d.rounded_rectangle((cx * k, y * k, (cx + w) * k, (y + 29) * k), 14.5 * k, outline=col, width=int(1.5 * k))
                d.text(((cx + 15) * k, (y + 21) * k), text, font=fc, fill=(232, 150, 150, 255) if warn else (226, 226, 220, 255), anchor="ls")
                cx += w + 10
            chip_i += 1
        elif kind == "note":
            d.text((24 * k, (y + 19) * k), note_lines[note_i], font=fn, fill=(214, 213, 207, 255), anchor="ls")
            note_i += 1
        else:
            d.text((24 * k, (y + 18) * k), UPI_CARD["quote"], font=fq, fill=(160, 158, 148, 255), anchor="ls")
        y += h + 6
    return img.resize((POP_W, POP_H), Image.LANCZOS)


class Pop:
    """The band of the stream that holds the full UPI card, faded over the page's own picture of that spot."""

    def __init__(self, back: Image.Image, start: float):
        self.back = back.convert("RGBA")
        self.card = upi_card_image()
        self.start = start

    def frame(self, t: float) -> Image.Image:
        a = ramp(t - self.start, 0, 0.3)
        out = Image.new("RGB", (W, POP_H), (0, 0, 0))
        if a <= 0:
            out.paste(self.back.convert("RGB"), (0, 0))
            return out
        layer = Image.new("RGBA", self.back.size, (0, 0, 0, 0))
        card = self.card.copy()
        card.putalpha(card.getchannel("A").point(lambda v: int(v * ease_out(a))))
        layer.paste(card, (0, round((1 - ease_out(a)) * 14)))
        out.paste(Image.alpha_composite(self.back, layer).convert("RGB"), (0, 0))
        return out


# ------------------------------------------------------------------ quiet background music, made here (no download, no licence)
MUSIC_RMS_DB = -27.0     # level of the music alone; the voice is normalised to -16 LUFS first, the music is then ducked a little under it


MUSIC_STYLE = "calm"      # "calm" (slow pad) or "build" (pulsing, rising); chosen with --music
OUT_NAME = "mirage_demo_x.mp4"


def make_music_build(total_s: float, ev_times: list[float], sr: int = 48000) -> np.ndarray:
    """A pulse that builds: a riser through the title card and an impact as the call starts, a muted 16th-note bass, a soft kick and ticking
    hats, a sparse bright arpeggio that gets busier as the call goes on, a short rising run at each piece of evidence, and a held chord on
    the end card. A minor, 112 bpm. Stereo float32; the caller ducks it under speech."""
    from scipy.signal import butter, sosfilt

    n = int(total_s * sr)
    rng = np.random.default_rng(5)
    beat = 60 / 112
    s16 = beat / 4
    out = np.zeros((n, 2), np.float32)
    hz = lambda m: 440.0 * 2 ** ((m - 69) / 12)
    call_start, end_start = TITLE_S, total_s - END_S

    def put(sig, t0, pan=0.5, gain=1.0):
        i = int(t0 * sr)
        if i < 0 or i >= n:
            return
        m = min(len(sig), n - i)
        out[i:i + m, 0] += sig[:m] * (1 - pan) * 2 * gain * 0.5
        out[i:i + m, 1] += sig[:m] * pan * 2 * gain * 0.5

    def lowpass(x, fc):
        return sosfilt(butter(2, min(fc, sr / 2 - 100), "low", fs=sr, output="sos"), x)

    def highpass(x, fc):
        return sosfilt(butter(2, fc, "high", fs=sr, output="sos"), x)

    def energy(t):             # 0..1: how busy the music is at call-time t
        if t < call_start:
            return 0.5
        k = (t - call_start) / max(end_start - call_start, 1)
        e = 0.3 + 0.45 * k
        if t > ev_times[-1] - 0.5 if ev_times else False:
            e = 1.0
        return e

    # title: a riser into an impact exactly as the call starts
    tr = np.arange(int(call_start * sr)) / sr
    ramp_ = (tr / call_start) ** 2.2
    noise = rng.normal(0, 1, len(tr))
    riser = np.zeros(len(tr))
    chunk = int(0.1 * sr)
    for a in range(0, len(tr), chunk):
        fc = 400 + 7000 * (a / len(tr)) ** 1.5
        riser[a:a + chunk] = highpass(noise[a:a + chunk], fc)
    chirp = np.sin(2 * np.pi * np.cumsum(180 + 900 * (tr / call_start) ** 2) / sr)
    put((riser * 0.22 + chirp * 0.25) * ramp_ * 0.9, 0.0, 0.5)
    ti = np.arange(int(1.6 * sr)) / sr
    boom = np.sin(2 * np.pi * np.cumsum(70 * np.exp(-ti * 2.5) + 34) / sr) * np.exp(-ti * 2.2)
    crash = highpass(rng.normal(0, 1, len(ti)), 2500) * np.exp(-ti * 7) * 0.25
    put(boom * 0.9 + crash, call_start, 0.5)

    chords = [[57, 60, 64, 69], [57, 60, 65, 69], [60, 64, 67, 72], [59, 62, 67, 71]]     # Am, F(add9 feel), C, G
    roots = [45, 41, 48, 43]
    bass_pattern = [1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1]
    arp_pattern = [0, 2, 1, 3, 2, 1, 3, 2, 0, 3, 1, 2, 3, 2, 1, 0]
    step = 0
    t = call_start
    while t < end_start - 0.05:
        bar = int((t - call_start) / (4 * beat))
        pos = int(round((t - call_start) / s16)) % 16
        chord, root = chords[bar % 4], roots[bar % 4]
        e = energy(t)
        if bass_pattern[pos]:
            tn = np.arange(int(0.16 * sr)) / sr
            f = hz(root)
            saw = 2 * ((f * tn) % 1.0) - 1
            note = lowpass(saw, 260 + 700 * e) * np.exp(-tn * 14) * (1.0 if pos % 4 == 0 else 0.7)
            put(note * 0.5, t, 0.5)
        if pos % 4 == 0 and t > call_start + 6:
            tk = np.arange(int(0.22 * sr)) / sr
            kick = np.sin(2 * np.pi * np.cumsum(46 + 110 * np.exp(-tk * 30)) / sr) * np.exp(-tk * 16)
            put(kick * 0.55, t, 0.5)
        if pos % 2 == 0:
            th = np.arange(int(0.05 * sr)) / sr
            hat = highpass(rng.normal(0, 1, len(th)), 7000) * np.exp(-th * 90)
            put(hat * (0.16 + 0.12 * e) * (1.0 if pos % 4 == 2 else 0.6), t, 0.62 if pos % 4 == 2 else 0.4)
        if rng.random() < 0.2 + 0.6 * e:
            idx = arp_pattern[pos]
            f = hz(chord[idx] + (12 if (pos // 4) % 2 else 0))
            tp = np.arange(int(0.4 * sr)) / sr
            pl = (np.sin(2 * np.pi * f * tp) + 0.4 * np.sin(2 * np.pi * 2 * f * tp) * np.exp(-tp * 12) + 0.15 * np.sin(2 * np.pi * 3 * f * tp)) \
                * np.exp(-tp * 9) * (1 - np.exp(-tp / 0.003))
            put(pl * (0.14 + 0.1 * e), t, 0.3 + 0.4 * (step % 2))
        t += s16
        step += 1

    # a short rising run at each piece of evidence
    for te in ev_times:
        for k, m in enumerate([69, 72, 76, 81]):
            tp = np.arange(int(0.5 * sr)) / sr
            f = hz(m)
            pl = (np.sin(2 * np.pi * f * tp) + 0.3 * np.sin(2 * np.pi * 2 * f * tp) * np.exp(-tp * 10)) * np.exp(-tp * 7) * (1 - np.exp(-tp / 0.003))
            put(pl * 0.2, te - 0.18 + k * 0.06, 0.3 + 0.13 * k)

    # end card: the beat stops and one chord is held
    te = np.arange(int((total_s - end_start) * sr)) / sr
    env = np.minimum(te / 0.25, 1.0) * np.minimum((total_s - end_start - te) / 2.2, 1.0).clip(0, 1)
    for m in [45, 57, 60, 64, 67]:
        f = hz(m)
        w = (np.sin(2 * np.pi * f * te) + 0.3 * np.sin(2 * np.pi * 2 * f * te)) * env * 0.12
        put(w, end_start, 0.5)
    hit = np.sin(2 * np.pi * np.cumsum(60 * np.exp(-ti * 3) + 40) / sr) * np.exp(-ti * 3)
    put(hit * 0.6, end_start, 0.5)

    # a little ping-pong echo on everything, then the level
    echo = out.copy()
    for tap, g in enumerate([0.3, 0.16, 0.08], start=1):
        d = int(s16 * 3 * sr * tap)
        echo[d:, 0] += out[:n - d, 1] * g
        echo[d:, 1] += out[:n - d, 0] * g
    m = echo
    m /= np.sqrt((m ** 2).mean()) + 1e-9
    m *= 10 ** ((MUSIC_RMS_DB - 5.0) / 20)      # the build style sits 5 dB lower than the calm one
    tt = np.arange(n) / sr
    m *= np.minimum((total_s - tt) / 1.5, 1.0)[:, None].clip(0, 1)
    return m.astype(np.float32)


def make_music(total_s: float, sr: int = 48000) -> np.ndarray:
    """A slow, warm loop in D minor: soft pad, a low root note, and a sparse high pluck with a ping-pong echo. Stereo float32."""
    n = int(total_s * sr)
    rng = np.random.default_rng(11)
    beat = 60 / 68
    chord_len = 8 * beat
    chords = [[50, 53, 57, 60, 64], [46, 53, 57, 62, 65], [53, 57, 60, 64, 67], [48, 55, 62, 64, 67]]     # Dm9, Bbmaj7, Fmaj7, Cadd9
    roots = [38, 34, 41, 36]
    hz = lambda m: 440.0 * 2 ** ((m - 69) / 12)
    pad, arp = np.zeros((n, 2), np.float32), np.zeros((n, 2), np.float32)

    def add(buf, start, sig, pan=0.5):
        i = int(start * sr)
        if i >= n:
            return
        m = min(len(sig), n - i)
        buf[i:i + m, 0] += sig[:m] * (1 - pan)
        buf[i:i + m, 1] += sig[:m] * pan

    k = 0
    while k * chord_len < total_s:
        i = k % len(chords)
        start, dur = k * chord_len, chord_len + 2.6
        t = np.arange(int(dur * sr)) / sr
        env = np.minimum(t / 1.8, 1.0) ** 2 * np.minimum((dur - t) / 2.6, 1.0) ** 2
        for m in chords[i]:
            f = hz(m)
            for ch, det in ((0, 0.9985), (1, 1.0015)):
                ph = rng.uniform(0, 6.28)
                w = np.sin(2 * np.pi * f * det * t + ph) + 0.33 * np.sin(2 * np.pi * 2 * f * det * t + ph * 1.7) + 0.1 * np.sin(2 * np.pi * 3 * f * det * t)
                w *= env * (1 + 0.14 * np.sin(2 * np.pi * 0.11 * t + ph)) * 0.05
                j = int(start * sr)
                mm = min(len(w), n - j)
                if mm > 0:
                    pad[j:j + mm, ch] += w[:mm]
        f = hz(roots[i])
        b = (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t)) * env * 0.16
        add(pad, start, b.astype(np.float32), 0.5)
        up = [m + 12 for m in chords[i][1:]]
        pattern = [0, 1, 2, 1, 3, 2, 1, 0, 2, 3, 1, 2, 0, 1, 3, 2]
        for step, idx in enumerate(pattern):
            if rng.random() < 0.28:
                continue
            tp = np.arange(int(1.5 * sr)) / sr
            fp = hz(up[idx])
            pl = (np.sin(2 * np.pi * fp * tp) + 0.25 * np.sin(2 * np.pi * 2 * fp * tp) * np.exp(-tp * 8)) * np.exp(-tp * 3.4) * (1 - np.exp(-tp / 0.004))
            add(arp, start + step * beat / 2, (pl * rng.uniform(0.6, 1.0) * 0.07).astype(np.float32), 0.35 + 0.3 * (step % 2))
        k += 1
    echo = arp.copy()
    for tap, g in enumerate([0.42, 0.26, 0.16, 0.1], start=1):
        d = int(0.44 * sr * tap)
        if d < n:
            echo[d:, 0] += arp[:n - d, 1] * g
            echo[d:, 1] += arp[:n - d, 0] * g
    m = pad + echo
    m /= np.sqrt((m ** 2).mean()) + 1e-9
    m *= 10 ** (MUSIC_RMS_DB / 20)
    tt = np.arange(n) / sr
    m *= np.minimum(tt / 1.0, 1.0)[:, None] * np.minimum((total_s - tt) / 2.5, 1.0)[:, None]
    return m.astype(np.float32)


# ------------------------------------------------------------------ what a call's logs and the recording's sound say
def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf8").splitlines() if l.strip()]


def mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def sound_levels() -> np.ndarray:
    """Loudness of the recording's audio every 10 ms, in dB."""
    WORK.mkdir(parents=True, exist_ok=True)
    raw = WORK / "rec_audio.raw"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(SRC), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", str(raw)])
    x = np.fromfile(raw, dtype="<i2").astype(float) / 32768
    n = len(x) // 160
    return 20 * np.log10(np.sqrt((x[:n * 160].reshape(n, 160) ** 2).mean(1)) + 1e-9)


def estimate_offset(voiced: np.ndarray, uncle_starts: list[float]) -> tuple[float, list[float]]:
    """recording time minus call-log time. A sound onset (voiced after 0.4 s of quiet) follows each Uncle line start by the same delay; the
    delay that most Uncle starts agree on (within 0.12 s) is the offset, and the median of those delays is returned."""
    n = len(voiced)
    onsets = [i / 100 for i in range(40, n) if voiced[i] and not voiced[i - 40:i].any()]
    best: list[float] = []
    for o in onsets:
        for s0 in uncle_starts:
            d0 = o - s0
            found = []
            for s1 in uncle_starts:
                near = [q - s1 for q in onsets if abs(q - s1 - d0) <= 0.12]
                if near:
                    found.append(min(near, key=lambda z: abs(z - d0)))
            if len(found) > len(best):
                best = found
    if len(best) < max(3, len(uncle_starts) - 1):
        sys.exit(f"could not match the Uncle line starts to the sound ({len(best)} of {len(uncle_starts)}); check the recording and call id")
    return float(np.median(best)), best


def snap_onset(voiced: np.ndarray, rec_t: float, before: float, after: float, quiet: int = 12, earliest: bool = False) -> float | None:
    """A sound onset (`quiet` hundredths of a second of quiet, then sound) within the window around rec_t, in recording seconds: the nearest to
    rec_t, or the earliest in the window when `earliest`."""
    lo, hi = max(int((rec_t - before) * 100), quiet), min(int((rec_t + after) * 100), len(voiced) - 1)
    onsets = [i / 100 for i in range(lo, hi) if voiced[i] and not voiced[i - quiet:i].any()]
    if not onsets:
        return None
    return onsets[0] if earliest else min(onsets, key=lambda o: abs(o - rec_t))


def split_digits(text: str) -> tuple[str, str]:
    """(words before the trailing digit words, the digits): "case number है आठ" -> ("case number है", "8"). Uses the extractor's own digit words."""
    from mirage.extract import _tokenize
    toks = [t for t in _tokenize(text, 0) if t.kind not in ("sep", "break")]
    k = len(toks)
    while k > 0 and toks[k - 1].kind in ("digit", "numeral", "mult"):
        k -= 1
    if any(t.kind == "mult" for t in toks[k:]):
        sys.exit(f"a repeat word ('double', 'triple') in {text!r}: the digit captions do not handle it yet")
    head = text[:toks[k - 1].end].strip(" ।,.") if k else ""
    return head, "".join(t.val for t in toks[k:])


def configure(call_id: str, recording: Path) -> dict:
    """Set OFFSET_S, KEEP, CAPTIONS, EVIDENCE and UPI_CARD for one call from its logs and the recording's sound. Returns notes for printing."""
    global SRC, CALL_ID, OFFSET_S, KEEP, CAPTIONS, EVIDENCE, UPI_CARD
    SRC, CALL_ID = recording, call_id
    cdir = ROOT / "data" / "calls" / call_id
    events, turns = read_jsonl(cdir / "events.jsonl"), read_jsonl(cdir / "turns.jsonl")
    transcripts, findings = read_jsonl(cdir / "transcripts.jsonl"), json.loads((cdir / "findings.json").read_text(encoding="utf8"))
    uncle = []                                                   # (turn, start, end, text)
    pending = None
    for e in events:
        if e["event"] == "uncle_speaking_start":
            pending = e
        elif e["event"] == "uncle_speaking_end" and pending:
            uncle.append((pending["turn"], pending["t"], e["t"], pending["text"]))
            pending = None
    call_end = next(e["t"] for e in events if e["event"] == "end")
    db = sound_levels()
    voiced = db > db.max() - 38
    OFFSET_S, delays = estimate_offset(voiced, [u[1] for u in uncle])
    notes = {"offset": OFFSET_S, "delays": delays}

    # cuts: quiet stretches of 1.6 s or more that do not end at an Uncle line are cut down to 0.6 s; the start is 0.96 s before the first caller sound
    first = snap_onset(voiced, OFFSET_S + transcripts[0]["t"] - transcripts[0]["audio_s"] + 0.3, 1.0, 1.0)
    start = (first if first is not None else OFFSET_S + 2.0) - OFFSET_S - 0.96
    end = uncle[-1][2] + 1.55
    quiet, runs, i = ~voiced, [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            runs.append((i / 100 - OFFSET_S, j / 100 - OFFSET_S))
            i = j
        else:
            i += 1
    cuts = []
    for a, b in runs:
        if a < start or b > end or b - a < 1.6 or any(abs(b - u[1]) < 0.35 for u in uncle):
            continue
        cuts.append((a + 0.3, b - 0.3))
    pieces, t0 = [], start
    for a, b in cuts:
        pieces.append((t0, a))
        t0 = b
    pieces.append((t0, end))
    KEEP = [(round((a + OFFSET_S) * FPS) / FPS - OFFSET_S, round((b + OFFSET_S) * FPS) / FPS - OFFSET_S) for a, b in pieces]
    notes.update(start=start, end=end, cuts=cuts)

    # caption windows
    caps: list[tuple] = []
    for turn, s0, e0, text in uncle:
        if text not in ENGLISH_UNCLE:
            sys.exit(f"no English for Uncle's line: {text!r}")
        snapped = snap_onset(voiced, s0 + OFFSET_S, 0.15, 0.15)
        caps.append((UNCLE, (snapped - OFFSET_S) if snapped else s0, e0 - 0.5, "", ENGLISH_UNCLE[text], "text", False))
    pieces_c = []
    for idx, r in enumerate(transcripts):
        lat = (r.get("server_latency_ms") or 0) / 1000
        s0, e0 = r["t"] - lat - r["audio_s"] + 0.3, r["t"] - lat - 0.4
        snapped = snap_onset(voiced, s0 + OFFSET_S, 0.7, 0.2, quiet=25, earliest=True)   # the transcript's audio length includes padding: the sound starts a little earlier
        if snapped is not None:
            s0 = snapped - OFFSET_S
        head, digits = split_digits(r["text"])
        if head and not any(head.endswith(k2) for k2 in ENGLISH_CALLER_LEAD):
            digits = ""                      # a sentence that merely ends in a number word ("... verify officer one"): shown as words
        pieces_c.append(dict(i=idx, s=s0, e=max(e0, s0 + 0.9), text=r["text"], head=head, digits=digits))
    run_digits: list[dict] = []
    text_run: list[dict] = []

    def flush_text():
        if text_run:
            words = " ".join(ENGLISH_CALLER[p["text"].rstrip("।").strip()] if p["text"].rstrip("।").strip() in ENGLISH_CALLER else
                             sys.exit(f"no English for the caller's line: {p['text']!r}") for p in text_run)
            caps.append((CALLER, text_run[0]["s"], text_run[-1]["e"], "", words, "text", False))
            text_run.clear()

    def flush_digits():
        if not run_digits:
            return
        label, shown, emitted = run_digits[0]["label"], "", 0
        for k, p in enumerate(run_digits):
            shown = (shown + " " + " ".join(p["digits"])).strip()
            nxt = run_digits[k + 1]["s"] if k + 1 < len(run_digits) else p["e"] + 0.5
            if nxt - p["s"] < 0.4 and k + 1 < len(run_digits):
                continue                     # a state that would flash by (the "zero" at the end of the lead-in sentence): the next one includes it
            caps.append((CALLER, p["s"], nxt, label, shown, "digits", emitted > 0))
            emitted += 1
        run_digits.clear()

    for p in pieces_c:
        if not p["digits"]:
            flush_digits()
            if text_run and (p["s"] - text_run[-1]["e"] >= 1.0 or any(text_run[-1]["e"] < u[1] < p["s"] for u in uncle)):
                flush_text()
            text_run.append(p)
            continue
        flush_text()
        if p["head"]:
            flush_digits()
            lead = next((v for k2, v in ENGLISH_CALLER_LEAD.items() if p["head"].endswith(k2) or p["head"] == k2), None)
            if lead is None:
                sys.exit(f"no English for the caller's lead-in: {p['head']!r}")
            start_digits = p["s"]
            if lead[0]:
                caps.append((CALLER, p["s"], p["e"], "", lead[0], "text", False))
                start_digits = p["e"] + 0.05
            run_digits.append(dict(p, label=lead[1], s=start_digits))
        else:
            run_digits.append(dict(p, label=run_digits[0]["label"] if run_digits else "Digits"))
    flush_text()
    flush_digits()
    CAPTIONS = sorted(caps, key=lambda c: c[1])

    # evidence: the first turn whose findings hold each item, at the start of Uncle's line for that turn
    starts = {u[0]: u[1] for u in uncle}
    seen, EVIDENCE = set(), []
    for tr in turns:
        for f in tr["findings"]:
            key = (f["kind"], f["value"])
            if key in seen:
                continue
            seen.add(key)
            name, short = KIND_NAMES.get(f["kind"], (f["kind"].upper(), f["kind"].upper()))
            label = f"{name} HEARD, TO CONFIRM" if f["needs_confirmation"] else f"{name} CAPTURED"
            EVIDENCE.append((starts.get(tr["turn"], tr["reply_started_s"]), label, f["value"], short))

    vpa = next(f for f in findings.get("vpa", []))
    chips = [(f"confidence: {vpa['confidence']}", False), ("valid format" if vpa["valid"] else f"not valid: {vpa['reason']}", False)]
    if vpa["needs_confirmation"]:
        chips.append(("needs confirmation", True))
    maybe = list(vpa.get("candidates") or []) or ([vpa["suggested"]] if vpa.get("suggested") else [])
    UPI_CARD = dict(value=vpa["value"], chips=chips,
                    note=("heard as above; a guess, not the value: maybe " + " or ".join(maybe)) if maybe else "",
                    quote=f"\u201c{vpa['quote']}\u201d at {mmss(vpa['t'])}")
    return notes


# ------------------------------------------------------------------ ffmpeg plumbing
def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf8", errors="replace")
    if r.returncode:
        sys.exit(f"command failed: {' '.join(cmd[:6])} ...\n{r.stderr[-2500:]}")


def pipe_frames(frames, size: tuple[int, int], args: list[str], out: Path) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}",
           "-r", str(FPS), "-i", "-"] + args + [str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for fr in frames:
            p.stdin.write(fr.tobytes())
    finally:
        p.stdin.close()
    err = p.stderr.read().decode("utf8", "replace")
    if p.wait():
        sys.exit("ffmpeg failed:\n" + err[-2500:])


def cut_ui_and_audio() -> None:
    """Step 1 and 2: the kept pieces of the recording, joined, page-only crop, 30 fps; and the matching audio as wav.

    The recording is variable frame rate (a frame is written only when the screen changes), so cutting it directly would start a piece late
    and push the picture ahead of the sound. It is first turned into a constant 30 fps picture on the recording's own clock, then cut by
    whole frames; the sound is cut by samples at the same instants, so each piece has exactly the same length in both."""
    x, y, w, h = CROP
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(SRC), "-an",
         "-vf", f"crop={w}:{h}:{x}:{y},scale={UI_W}:{UI_H}:flags=lanczos,fps={FPS}:start_time=0:round=near,format=yuv420p",
         "-c:v", "libx264", "-crf", "8", "-preset", "fast", "-r", str(FPS), str(WORK / "cfr.mkv")])
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(SRC), "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(WORK / "src.wav")])
    parts, labels = [], ""
    for i, (a, b) in enumerate(KEEP):
        f0, f1 = round((a + OFFSET_S) * FPS), round((b + OFFSET_S) * FPS)
        s0, s1 = f0 * 48000 // FPS, f1 * 48000 // FPS
        parts.append(f"[0:v]trim=start_frame={f0}:end_frame={f1},setpts=PTS-STARTPTS[v{i}];"
                     f"[1:a]atrim=start_sample={s0}:end_sample={s1},asetpts=PTS-STARTPTS,afade=t=in:d=0.02,"
                     f"afade=t=out:st={(f1 - f0) / FPS - 0.02}:d=0.02[a{i}];")
        labels += f"[v{i}][a{i}]"
    graph = "".join(parts) + f"{labels}concat=n={len(KEEP)}:v=1:a=1[v][a]"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(WORK / "cfr.mkv"), "-i", str(WORK / "src.wav"), "-filter_complex", graph,
         "-map", "[v]", "-an", "-c:v", "libx264", "-crf", "9", "-preset", "fast", "-r", str(FPS), str(WORK / "ui.mkv"),
         "-map", "[a]", "-c:a", "pcm_s16le", str(WORK / "main.wav")])


def chime(sr: int) -> np.ndarray:
    t = np.arange(int(0.7 * sr)) / sr
    y = 0.6 * np.sin(2 * np.pi * 1318.5 * t) + 0.4 * np.sin(2 * np.pi * 1975.5 * t) + 0.15 * np.sin(2 * np.pi * 2637 * t)
    env = np.exp(-t * 6.5) * np.minimum(t / 0.004, 1.0)
    return (y * env * 0.045).astype(np.float32)


def write_wav(path: Path, data: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(data.shape[1])
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())


def build_audio(ev: list[dict]) -> Path:
    with wave.open(str(WORK / "main.wav")) as wf:
        sr, ch = wf.getframerate(), wf.getnchannels()
        main = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32).reshape(-1, ch) / 32768
    c = chime(sr)
    for e in ev:
        i = int(e["s"] * sr)
        n = min(len(c), len(main) - i)
        main[i:i + n] += c[:n, None]
    pre, post = np.zeros((int(TITLE_S * sr), ch), np.float32), np.zeros((int(END_S * sr), ch), np.float32)
    full = np.clip(np.concatenate([pre, main, post]), -1, 1)
    path = WORK / "full.wav"
    write_wav(path, full, sr)
    if MUSIC_STYLE == "build":
        music = make_music_build(len(full) / sr, [TITLE_S + e["s"] for e in ev], sr)
    else:
        music = make_music(len(full) / sr, sr)
    write_wav(WORK / "music.wav", music, sr)
    return path


def render(plan_only: bool = False) -> None:
    notes = configure(CALL_ID, SRC)
    print(f"offset {OFFSET_S:.2f} s (recording = call log + offset; Uncle line delays {[round(d, 2) for d in notes['delays']]})")
    wins, ev, total = caption_windows(), evidence_windows(), main_length()
    print(f"kept {total:.2f} s of the call; with cards {TITLE_S + total + END_S:.2f} s")
    print("cuts (call-log seconds removed):", [(round(KEEP[i][1], 2), round(KEEP[i + 1][0], 2), round(KEEP[i + 1][0] - KEEP[i][1], 2)) for i in range(len(KEEP) - 1)])
    print(f"start trimmed: recording 0 to {KEEP[0][0] + OFFSET_S:.2f} s; end trimmed: recording {KEEP[-1][1] + OFFSET_S:.2f} s to the end")
    if plan_only:
        for c in wins:
            print(f"{c['s']:7.2f} {c['e']:7.2f} {c['who']:6} {c['prefix']} {c['text']}")
        for e in ev:
            print(f"{e['s']:7.2f} {e['label']} {e['value']}")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    cut_ui_and_audio()
    audio = build_audio(ev)

    bg = make_background()
    bg.save(WORK / "bg.png")
    plain = make_background(shadow=False)
    mask, ring = make_ui_mask_and_ring()
    mask.save(WORK / "mask.png")
    ring.save(WORK / "ring.png")
    n = int(round(total * FPS))
    strip, caps = Strip(bg), Captions(bg, wins, total)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "20", "-i", str(WORK / "ui.mkv"), "-frames:v", "1", str(WORK / "ui_still.png")])
    pop = Pop(Image.open(WORK / "ui_still.png").convert("RGB").crop(POP_UI_BOX), ev[-1]["s"] + POP_FROM_S)

    def strips():
        for k in range(n):
            t = k / FPS
            img = Image.new("RGB", (W, STRIP_H + CAP_H + POP_H))
            img.paste(strip.frame(t, ev), (0, 0))
            img.paste(caps.frame(t), (0, STRIP_H))
            img.paste(pop.frame(t), (0, STRIP_H + CAP_H))
            yield img

    main_ov = WORK / "main_ov.mp4"
    # the strips travel as one raw stream (top strip stacked on the caption zone) and are split again inside ffmpeg
    graph = (f"[2:v]format=gray[m];[1:v]format=rgba[u];[u][m]alphamerge[ui];"
             f"[0:v][ui]overlay={UI_X}:{UI_Y}[a];[a][3:v]overlay={UI_X}:{UI_Y}[b];"
             f"[4:v]split=3[s1][s2][s3];[s1]crop={W}:{STRIP_H}:0:0[top];[s2]crop={W}:{CAP_H}:0:{STRIP_H}[bot];"
             f"[s3]crop={POP_W}:{POP_H}:0:{STRIP_H + CAP_H}[pop];"
             f"[b][top]overlay=0:0[c];[c][bot]overlay=0:{CAP_Y}[d];[d][pop]overlay={POP_X}:{POP_Y}:enable='gte(t,{pop.start - 0.1:.3f})',format=yuv420p[out]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-loop", "1", "-framerate", str(FPS), "-i", str(WORK / "bg.png"),
           "-i", str(WORK / "ui.mkv"),
           "-loop", "1", "-framerate", str(FPS), "-i", str(WORK / "mask.png"),
           "-loop", "1", "-framerate", str(FPS), "-i", str(WORK / "ring.png"),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{STRIP_H + CAP_H + POP_H}", "-r", str(FPS), "-i", "-",
           "-filter_complex", graph, "-map", "[out]", "-frames:v", str(n), "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-r", str(FPS), str(main_ov)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for fr in strips():
            p.stdin.write(fr.tobytes())
    finally:
        p.stdin.close()
    err = p.stderr.read().decode("utf8", "replace")
    if p.wait():
        sys.exit("ffmpeg overlay failed:\n" + err[-3000:])

    enc = ["-c:v", "libx264", "-crf", "12", "-preset", "fast", "-pix_fmt", "yuv420p"]
    pipe_frames((title_frame(plain, k / FPS / 1.35) for k in range(int(TITLE_S * FPS))), (W, H), enc, WORK / "title.mp4")
    pipe_frames((end_frame(plain, k / FPS) for k in range(int(END_S * FPS))), (W, H), enc, WORK / "end.mp4")

    fade_f = FADE_S
    graph = (f"[1:v]fade=t=in:d={fade_f},fade=t=out:st={total - fade_f}:d={fade_f}[mv];"
             f"[0:v][mv][2:v]concat=n=3:v=1:a=0,fps={FPS},format=yuv420p[v];"
             f"[3:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,asplit=2[vox][sc];"
             f"[4:a][sc]sidechaincompress=threshold=0.08:ratio=2.5:attack=25:release=500:makeup=1[duck];"
             f"[vox][duck]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.89:level=disabled[a]")
    final = OUT / OUT_NAME
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(WORK / "title.mp4"), "-i", str(main_ov), "-i", str(WORK / "end.mp4"),
         "-i", str(audio), "-i", str(WORK / "music.wav"), "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-profile:v", "high", "-level", "4.2", "-preset", "slow", "-crf", "17", "-maxrate", "14M", "-bufsize", "28M",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(final)])
    print("wrote", final)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--plan", action="store_true", help="print the cut list and caption windows, render nothing")
    ap.add_argument("--call", default=CALL_ID, help="call id in data/calls/")
    ap.add_argument("--recording", default=str(SRC), help="the screen recording of that call")
    ap.add_argument("--music", choices=["calm", "build"], default="calm", help="background music: slow pad (default) or a pulse that builds")
    ap.add_argument("--out", default=None, help="output file name inside data/video/")
    a = ap.parse_args()
    CALL_ID, SRC = a.call, Path(a.recording)
    MUSIC_STYLE = a.music
    OUT_NAME = a.out or ("mirage_demo_x_build.mp4" if a.music == "build" else "mirage_demo_x.mp4")
    render(a.plan)
