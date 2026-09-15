#!/usr/bin/env python3
"""Build the architecture diagram as both PNG and SVG from one description.

The diagram answers a single question: what runs where. Keeping it as data
rather than two hand-drawn files means the PNG and the SVG cannot drift apart,
and moving a box is a coordinate change instead of a redraw.

    uv run python docs/build_diagram.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1600, 1170

# name -> (size, bold, colour, mono)
STYLES = {
    "title": (28, True, "#0f172a", False),
    "sub": (14, False, "#64748b", False),
    "band": (14, True, "#ffffff", False),
    "bandnote": (12, False, "#d1fae5", False),
    "t": (15, True, "#0f172a", False),
    "s": (13, False, "#475569", False),
    "m": (12, False, "#334155", True),
    "lbl": (11, False, "#64748b", False),
    "flag": (13, True, "#b45309", False),
    "muted": (13, False, "#94a3b8", False),
    "mutedt": (15, True, "#64748b", False),
}

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
]


def _font_path(bold: bool, mono: bool) -> str | None:
    names = []
    if mono:
        names = ["DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"]
    else:
        names = [
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
            "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf",
        ]
    for directory in FONT_DIRS:
        for name in names:
            candidate = Path(directory) / name
            if candidate.exists():
                return str(candidate)
    return None


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int
    fill: str
    stroke: str
    width: int = 2
    radius: int = 12
    dashed: bool = False


@dataclass
class Text:
    x: int
    y: int
    content: str
    style: str = "s"
    anchor: str = "lt"


@dataclass
class Arrow:
    points: list[tuple[int, int]]
    colour: str = "#94a3b8"
    width: int = 2
    both: bool = False
    dashed: bool = False


@dataclass
class Canvas:
    rects: list[Rect] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    arrows: list[Arrow] = field(default_factory=list)

    def rect(self, *args, **kwargs) -> None:
        self.rects.append(Rect(*args, **kwargs))

    def text(self, *args, **kwargs) -> None:
        self.texts.append(Text(*args, **kwargs))

    def arrow(self, *args, **kwargs) -> None:
        self.arrows.append(Arrow(*args, **kwargs))

    def card(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        title: str,
        lines: list[str],
        fill: str,
        stroke: str,
        *,
        mono: str | None = None,
        width: int = 2,
        dashed: bool = False,
        title_style: str = "t",
        line_style: str = "s",
    ) -> None:
        self.rect(x, y, w, h, fill, stroke, width=width, dashed=dashed)
        self.text(x + 16, y + 14, title, title_style)
        offset = y + 40
        for line in lines:
            self.text(x + 16, offset, line, line_style)
            offset += 19
        if mono:
            self.text(x + 16, y + h - 26, mono, "m")


def build() -> Canvas:
    c = Canvas()

    c.text(48, 26, "hemopt — vad körs var", "title")
    c.text(
        48,
        68,
        "Allt vårt körs på samma Raspberry Pi som Home Assistant. Ingen extra dator, "
        "ingen kod på ESP32 eller STM32.",
        "sub",
    )

    # ---------------------------------------------------------------- moln
    c.rect(40, 110, 1520, 112, "#f8fafc", "#cbd5e1", width=2, radius=14)
    c.rect(40, 110, 1520, 30, "#475569", "#475569", width=1, radius=14)
    c.text(60, 117, "MOLN", "band")
    c.text(1540, 117, "Läses över HTTPS · ingen egen kod", "bandnote", anchor="rt")

    c.card(
        320,
        152,
        420,
        58,
        "elprisetjustnu.se — Nordpool",
        ["SE1–SE4 · dygn / timme / kvart"],
        "#eff6ff",
        "#93c5fd",
    )
    c.card(
        770,
        152,
        380,
        58,
        "Väderprognos",
        ["Timtemperatur 36 h framåt"],
        "#f0fdf4",
        "#86efac",
    )

    # ------------------------------------------------------- raspberry pi
    c.rect(40, 262, 1520, 468, "#ffffff", "#0f766e", width=3, radius=16)
    c.rect(40, 262, 1520, 32, "#0f766e", "#0f766e", width=1, radius=16)
    c.text(60, 270, "RASPBERRY PI — HOME ASSISTANT OS", "band")
    c.text(1540, 270, "HÄR KÖRS ALLT VÅRT", "band", anchor="rt")

    # Home Assistant Core
    c.rect(70, 312, 468, 392, "#eff6ff", "#60a5fa", width=2, radius=14)
    c.text(86, 326, "Home Assistant Core", "t")
    c.text(86, 350, "Det du ser och rör", "s")

    c.card(
        86,
        376,
        436,
        76,
        "Lovelace-vyer",
        ["Kostnadsoptimering · Energi", "Summerad kostnad dag / månad"],
        "#ffffff",
        "#bfdbfe",
    )
    c.card(
        86,
        462,
        436,
        96,
        "Reglage",
        [
            "Slå på styrning · prioritet 1–5",
            "Regler för effekttoppar",
            "Välj elområde och elavtal",
        ],
        "#ffffff",
        "#bfdbfe",
    )
    c.card(
        86,
        568,
        436,
        62,
        "Enheter",
        ["Lägg till rumsgivare, elmätare"],
        "#ffffff",
        "#bfdbfe",
    )
    c.card(
        86,
        638,
        436,
        62,
        "Recorder",
        ["Historiken modellen tränar på"],
        "#ffffff",
        "#bfdbfe",
    )

    # Mosquitto
    c.rect(558, 312, 152, 392, "#f8fafc", "#cbd5e1", width=2, radius=14)
    c.text(574, 330, "Mosquitto", "t")
    c.text(574, 354, "MQTT-broker", "s")
    c.text(574, 373, "(HA add-on)", "s")
    c.text(574, 410, "Bär:", "s")
    c.text(574, 432, "· H66-data", "s")
    c.text(574, 451, "· hemopts", "s")
    c.text(574, 470, "  entiteter", "s")
    c.text(574, 489, "· discovery", "s")

    # hemopt add-on
    c.rect(730, 312, 800, 392, "#ecfdf5", "#10b981", width=3, radius=14)
    c.text(748, 328, "hemopt — Home Assistant Add-on", "t")
    c.text(748, 352, "Egen container, startas av HA Supervisor. Python + HiGHS.", "s")

    cells = [
        ("Samla + Lär", ["States och historik", "Tröghet per rum", "VV-vanor, baslast"]),
        ("Planerare", ["LP via HiGHS", "36 h i kvartssteg", "Löser på ~0,3 s"]),
        ("Effektvakt", ["Varje minut", "Mot klocktimmen", "EXT-block vid behov"]),
        ("Avtalssimulator", ["Dag / tim / kvart", "Kostnad för alla tre", "Ger rekommendation"]),
        ("SQLite", ["Modeller och toppar", "Mätserier", "Överlever omstart"]),
        ("Setup-UI", ["Via HA Ingress", "Välj entiteter och rum", "Elområde och avtal"]),
    ]
    for index, (title, lines) in enumerate(cells):
        col, row = index % 3, index // 3
        c.card(
            748 + col * 266,
            382 + row * 156,
            250,
            140,
            title,
            lines,
            "#ffffff",
            "#6ee7b7",
        )

    # --------------------------------------------------------------- huset
    c.rect(40, 770, 1520, 172, "#fffbeb", "#ea580c", width=2, radius=14)
    c.rect(40, 770, 1520, 30, "#ea580c", "#ea580c", width=1, radius=14)
    c.text(60, 777, "HUSETS NÄTVERK — GIVARE OCH FÄLTBUSS", "band")
    c.text(1540, 777, "Ingen egen kod · bara HA-integrationer", "bandnote", anchor="rt")

    c.card(
        70,
        816,
        350,
        110,
        "Husdata H66",
        ["CAN till MQTT-brygga", "Läser IVT Rego 1000", "EXT-portar = hård stopp"],
        "#ffffff",
        "#fdba74",
    )
    c.card(
        440,
        816,
        320,
        110,
        "LK Arc Hub",
        ["11 rumsgivare, två plan", "Börvärde per zon"],
        "#ffffff",
        "#fdba74",
    )
    c.card(
        780,
        816,
        360,
        110,
        "Elmätare (HAN / P1)",
        ["Hela husets effekt", "Krävs för effekttoppar"],
        "#fff7ed",
        "#f59e0b",
        width=3,
    )
    c.text(796, 898, "← koppla in denna", "flag")

    c.card(
        1160,
        816,
        370,
        110,
        "ESP32 / STM32",
        ["Behövs inte.", "H66 är redan bryggan.", "LP-lösaren ryms inte i en MCU."],
        "#f8fafc",
        "#cbd5e1",
        dashed=True,
        title_style="mutedt",
        line_style="muted",
    )

    # -------------------------------------------------------------- deploy
    c.rect(40, 982, 1520, 148, "#faf5ff", "#7c3aed", width=2, radius=14)
    c.rect(40, 982, 1520, 30, "#7c3aed", "#7c3aed", width=1, radius=14)
    c.text(60, 989, "DEPLOY — INGEN KLIPP-OCH-KLISTRA", "band")
    c.text(1540, 989, "Uppdateringar kommer som en knapp i HA", "bandnote", anchor="rt")

    steps = [
        ("1. Git-repo", ["Add-on repository"]),
        ("2. HA → Add-ons", ["Lägg till repository-URL"]),
        ("3. Installera", ["Ett klick, HA bygger"]),
        ("4. Uppdatera", ["Knapp när ny version finns"]),
    ]
    for index, (title, lines) in enumerate(steps):
        x = 70 + index * 370
        c.card(x, 1028, 330, 76, title, lines, "#ffffff", "#c4b5fd")
        if index < 3:
            c.arrow([(x + 336, 1066), (x + 364, 1066)], "#7c3aed", 2)

    c.text(
        48,
        1142,
        "Hushållsprofilen (rum, entiteter, elavtal, elområde) är data — inte kod. "
        "Samma add-on installeras oförändrad i nästa hushåll.",
        "sub",
    )

    # -------------------------------------------------------------- arrows
    c.arrow([(880, 222), (880, 262)], "#0f766e", 3)
    c.text(892, 228, "priser + väderprognos", "lbl")

    c.arrow([(300, 730), (300, 770)], "#0f766e", 3, both=True)
    c.text(312, 738, "MQTT", "lbl")

    c.arrow([(960, 730), (960, 770)], "#0f766e", 3, both=True)
    c.text(972, 738, "REST + MQTT", "lbl")

    return c


# --------------------------------------------------------------------- PNG


def render_png(canvas: Canvas, path: Path) -> None:
    image = Image.new("RGB", (W, H), "#f8fafc")
    draw = ImageDraw.Draw(image)
    cache: dict[tuple[int, bool, bool], ImageFont.FreeTypeFont] = {}

    def font_for(style: str):
        size, bold, _, mono = STYLES[style]
        key = (size, bold, mono)
        if key not in cache:
            path_ = _font_path(bold, mono)
            cache[key] = (
                ImageFont.truetype(path_, size) if path_ else ImageFont.load_default()
            )
        return cache[key]

    def dashed_round_rect(r: Rect) -> None:
        # PIL has no dash support, so the outline is stroked as short segments.
        draw.rounded_rectangle(
            (r.x, r.y, r.x + r.w, r.y + r.h), radius=r.radius, fill=r.fill
        )
        step, dash = 12, 7
        for x in range(r.x + r.radius, r.x + r.w - r.radius, step):
            draw.line((x, r.y, x + dash, r.y), fill=r.stroke, width=r.width)
            draw.line((x, r.y + r.h, x + dash, r.y + r.h), fill=r.stroke, width=r.width)
        for y in range(r.y + r.radius, r.y + r.h - r.radius, step):
            draw.line((r.x, y, r.x, y + dash), fill=r.stroke, width=r.width)
            draw.line((r.x + r.w, y, r.x + r.w, y + dash), fill=r.stroke, width=r.width)

    for r in canvas.rects:
        if r.dashed:
            dashed_round_rect(r)
        else:
            draw.rounded_rectangle(
                (r.x, r.y, r.x + r.w, r.y + r.h),
                radius=r.radius,
                fill=r.fill,
                outline=r.stroke,
                width=r.width,
            )

    for a in canvas.arrows:
        for start, end in zip(a.points, a.points[1:], strict=False):
            draw.line((*start, *end), fill=a.colour, width=a.width)

        def head(tip: tuple[int, int], prev: tuple[int, int]) -> None:
            dx, dy = tip[0] - prev[0], tip[1] - prev[1]
            if abs(dx) >= abs(dy):
                step = 9 if dx > 0 else -9
                draw.polygon(
                    [tip, (tip[0] - step, tip[1] - 5), (tip[0] - step, tip[1] + 5)],
                    fill=a.colour,
                )
            else:
                step = 9 if dy > 0 else -9
                draw.polygon(
                    [tip, (tip[0] - 5, tip[1] - step), (tip[0] + 5, tip[1] - step)],
                    fill=a.colour,
                )

        head(a.points[-1], a.points[-2])
        if a.both:
            head(a.points[0], a.points[1])

    for t in canvas.texts:
        _, _, colour, _ = STYLES[t.style]
        anchor = "rt" if t.anchor == "rt" else "lt"
        draw.text((t.x, t.y), t.content, fill=colour, font=font_for(t.style), anchor=anchor)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)


# --------------------------------------------------------------------- SVG


def render_svg(canvas: Canvas, path: Path) -> None:
    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        'role="img" aria-label="hemopt - vad kors var">',
        "<defs>",
    ]

    colours = sorted({a.colour for a in canvas.arrows})
    for index, colour in enumerate(colours):
        out.append(
            f'<marker id="m{index}" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M0 0 L10 5 L0 10 z" fill="{colour}"/></marker>'
        )
    out.append("</defs>")
    out.append(f'<rect width="{W}" height="{H}" fill="#f8fafc"/>')

    for r in canvas.rects:
        dash = ' stroke-dasharray="7 5"' if r.dashed else ""
        out.append(
            f'<rect x="{r.x}" y="{r.y}" width="{r.w}" height="{r.h}" rx="{r.radius}" '
            f'fill="{r.fill}" stroke="{r.stroke}" stroke-width="{r.width}"{dash}/>'
        )

    for a in canvas.arrows:
        index = colours.index(a.colour)
        points = " ".join(f"{x},{y}" for x, y in a.points)
        start = f' marker-start="url(#m{index})"' if a.both else ""
        out.append(
            f'<polyline points="{points}" fill="none" stroke="{a.colour}" '
            f'stroke-width="{a.width}" marker-end="url(#m{index})"{start}/>'
        )

    for t in canvas.texts:
        size, bold, colour, mono = STYLES[t.style]
        family = "DejaVu Sans Mono, monospace" if mono else "DejaVu Sans, sans-serif"
        weight = "700" if bold else "400"
        anchor = ' text-anchor="end"' if t.anchor == "rt" else ""
        # PIL anchors text from the top; SVG from the baseline.
        out.append(
            f'<text x="{t.x}" y="{t.y + round(size * 0.80)}" fill="{colour}" '
            f'font-family="{family}" font-size="{size}" font-weight="{weight}"'
            f"{anchor}>{escape(t.content)}</text>"
        )

    out.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


def main() -> None:
    canvas = build()
    here = Path(__file__).resolve().parent
    render_png(canvas, here / "system-overview.png")
    render_svg(canvas, here / "system-overview.svg")
    print("wrote system-overview.png and system-overview.svg")


if __name__ == "__main__":
    main()
