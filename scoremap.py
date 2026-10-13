"""The Growth Effectiveness map.

Eight stages on a radar, the eight handoffs on the lines between them,
because that is where the join is. Drawn from the frozen scorecard and
nothing else: no figure here is typed, chosen or adjusted.

Drawn with Pillow rather than an SVG renderer. Cairo is a system
dependency and a painful one on Windows, and this has to run on a
consultant's laptop as readily as on a server.

Two things the sales-deck version gets wrong and this does not.

  The dashed octagon is labelled a benchmark there, and compared against a
  well-engineered system "in the same segment". Blend has no peer dataset
  and the Deliverable Kit instructs that the claim be removed wherever it
  appears. It is the defined standard: what the level 4 anchors describe.

  A stage with no scored element has no point. The sales version has no way
  to show that, and a map that silently plots nought for a stage nobody
  assessed is the same error as scoring it nought.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

NAVY = (24, 17, 68)
TEAL = (36, 184, 203)
TEAL_FILL = (36, 184, 203, 90)
TEAL_DARK = (27, 137, 152)
CORAL = (249, 108, 69)
WHITE = (255, 255, 255)
GREY = (232, 232, 232)
MUTED = (93, 88, 124)

# What the dashed ring represents. The level 4 anchors describe a
# well-engineered revenue system, so that is the line being drawn.
DEFINED_STANDARD = 4.0

# Drawn at three times the final size and reduced, which is how Pillow gets
# clean edges without an SVG renderer.
SCALE = 3

# Searched in order. The repository's own fonts folder first, so dropping
# Red Hat Display in there is all it takes to get the brand typeface,
# then the places an operating system keeps fonts.
FONT_DIRS = [Path(__file__).parent / "fonts",
             Path("/usr/share/fonts/truetype/google-fonts"),
             Path("/usr/share/fonts/truetype/dejavu"),
             Path("/usr/share/fonts/truetype/freefont"),
             Path("/Library/Fonts"), Path("C:/Windows/Fonts")]
FONT_NAMES = ["RedHatDisplay-Bold.ttf", "RedHatDisplay-Medium.ttf",
              "RedHatDisplay-Regular.ttf"]


def _font(size: int, bold: bool = False):
    """Red Hat Display where it is available, something legible where not.

    The brand font is a Google font and not redistributable through this
    repository, so it is looked for and not assumed. The caller is told
    which was used, because a report in the wrong typeface is worth
    noticing before it reaches a client.
    """
    wanted = FONT_NAMES[0] if bold else FONT_NAMES[2]
    for d in FONT_DIRS:
        for name in ([wanted] + FONT_NAMES):
            p = d / name
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size), name
                except OSError:
                    pass
    # Red Hat Display is a Google font and is not redistributed here, so a
    # fallback has to exist. Anything with real outlines beats the bitmap
    # default, which ignores the size it is given and renders a report
    # illegibly small.
    for fallback in (("Poppins-Bold.ttf", "DejaVuSans-Bold.ttf",
                      "FreeSansBold.ttf", "Arialbd.ttf", "arialbd.ttf")
                     if bold else
                     ("Poppins-Regular.ttf", "DejaVuSans.ttf",
                      "FreeSans.ttf", "Arial.ttf", "arial.ttf")):
        for d in FONT_DIRS:
            p = d / fallback
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size), fallback
                except OSError:
                    pass
    return ImageFont.load_default(), "the default bitmap font, which ignores size"


def _ring(cx, cy, r, n=8, rotation=-math.pi / 2):
    return [(cx + r * math.cos(rotation + 2 * math.pi * i / n),
             cy + r * math.sin(rotation + 2 * math.pi * i / n))
            for i in range(n)]


def draw(data: dict, out_path: str) -> dict:
    """Draw the map.

    data wants:
      client, engagement        for the title
      stages        [{code, name, average or None}] in order, eight of them
      handoffs      [{ref, from_stage, to_stage, value or None}]
      score         {growth_effectiveness, elements_scored, elements_total,
                     handoff_integrity, handoffs_scored, handoffs_total,
                     elements_not_applicable, elements_not_assessed}
      interventions [str] up to three
      opportunity   str
    """
    W, H = 1420 * SCALE, 970 * SCALE
    img = Image.new("RGBA", (W, H), WHITE + (255,))
    d = ImageDraw.Draw(img)

    f_title, used = _font(15 * SCALE, True)
    f_stage, _ = _font(17 * SCALE, True)
    f_big, _ = _font(54 * SCALE, True)
    f_label, _ = _font(13 * SCALE, True)
    f_body, _ = _font(15 * SCALE)
    f_small, _ = _font(12 * SCALE)

    d.text((40 * SCALE, 30 * SCALE), "THE GROWTH EFFECTIVENESS SCORE",
           font=f_title, fill=TEAL)

    cx, cy, R = 430 * SCALE, 420 * SCALE, 250 * SCALE
    px0, py0, px1 = 840 * SCALE, 128 * SCALE, 1380 * SCALE
    stages = data["stages"]
    n = len(stages)

    # The rings, one per level.
    for level in range(1, 6):
        d.polygon(_ring(cx, cy, R * level / 5, n), outline=GREY, width=1 * SCALE)
    for p in _ring(cx, cy, R, n):
        d.line([(cx, cy), p], fill=GREY, width=1 * SCALE)

    # The defined standard. Dashed, because it is a line to reach rather
    # than a measurement.
    pts = _ring(cx, cy, R * DEFINED_STANDARD / 5, n)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        steps = 26
        for s in range(0, steps, 2):
            t0, t1 = s / steps, min((s + 1) / steps, 1)
            d.line([(a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                    (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)],
                   fill=NAVY, width=2 * SCALE)

    # This company. A stage with no scored element has no point, and the
    # shape breaks there rather than being drawn through nought.
    outer = _ring(cx, cy, R, n)
    scored = [(i, s) for i, s in enumerate(stages) if s.get("average") is not None]
    poly = []
    for i, s in scored:
        ang = math.atan2(outer[i][1] - cy, outer[i][0] - cx)
        rr = R * float(s["average"]) / 5
        poly.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    if len(poly) >= 3 and len(scored) == n:
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).polygon(poly, fill=TEAL_FILL)
        img = Image.alpha_composite(img, overlay)
        d = ImageDraw.Draw(img)
        d.line(poly + [poly[0]], fill=TEAL_DARK, width=3 * SCALE)
    elif len(poly) >= 2:
        d.line(poly, fill=TEAL_DARK, width=3 * SCALE)
    for p in poly:
        d.ellipse([p[0] - 5 * SCALE, p[1] - 5 * SCALE,
                   p[0] + 5 * SCALE, p[1] + 5 * SCALE], fill=TEAL_DARK)

    # Stage names, pushed out from their axis.
    for i, s in enumerate(stages):
        ang = math.atan2(outer[i][1] - cy, outer[i][0] - cx)
        lx = cx + (R + 54 * SCALE) * math.cos(ang)
        ly = cy + (R + 54 * SCALE) * math.sin(ang)
        label = f"{s['code']} {s['name']}"
        w = d.textlength(label, font=f_stage)
        anchor_x = lx - w / 2
        if math.cos(ang) > 0.4:
            anchor_x = lx - w * 0.1
        elif math.cos(ang) < -0.4:
            anchor_x = lx - w * 0.9
        # Keep it on the canvas. A stage name running off the left edge is
        # the commonest way this drawing fails, because the longest names
        # sit where there is least room.
        anchor_x = max(8 * SCALE, min(anchor_x, px0 - w - 16 * SCALE))
        d.text((anchor_x, ly - 10 * SCALE), label, font=f_stage, fill=NAVY)

    # The handoffs, on the line between the two stages they join.
    for h in data.get("handoffs", []):
        try:
            a = next(i for i, s in enumerate(stages) if s["code"] == h["from_stage"])
            b = next(i for i, s in enumerate(stages) if s["code"] == h["to_stage"])
        except StopIteration:
            continue
        if (b - a) % n not in (1, n - 1):
            continue
        mid = ((outer[a][0] + outer[b][0]) / 2, (outer[a][1] + outer[b][1]) / 2)
        ang = math.atan2(mid[1] - cy, mid[0] - cx)
        px = cx + (R + 16 * SCALE) * math.cos(ang)
        py = cy + (R + 16 * SCALE) * math.sin(ang)
        r = 15 * SCALE
        d.ellipse([px - r, py - r, px + r, py + r], outline=CORAL,
                  width=2 * SCALE, fill=WHITE)
        v = "–" if h.get("value") is None else str(h["value"])
        w = d.textlength(v, font=f_label)
        d.text((px - w / 2, py - 9 * SCALE), v, font=f_label, fill=CORAL)

    # The panel.
    sc = data["score"]
    d.rounded_rectangle([px0, py0, px1, py0 + 376 * SCALE], radius=10 * SCALE,
                        outline=GREY, width=2 * SCALE)
    d.rounded_rectangle([px0, py0, px1, py0 + 8 * SCALE], radius=4 * SCALE,
                        fill=TEAL)
    y = py0 + 36 * SCALE
    d.text((px0 + 36 * SCALE, y), "GROWTH EFFECTIVENESS", font=f_title, fill=TEAL)
    y += 26 * SCALE
    ges = "–" if sc.get("growth_effectiveness") is None else str(int(sc["growth_effectiveness"]))
    d.text((px0 + 36 * SCALE, y), ges, font=f_big, fill=TEAL_DARK)
    gw = d.textlength(ges, font=f_big)
    d.text((px0 + 46 * SCALE + gw, y + 26 * SCALE), "/ 100", font=f_body, fill=MUTED)
    base = (f"from {sc.get('elements_scored', 0)} of "
            f"{sc.get('elements_total', 40)} elements")
    d.text((px0 + 46 * SCALE + gw, y + 48 * SCALE), base, font=f_small, fill=MUTED)

    y += 96 * SCALE
    d.line([(px0 + 36 * SCALE, y), (px1 - 36 * SCALE, y)], fill=GREY, width=2 * SCALE)
    y += 22 * SCALE
    d.text((px0 + 36 * SCALE, y), "HANDOFF INTEGRITY", font=f_title, fill=CORAL)
    y += 26 * SCALE
    hi = "–" if sc.get("handoff_integrity") is None else str(int(sc["handoff_integrity"]))
    d.text((px0 + 36 * SCALE, y), hi, font=f_big, fill=CORAL)
    hw = d.textlength(hi, font=f_big)
    d.text((px0 + 46 * SCALE + hw, y + 26 * SCALE),
           f"/ 40 — scored separately", font=f_body, fill=MUTED)

    y += 96 * SCALE
    d.text((px0 + 36 * SCALE, y), "PRIORITY INTERVENTIONS", font=f_title, fill=TEAL)
    y += 28 * SCALE
    for item in data.get("interventions", [])[:3]:
        d.ellipse([px0 + 38 * SCALE, y + 6 * SCALE,
                   px0 + 48 * SCALE, y + 16 * SCALE], fill=CORAL)
        d.text((px0 + 60 * SCALE, y), item[:58], font=f_body, fill=NAVY)
        y += 26 * SCALE

    # The one thing.
    oy = py0 + 406 * SCALE
    d.rounded_rectangle([px0, oy, px1, oy + 162 * SCALE], radius=10 * SCALE,
                        outline=GREY, width=2 * SCALE)
    d.rounded_rectangle([px0, oy, px1, oy + 8 * SCALE], radius=4 * SCALE, fill=CORAL)
    d.text((px0 + 36 * SCALE, oy + 30 * SCALE), "BIGGEST SINGLE OPPORTUNITY",
           font=f_title, fill=CORAL)
    words, line, ly = (data.get("opportunity") or "").split(), "", oy + 62 * SCALE
    for word in words:
        trial = (line + " " + word).strip()
        if d.textlength(trial, font=f_body) > (px1 - px0 - 72 * SCALE):
            d.text((px0 + 36 * SCALE, ly), line, font=f_body, fill=NAVY)
            ly += 24 * SCALE
            line = word
        else:
            line = trial
    if line:
        d.text((px0 + 36 * SCALE, ly), line, font=f_body, fill=NAVY)

    # Stage by stage, underneath.
    by = 760 * SCALE
    d.text((40 * SCALE, by), "STAGE BY STAGE", font=f_title, fill=TEAL)
    by += 34 * SCALE
    col = (W - 80 * SCALE) / n
    for i, s in enumerate(stages):
        x = 40 * SCALE + i * col
        d.text((x, by), s["name"][:18], font=f_label, fill=NAVY)
        avg = s.get("average")
        for k in range(5):
            filled = avg is not None and float(avg) >= k + 0.5
            r = 9 * SCALE
            ccx, ccy = x + 10 * SCALE + k * 26 * SCALE, by + 40 * SCALE
            d.ellipse([ccx - r, ccy - r, ccx + r, ccy + r],
                      fill=TEAL_DARK if filled else GREY)
        if avg is None:
            d.text((x + 140 * SCALE, by + 32 * SCALE), "not assessed",
                   font=f_small, fill=MUTED)

    ly = 884 * SCALE
    for mark, text, colour in (
            ("fill", "This company, scored across the elements in each stage "
                     "and plotted at the middle of each wedge.", TEAL_DARK),
            ("dash", "The defined standard: what a well-engineered revenue "
                     "system looks like, element by element.", NAVY),
            ("ring", "Handoff score, on the line between two wedges, because "
                     "that is where the join is.", CORAL)):
        r = 8 * SCALE
        if mark == "fill":
            d.ellipse([40 * SCALE - r, ly - r, 40 * SCALE + r, ly + r],
                      fill=(36, 184, 203, 120), outline=colour, width=2 * SCALE)
        else:
            d.ellipse([40 * SCALE - r, ly - r, 40 * SCALE + r, ly + r],
                      outline=colour, width=2 * SCALE)
        d.text((62 * SCALE, ly - 9 * SCALE), text, font=f_small, fill=MUTED)
        ly += 22 * SCALE

    img = img.convert("RGB").resize((W // SCALE, H // SCALE), Image.LANCZOS)
    img.save(out_path, "PNG", optimize=True)
    return {"path": out_path, "font": used,
            "stages_plotted": len(poly), "stages": n,
            "defined_standard": DEFINED_STANDARD}
