#!/usr/bin/env python3
"""Build the 5-page Ev pitch deck PDF v2 — more creative, more genuine.

Vector-only, no external images, stays far under 10 MB.
Design: clean, creative, subtle, non-formal. Plain language for non-technical investors.
"""
import math

from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

W, H = 960, 540  # 16:9 landscape slide

BG = HexColor("#FBF8F3")
INK = HexColor("#23273B")
ACCENT = HexColor("#E07B39")
ACCENT_SOFT = HexColor("#F5D9C2")
TEAL = HexColor("#2A9D8F")
TEAL_SOFT = HexColor("#D3EDE9")
GRAY = HexColor("#8B90A5")
LIGHT = HexColor("#ECE5D9")
CARD = HexColor("#FFFFFF")
DARK_BG = HexColor("#1B1E30")
DARK_CARD = HexColor("#262B45")

OUT = "/Users/sahajpatel/Code/ev/pitch-deck/EV_Pitch_Deck.pdf"


def wrap(text, font, size, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if pdfmetrics.stringWidth(t, font, size) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def para(c, text, x, y, font, size, color, max_w, lh=None, align="left"):
    lh = lh or size * 1.35
    c.setFillColor(color)
    c.setFont(font, size)
    yy = y
    for line in wrap(text, font, size, max_w):
        if align == "center":
            c.drawCentredString(x, yy, line)
        elif align == "right":
            c.drawRightString(x, yy, line)
        else:
            c.drawString(x, yy, line)
        yy -= lh
    return yy


def eyebrow(c, text, x, y, align="left", color=None):
    color = color or ACCENT
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(color)
    tw = pdfmetrics.stringWidth(text, "Helvetica-Bold", 11)
    if align == "center":
        x -= tw / 2
    c.drawString(x, y, text)
    c.setStrokeColor(color)
    c.setLineWidth(2)
    c.line(x, y - 8, x + min(tw, 64), y - 8)


def footer(c, n, dark=False):
    fg = HexColor("#9AA0B5") if dark else GRAY
    c.setFont("Helvetica", 9)
    c.setFillColor(fg)
    left = "Ev  \u2014  Your mind, with backup."
    c.drawString(48, 28, left)
    c.drawRightString(W - 48, 28, f"{n} / 5")
    # page dots, centered — subtle wayfinding
    cx = W / 2
    for i in range(1, 6):
        c.setFillColor(ACCENT if i == n else (HexColor("#4A4F6B") if dark else LIGHT))
        c.circle(cx + (i - 3) * 18, 30, 4 if i == n else 3, stroke=0, fill=1)


def dot_grid(c, x0, y0, x1, y1, step=26, color=None, r=1.3, alpha=0.5):
    c.saveState()
    c.setFillColor(color or LIGHT)
    try:
        c.setFillAlpha(alpha)
    except Exception:
        pass
    y = y0
    while y <= y1:
        x = x0
        while x <= x1:
            c.circle(x, y, r, stroke=0, fill=1)
            x += step
        y += step
    c.restoreState()


def pill(c, x, y, text, font="Helvetica-Bold", size=11, fg=None, bg=None, stroke=None):
    fg = fg or INK
    bg = bg or CARD
    stroke = stroke or LIGHT
    wdt = pdfmetrics.stringWidth(text, font, size) + 30
    c.setFillColor(bg)
    c.setStrokeColor(stroke)
    c.setLineWidth(1)
    c.roundRect(x, y, wdt, 26, 13, stroke=1, fill=1)
    c.setFillColor(fg)
    c.setFont(font, size)
    c.drawCentredString(x + wdt / 2, y + 9, text)
    return wdt


def orb(c, cx, cy, r):
    """Hero orb with three labeled orbiters: remembers / acts / protects."""
    c.saveState()
    for mult, alpha in ((1.55, 0.08), (1.28, 0.14), (1.0, 0.28)):
        c.setFillColor(ACCENT)
        try:
            c.setFillAlpha(alpha)
        except Exception:
            pass
        c.circle(cx, cy, r * mult, stroke=0, fill=1)
    try:
        c.setFillAlpha(1.0)
    except Exception:
        pass
    c.setFillColor(ACCENT)
    c.circle(cx, cy, r * 0.62, stroke=0, fill=1)
    c.setFillColor(HexColor("#FFF6EC"))
    c.circle(cx - r * 0.18, cy + r * 0.2, r * 0.14, stroke=0, fill=1)
    c.setStrokeColor(ACCENT)
    c.setLineWidth(1)
    try:
        c.setStrokeAlpha(0.45)
    except Exception:
        pass
    c.circle(cx, cy, r * 1.62, stroke=1, fill=0)
    try:
        c.setStrokeAlpha(1.0)
    except Exception:
        pass
    labels = ((32, "remembers"), (152, "acts"), (268, "protects"))
    for ang, word in labels:
        a = math.radians(ang)
        dx, dy = 1.62 * r * math.cos(a), 1.62 * r * math.sin(a)
        c.setFillColor(INK)
        c.circle(cx + dx, cy + dy, 5, stroke=0, fill=1)
        c.setFillColor(CARD)
        c.setStrokeColor(LIGHT)
        c.setLineWidth(1)
        tw = pdfmetrics.stringWidth(word, "Helvetica-Bold", 10) + 20
        lx, ly = cx + dx - tw / 2, cy + dy + 10
        c.roundRect(lx, ly, tw, 22, 11, stroke=1, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(cx + dx, ly + 8, word)
    c.restoreState()


def page1(c):
    c.setFillColor(BG)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    dot_grid(c, 36, 320, 220, 500)
    dot_grid(c, 740, 60, 924, 240)
    orb(c, W / 2, 216, 58)
    eyebrow(c, "EV   \u2022   A PERSONAL AI COMPANION", W / 2, 474, align="center")
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 92)
    c.drawCentredString(W / 2, 372, "Meet Ev.")
    c.setFont("Helvetica", 23)
    c.setFillColor(GRAY)
    c.drawCentredString(W / 2, 332, "A companion that actually knows you.")
    chips = ["One owner: you", "Voice + vision + memory", "Private by default"]
    c.setFont("Helvetica-Bold", 12)
    widths = [pdfmetrics.stringWidth(t, "Helvetica-Bold", 12) + 34 for t in chips]
    x = (W - sum(widths) - 2 * 12) / 2
    y = 104
    for t, wdt in zip(chips, widths):
        c.setFillColor(CARD)
        c.setStrokeColor(LIGHT)
        c.setLineWidth(1)
        c.roundRect(x, y, wdt, 30, 15, stroke=1, fill=1)
        c.setFillColor(INK)
        c.drawCentredString(x + wdt / 2, y + 11, t)
        x += wdt + 12
    # genuine live strip — this is working software, not a mockup
    c.setFont("Helvetica-Bold", 10.5)
    c.setFillColor(TEAL)
    c.drawCentredString(W / 2, 82, "LIVE TODAY  \u2022  CLI  \u2022  WEB  \u2022  MAC  \u2022  iPHONE + WATCH")
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(GRAY)
    c.drawCentredString(W / 2, 66, "19 PARTS OF LIFE, ONE MEMORY")
    c.setFont("Helvetica-Oblique", 14)
    c.setFillColor(ACCENT)
    c.drawCentredString(W / 2, 44, "Ev remembers.  You live.")
    footer(c, 1)
    c.showPage()


def thread_visual(c, x0, y0):
    cards = [("Dentist moved to Tue", "calendar \u2022 kept, with proof"),
             ("Maya loves oat lattes", "noted 3 wks ago \u2022 still true"),
             ("Lease renews Jun 1", "from email \u2022 never lost"),
             ("Idea: rooftop herbs", "voice note \u2022 dated + sourced")]
    c.saveState()
    c.setStrokeColor(ACCENT)
    c.setLineWidth(3)
    p = c.beginPath()
    p.moveTo(x0 - 10, y0 + 300)
    p.curveTo(x0 + 60, y0 + 220, x0 - 20, y0 + 150, x0 + 50, y0 + 40)
    c.drawPath(p, stroke=1, fill=0)
    cy = y0 + 268
    for i, (title, meta) in enumerate(cards):
        xx = x0 + (44 if i % 2 == 0 else 108)
        c.setFillColor(CARD)
        c.setStrokeColor(LIGHT)
        c.setLineWidth(1)
        c.roundRect(xx, cy, 218, 62, 10, stroke=1, fill=1)
        c.setFillColor(ACCENT)
        c.circle(xx - 6, cy + 31, 7, stroke=0, fill=1)
        c.setFillColor(CARD)
        c.circle(xx - 6, cy + 31, 2.6, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(xx + 16, cy + 35, title)
        c.setFillColor(GRAY)
        c.setFont("Helvetica", 10.5)
        c.drawString(xx + 16, cy + 18, meta)
        cy -= 80
    c.restoreState()


def page2(c):
    c.setFillColor(BG)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    c.setFillColor(CARD)
    c.roundRect(28, 28, W - 56, H - 56, 18, stroke=0, fill=1)
    eyebrow(c, "THE HUMAN PROBLEM", 72, 452)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 40)
    c.drawString(72, 408, "Life moves fast.")
    c.drawString(72, 362, "Memory can\u2019t keep up.")
    c.setStrokeColor(ACCENT_SOFT)
    c.setLineWidth(6)
    c.line(72, 340, 220, 340)
    bullets = [("Appointments slip. Receipts vanish. Great ideas fade by Friday.",
                "Every week, ordinary life leaks through the cracks."),
               ("Today\u2019s assistants forget everything the moment you close them.",
                "You repeat yourself. They start over. Nothing sticks."),
               ("And the helpful ones read everything \u2014 your mail, your files, your life.",
                "Convenience today costs privacy forever.")]
    y = 296
    for b, sub in bullets:
        c.setFillColor(ACCENT)
        c.circle(84, y + 4, 5, stroke=0, fill=1)
        y = para(c, b, 102, y, "Helvetica-Bold", 13, INK, 360)
        y = para(c, sub, 102, y - 2, "Helvetica", 11.5, GRAY, 360)
        y -= 10
    # genuine contrast strip: what Ev does differently, in one line each
    c.setFillColor(TEAL_SOFT)
    c.roundRect(72, 56, 440, 58, 10, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(90, 96, "Ev is the opposite:")
    c.setFont("Helvetica", 11)
    c.setFillColor(HexColor("#3A3F55"))
    c.drawString(90, 78, "dated, sourced memories \u2022 secrets never reach the AI \u2022 honest offline mode")
    c.setFont("Helvetica-Oblique", 10.5)
    c.setFillColor(GRAY)
    c.drawString(90, 62, "It shows its work \u2014 or says it doesn\u2019t know.")
    thread_visual(c, 556, 96)
    footer(c, 2)
    c.showPage()


def phone_snap(c, x, y, wdt, hgt, title, lines, proof):
    c.setFillColor(INK)
    c.roundRect(x, y, wdt, hgt, 16, stroke=0, fill=1)
    c.setFillColor(CARD)
    c.roundRect(x + 7, y + 7, wdt - 14, hgt - 14, 11, stroke=0, fill=1)
    c.setFillColor(INK)
    c.circle(x + wdt / 2, y + hgt - 20, 3, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", 11.5)
    c.setFillColor(INK)
    c.drawString(x + 20, y + hgt - 46, title)
    c.setStrokeColor(ACCENT)
    c.setLineWidth(3)
    c.line(x + 20, y + hgt - 54, x + 64, y + hgt - 54)
    yy = y + hgt - 78
    for bold, txt, wd in lines:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        c.setFillColor(INK if bold else GRAY)
        c.drawString(x + 20, yy, txt)
        yy -= 17
        if wd:
            c.setFillColor(ACCENT_SOFT if bold else LIGHT)
            c.roundRect(x + 20, yy - 2, wd, 8, 4, stroke=0, fill=1)
            yy -= 16
    # provenance line — the genuine touch: Ev cites why it acted
    c.setFont("Helvetica-Oblique", 9)
    c.setFillColor(TEAL)
    c.drawString(x + 20, y + 52, proof)
    c.setFillColor(ACCENT)
    c.roundRect(x + 20, y + 20, wdt - 40, 24, 12, stroke=0, fill=1)
    c.setFillColor(CARD)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(x + wdt / 2, y + 28, "Done \u2022 while you slept")


def page3(c):
    c.setFillColor(BG)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    eyebrow(c, "A NORMAL TUESDAY WITH EV", 72, 480)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 36)
    c.drawString(72, 438, "Proof from everyday life.")
    c.setFont("Helvetica", 13.5)
    c.setFillColor(GRAY)
    c.drawString(72, 414, "Not a demo. Just Tuesday \u2014 three small moments Ev handles on its own.")
    # trust pill, top right
    pill(c, 700, 452, "on your machines \u2022 works offline", size=10.5, fg=TEAL,
         bg=TEAL_SOFT, stroke=TEAL_SOFT)
    cols = [("It codes.", "Overnight fix, explained plainly.",
             [(True, "Fixed: login crash 2:14 AM", 150), (False, "Tests pass \u2022 ready to review", 120)],
             "rule from Mar 3 \u2022 you approved once"),
            ("It orders.", "The usual, before you run out.",
             [(True, "Oat milk price dropped 18%", 130), (False, "Arrives Thu \u2022 under budget", 120)],
             "asked first \u2022 receipt kept"),
            ("Your second half.", "Mornings that run themselves.",
             [(True, "30-sec brief: 3 things today", 150), (False, "Leave 8:40 \u2022 rain at 9", 120)],
             "hands-free \u2022 filed away")]
    x = 72
    for title, sub, lines, proof in cols:
        c.setFont("Helvetica-Bold", 17)
        c.setFillColor(INK)
        c.drawString(x + 4, 376, title)
        c.setFont("Helvetica", 11.5)
        c.setFillColor(GRAY)
        c.drawString(x + 4, 358, sub)
        phone_snap(c, x, 84, 252, 252, "Ev", lines, proof)
        x += 276
    # bottom honesty strip — what makes Ev safe to trust with real life
    strips = ["Asks before acting", "Shows its work", "Says when unsure"]
    sx = 72
    c.setFont("Helvetica-Bold", 10.5)
    for s in strips:
        wdt = pdfmetrics.stringWidth(s, "Helvetica-Bold", 10.5) + 44
        c.setFillColor(CARD)
        c.setStrokeColor(LIGHT)
        c.roundRect(sx, 48, wdt, 24, 12, stroke=1, fill=1)
        c.setFillColor(TEAL)
        c.circle(sx + 16, 60, 4, stroke=0, fill=1)
        c.setFillColor(INK)
        c.drawString(sx + 26, 57, s)
        sx += wdt + 10
    c.setFont("Helvetica-Oblique", 10.5)
    c.setFillColor(GRAY)
    c.drawString(sx + 6, 57, "Permissioned voice, vision, and live data \u2014 you stay in charge.")
    footer(c, 3)
    c.showPage()


def page4(c):
    c.setFillColor(BG)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    eyebrow(c, "SIMPLE MATH THAT SCALES", 72, 480)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 36)
    c.drawString(72, 438, "$19 a month. Built to last.")
    c.setFont("Helvetica", 13.5)
    c.setFillColor(GRAY)
    c.drawString(72, 414, "One flat subscription. No ads. No selling your data. Ever.")
    # price card
    c.setFillColor(INK)
    c.roundRect(72, 108, 300, 272, 18, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(104, 344, "EV PERSONAL  \u2022  SUBSCRIPTION")
    c.setFillColor(CARD)
    c.setFont("Helvetica-Bold", 68)
    c.drawString(100, 274, "$19")
    c.setFont("Helvetica", 15)
    c.setFillColor(HexColor("#C6CADB"))
    pw = pdfmetrics.stringWidth("$19", "Helvetica-Bold", 68)
    c.drawString(100 + pw + 6, 278, "/ month")
    c.setFont("Helvetica", 11.5)
    for i, t in enumerate(["Everything included", "Cancel anytime", "Your data stays yours"]):
        c.setFillColor(ACCENT)
        c.circle(112, 238 - i * 25, 4, stroke=0, fill=1)
        c.setFillColor(CARD)
        c.drawString(126, 234 - i * 25, t)
    c.setFont("Helvetica", 10.5)
    c.setFillColor(HexColor("#C6CADB"))
    c.drawString(104, 150, "Rivals charge $20+ a month for less.")
    c.setFont("Helvetica-Oblique", 10)
    c.setFillColor(HexColor("#9AA0B5"))
    c.drawString(104, 130, "$228 a year \u2014 less than one lost day.")
    # margin bars with genuine ARR math
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(INK)
    c.drawString(430, 352, "Profit grows as Ev grows")
    c.setFont("Helvetica", 11.5)
    c.setFillColor(GRAY)
    c.drawString(430, 332, "Everyday work runs free on your device; the hosted brain is capped.")
    bars = [("1k owners", 0.40, "40%", "$228k / yr"),
            ("10k owners", 0.50, "50%", "$2.28M / yr"),
            ("100k owners", 0.55, "55%", "$22.8M / yr")]
    y = 292
    for label, frac, pct, arr in bars:
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(INK)
        c.drawString(430, y + 8, label)
        c.setFont("Helvetica", 10)
        c.setFillColor(GRAY)
        c.drawString(430, y - 8, arr)
        c.setFillColor(LIGHT)
        c.roundRect(560, y - 4, 230, 22, 11, stroke=0, fill=1)
        c.setFillColor(ACCENT)
        c.roundRect(560, y - 4, 230 * frac, 22, 11, stroke=0, fill=1)
        c.setFillColor(CARD)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(568, y + 3, pct)
        y -= 46
    # why margin holds — three honest reasons
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(INK)
    c.drawString(430, 148, "Why the 40\u201355% margin holds:")
    c.setFont("Helvetica", 10.5)
    c.setFillColor(HexColor("#3A3F55"))
    whys = ["Local-first \u2014 everyday work costs us ~$0.",
            "One memory \u2014 no rework, no re-asking.",
            "Private \u2014 no ad machinery to feed."]
    wy = 130
    for wline in whys:
        c.setFillColor(TEAL)
        c.circle(436, wy + 3, 3.5, stroke=0, fill=1)
        c.setFillColor(HexColor("#3A3F55"))
        c.drawString(448, wy, wline)
        wy -= 17
    # market strip
    c.setFillColor(CARD)
    c.setStrokeColor(LIGHT)
    c.setLineWidth(1)
    c.roundRect(640, 108, 248, 66, 12, stroke=1, fill=1)
    c.setFillColor(GRAY)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(660, 154, "WHY NOW")
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 12.5)
    c.drawString(660, 136, "337 copycats made $120M")
    c.setFont("Helvetica", 10.5)
    c.setFillColor(GRAY)
    c.drawString(660, 120, "and break when you need them.")
    footer(c, 4)
    c.showPage()


def stars(c, n=40):
    import random
    rnd = random.Random(7)
    c.saveState()
    for _ in range(n):
        x = rnd.uniform(30, W - 30)
        y = rnd.uniform(300, H - 20)
        r = rnd.uniform(0.8, 2.0)
        c.setFillColor(HexColor("#4A4F6B"))
        try:
            c.setFillAlpha(rnd.uniform(0.35, 0.8))
        except Exception:
            pass
        c.circle(x, y, r, stroke=0, fill=1)
    c.restoreState()


def page5(c):
    c.setFillColor(DARK_BG)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    stars(c)
    # sunrise arc band
    c.saveState()
    c.setFillColor(ACCENT)
    try:
        c.setFillAlpha(0.9)
    except Exception:
        pass
    c.circle(W - 120, 40, 210, stroke=0, fill=1)
    try:
        c.setFillAlpha(0.18)
    except Exception:
        pass
    c.circle(W - 120, 40, 280, stroke=0, fill=1)
    c.restoreState()
    c.setFillColor(DARK_BG)
    c.rect(0, 0, W - 330, 190, stroke=0, fill=1)
    c.setFillColor(HexColor("#F5D9C2"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, 474, "NO REAL COMPETITOR  \u2014  POINT TOOLS EXIST. NO SECOND HALF EXISTS.")
    c.setFillColor(CARD)
    c.setFont("Helvetica-Bold", 50)
    c.drawString(68, 418, "Own your mind.")
    para(c, "Big Tech rents your data back to you, and most people don\u2019t trust it. Ev is different: one owner, nineteen parts of life covered, running on your own machines \u2014 and it is working software today, not slides.",
         72, 384, "Helvetica", 13, HexColor("#D7DBE8"), 520)
    # genuine proof chips — measured, not promised
    proofs = ["live: phone \u2022 web \u2022 Mac \u2022 watch", "20/20 safety checks green", "works with no internet"]
    px = 72
    for ptext in proofs:
        wdt = pdfmetrics.stringWidth(ptext, "Helvetica-Bold", 10) + 28
        c.setFillColor(DARK_CARD)
        c.setStrokeColor(HexColor("#3A3F5C"))
        c.roundRect(px, 316, wdt, 26, 13, stroke=1, fill=1)
        c.setFillColor(TEAL_SOFT)
        c.circle(px + 15, 329, 4, stroke=0, fill=1)
        c.setFillColor(CARD)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(px + 24, 326, ptext)
        px += wdt + 10
    # ask card
    c.setFillColor(CARD)
    c.roundRect(72, 118, 500, 176, 16, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(100, 260, "THE ASK")
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(100, 234, "Back Ev to the first 10,000 owners.")
    c.setFont("Helvetica", 11.5)
    c.setFillColor(GRAY)
    c.drawString(100, 212, "App polish  \u2022  safety & privacy checks  \u2022  guides + support  \u2022  backup & sync")
    c.setStrokeColor(LIGHT)
    c.setLineWidth(1)
    c.line(100, 194, 544, 194)
    c.setFont("Helvetica", 11.5)
    c.setFillColor(INK)
    c.drawString(100, 172, "Grants first (~$275K), then ~$1.25\u20132M to scale past 10k.")
    c.setFont("Helvetica-Oblique", 11)
    c.setFillColor(GRAY)
    c.drawString(100, 152, "Every dollar makes Ev kinder, safer, and more yours.")
    c.setFont("Helvetica", 10)
    c.setFillColor(TEAL)
    c.drawString(100, 132, "We show what is still growing \u2014 honesty is the product.")
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(CARD)
    c.drawString(72, 86, "Your mind, with backup.")
    c.setFont("Helvetica", 10)
    c.setFillColor(HexColor("#9AA0B5"))
    c.drawString(72, 64, "$19/mo subscription  \u2022  private by default  \u2022  Ev remembers. You live.")
    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#9AA0B5"))
    c.drawRightString(W - 48, 28, "5 / 5")
    # dots for dark page
    cx = W / 2
    for i in range(1, 6):
        c.setFillColor(ACCENT if i == 5 else HexColor("#4A4F6B"))
        c.circle(cx + (i - 3) * 18, 30, 4 if i == 5 else 3, stroke=0, fill=1)
    c.showPage()


def main():
    c = canvas.Canvas(OUT, pagesize=(W, H))
    c.setTitle("Ev \u2014 Pitch Deck")
    c.setAuthor("Ev")
    c.setSubject("Ev pitch deck: a personal AI companion that actually knows you")
    c.setKeywords("Ev, personal AI, pitch deck, subscription, privacy, local-first")
    page1(c)
    page2(c)
    page3(c)
    page4(c)
    page5(c)
    c.save()
    print("wrote", OUT)


if __name__ == "__main__":
    main()
