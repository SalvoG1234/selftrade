"""Visual content: real PNG cards and Reel storyboards, with no image library.

Instagram publishing needs an image *file*, so "generate a card" has to mean a
real PNG - not a placeholder.  Rather than add Pillow to a project whose whole
dependency list is numpy/pandas/requests/PyYAML, this module writes PNG itself:
a 5x7 bitmap font, a few rectangles, and zlib's DEFLATE (both in the standard
library) produce a 1080x1080 card that a phone renders correctly.

That matters for honesty as well as weight: the picture the operator sees in the
Content Studio is the exact file that would be uploaded, so a claim in a caption
and a claim on the card cannot drift apart.

``storyboard`` produces the Reel plan as SVG frames - text, timings and the
shot list - because a Reel is a video and this project will not fake one: it
writes the script and the frames, and a human (or a later phase) assembles it.
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import obs

# --------------------------------------------------------------------- palette
BG = (9, 13, 20)
PANEL = (15, 23, 36)
TEXT = (226, 233, 243)
DIM = (154, 168, 189)
ACCENT = (34, 211, 238)          # cyan, the dashboard accent
WARN = (251, 191, 36)

#: 5x7 bitmap font, rows separated by "/".  Only what a card needs.
FONT: dict[str, str] = {
    "A": "01110/10001/10001/11111/10001/10001/10001",
    "B": "11110/10001/11110/10001/10001/10001/11110",
    "C": "01110/10001/10000/10000/10000/10001/01110",
    "D": "11110/10001/10001/10001/10001/10001/11110",
    "E": "11111/10000/11110/10000/10000/10000/11111",
    "F": "11111/10000/11110/10000/10000/10000/10000",
    "G": "01110/10001/10000/10111/10001/10001/01111",
    "H": "10001/10001/11111/10001/10001/10001/10001",
    "I": "11111/00100/00100/00100/00100/00100/11111",
    "J": "00111/00010/00010/00010/00010/10010/01100",
    "K": "10001/10010/10100/11000/10100/10010/10001",
    "L": "10000/10000/10000/10000/10000/10000/11111",
    "M": "10001/11011/10101/10101/10001/10001/10001",
    "N": "10001/11001/10101/10011/10001/10001/10001",
    "O": "01110/10001/10001/10001/10001/10001/01110",
    "P": "11110/10001/10001/11110/10000/10000/10000",
    "Q": "01110/10001/10001/10001/10101/10010/01101",
    "R": "11110/10001/10001/11110/10100/10010/10001",
    "S": "01111/10000/10000/01110/00001/00001/11110",
    "T": "11111/00100/00100/00100/00100/00100/00100",
    "U": "10001/10001/10001/10001/10001/10001/01110",
    "V": "10001/10001/10001/10001/10001/01010/00100",
    "W": "10001/10001/10001/10101/10101/11011/10001",
    "X": "10001/10001/01010/00100/01010/10001/10001",
    "Y": "10001/10001/01010/00100/00100/00100/00100",
    "Z": "11111/00001/00010/00100/01000/10000/11111",
    "0": "01110/10001/10011/10101/11001/10001/01110",
    "1": "00100/01100/00100/00100/00100/00100/01110",
    "2": "01110/10001/00001/00110/01000/10000/11111",
    "3": "11110/00001/00001/01110/00001/00001/11110",
    "4": "00010/00110/01010/10010/11111/00010/00010",
    "5": "11111/10000/11110/00001/00001/10001/01110",
    "6": "00110/01000/10000/11110/10001/10001/01110",
    "7": "11111/00001/00010/00100/01000/01000/01000",
    "8": "01110/10001/10001/01110/10001/10001/01110",
    "9": "01110/10001/10001/01111/00001/00010/01100",
    " ": "00000/00000/00000/00000/00000/00000/00000",
    ".": "00000/00000/00000/00000/00000/01100/01100",
    ",": "00000/00000/00000/00000/01100/01100/11000",
    ":": "00000/01100/01100/00000/01100/01100/00000",
    "-": "00000/00000/00000/11111/00000/00000/00000",
    "_": "00000/00000/00000/00000/00000/00000/11111",
    "/": "00001/00010/00010/00100/01000/01000/10000",
    "#": "01010/01010/11111/01010/11111/01010/01010",
    "%": "11001/11010/00010/00100/01000/01011/10011",
    "(": "00010/00100/01000/01000/01000/00100/00010",
    ")": "01000/00100/00010/00010/00010/00100/01000",
    "!": "00100/00100/00100/00100/00100/00000/00100",
    "?": "01110/10001/00001/00110/00100/00000/00100",
    "&": "01100/10010/10100/01000/10101/10010/01101",
    "+": "00000/00100/00100/11111/00100/00100/00000",
    "=": "00000/00000/11111/00000/11111/00000/00000",
    ">": "01000/00100/00010/00001/00010/00100/01000",
    "<": "00010/00100/01000/10000/01000/00100/00010",
    "'": "00100/00100/00000/00000/00000/00000/00000",
    "\"": "01010/01010/00000/00000/00000/00000/00000",
    "*": "00000/10101/01110/11111/01110/10101/00000",
    "@": "01110/10001/10111/10101/10111/10000/01110",
    "€": "00111/01000/11110/01000/11110/01000/00111",
    "°": "01100/10010/01100/00000/00000/00000/00000",
    "·": "00000/00000/01100/01100/00000/00000/00000",
    "—": "00000/00000/00000/11111/00000/00000/00000",
    "–": "00000/00000/00000/11111/00000/00000/00000",
    "’": "00100/00100/00000/00000/00000/00000/00000",
    "“": "01010/01010/00000/00000/00000/00000/00000",
    "”": "01010/01010/00000/00000/00000/00000/00000",
    "à": "00100/00000/01110/00001/01111/10001/01111",
    "è": "00100/00000/11111/10000/11110/10000/11111",
    "é": "00010/00000/11111/10000/11110/10000/11111",
    "ì": "00100/00000/01100/00100/00100/00100/01110",
    "ò": "00100/00000/01110/10001/10001/10001/01110",
    "ù": "00100/00000/10001/10001/10001/10011/01101",
}
FALLBACK = "?"
GLYPH_W, GLYPH_H = 5, 7
GAP = 1


@dataclass
class Canvas:
    width: int
    height: int
    pixels: list[bytearray] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.pixels:
            row = bytearray()
            for _ in range(self.width):
                row.extend(BG)
            self.pixels = [bytearray(row) for _ in range(self.height)]

    def rect(self, x: int, y: int, w: int, h: int, colour: tuple[int, int, int]) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        span = bytearray(colour * max(0, x1 - x0))
        for row_index in range(y0, y1):
            self.pixels[row_index][x0 * 3:x1 * 3] = span

    def text(self, x: int, y: int, message: str, *, scale: int = 2,
             colour: tuple[int, int, int] = TEXT, tracking: int = 1,
             max_width: int | None = None) -> int:
        """Draw uppercase text; returns the y of the next line."""
        cursor_x, cursor_y = x, y
        limit = max_width or (self.width - x - 20)
        step = (GLYPH_W + tracking) * scale
        for char in message.upper():
            glyph = FONT.get(char) or FONT.get(FALLBACK, "00000/00000/00000/00000/00000/00000/00000")
            if cursor_x + GLYPH_W * scale > x + limit:
                cursor_x = x
                cursor_y += (GLYPH_H + 3) * scale
            for row_index, bits in enumerate(glyph.split("/")):
                for col_index, bit in enumerate(bits):
                    if bit != "1":
                        continue
                    self.rect(cursor_x + col_index * scale,
                              cursor_y + row_index * scale, scale, scale, colour)
            cursor_x += step
        return cursor_y + (GLYPH_H + 3) * scale

    def to_png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(row) for row in self.pixels)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b"")
        )


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def wrap(text: str, limit: int) -> list[str]:
    """Wrap on words; long words are cut rather than allowed to overflow."""
    words, lines, current = str(text or "").split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word[:limit]
    if current:
        lines.append(current)
    return lines or [""]


def card_png(
    *,
    title: str,
    subtitle: str = "",
    bullets: Iterable[str] = (),
    footer: str = "SelfTrade · github.com/SalvoG1234/selftrade",  # · is in FONT

    width: int = 1080,
    height: int = 1080,
    accent: tuple[int, int, int] = ACCENT,
    badge: str = "",
) -> bytes:
    """One square card: dark panel, cyan rule, title, up to five bullets."""
    canvas = Canvas(width, height)
    margin = int(width * 0.075)
    canvas.rect(0, 0, width, 10, accent)
    canvas.rect(margin, margin, width - 2 * margin, height - 2 * margin - 40, PANEL)
    canvas.rect(margin, margin, 8, height - 2 * margin - 40, accent)

    y = margin + 60
    if badge:
        canvas.rect(margin + 40, y - 10, 22 * len(badge) + 40, 70, accent)
        canvas.text(margin + 60, y + 14, badge[:24], scale=3, colour=BG)
        y += 120
    else:
        y += 20
    for line in wrap(title, 26)[:4]:
        y = canvas.text(margin + 60, y, line, scale=6, colour=TEXT) + 18
    if subtitle:
        y += 10
        for line in wrap(subtitle, 46)[:3]:
            y = canvas.text(margin + 60, y, line, scale=3, colour=DIM) + 10
    if bullets:
        y += 20
        canvas.rect(margin + 60, y, width - 2 * margin - 140, 3, accent)
        y += 40
        for bullet in list(bullets)[:5]:
            for index, line in enumerate(wrap(bullet, 40)[:2]):
                marker = "›" if index == 0 else " "
                y = canvas.text(margin + 60, y, f"{marker} {line}", scale=3, colour=TEXT) + 12
            y += 12
    canvas.text(margin + 60, height - margin - 90, footer, scale=3, colour=DIM)
    return canvas.to_png()


@dataclass
class VisualPlan:
    """What was generated, where it lives, and what is still missing."""

    files: list[str] = field(default_factory=list)
    storyboard: str = ""
    notes: list[str] = field(default_factory=list)
    dimensions: str = "1080x1080"

    def to_dict(self) -> dict[str, Any]:
        return {"files": list(self.files), "storyboard": self.storyboard,
                "notes": list(self.notes), "dimensions": self.dimensions}


class VisualGenerator:
    """Writes the images a draft refers to.  Deterministic: same facts, same card."""

    def __init__(self, settings: Any, db: Any = None, output_dir: str | Path | None = None):
        self.settings = settings
        self.db = db
        base = output_dir or getattr(settings.instagram, "media_dir", "state/media")
        self.output_dir = Path(base)

    def safe_name(self, text: str) -> str:
        return (re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "card")

    def generate(self, brief: Any, ctx: dict[str, Any] | None = None) -> list[str]:
        """The callable the content engine passes in for ``media``."""
        ctx = ctx or {}
        return self.generate_for_brief(brief, ctx).files

    def generate_for_brief(self, brief: Any, ctx: dict[str, Any] | None = None) -> VisualPlan:
        ctx = ctx or {}
        plan = VisualPlan()
        if self.db is not None:
            self.db.audit(
                "visuals.generate", actor="visual-generator", subject=str(getattr(brief, "key", "")),
                detail={"category": str(getattr(brief, "category", "")),
                        "targets": list(getattr(brief, "platforms", []))},
            )
        brand = str(ctx.get("brand") or self.settings.project.brand)
        repo = str(ctx.get("repo_url") or self.settings.project.repo_url)
        bullets = [str(b) for b in (ctx.get("bullets") or ctx.get("bullets_it") or [])][:5]
        category = str(getattr(brief, "category", ""))
        key = self.safe_name(f"{category}-{getattr(brief, 'key', 'item')}")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        title_card = self.output_dir / f"{key}-01.png"
        title_card.write_bytes(card_png(
            title=str(getattr(brief, "title", "")),
            subtitle=str(getattr(brief, "summary", ""))[:180],
            bullets=[],
            badge=_badge_for(category),
            footer=f"{brand} · {repo}",
        ))
        plan.files.append(str(title_card))

        remaining = bullets[:4]
        for index in range(0, max(0, len(remaining)), 2):
            chunk = remaining[index:index + 2]
            path = self.output_dir / f"{key}-{2 + index // 2:02d}.png"
            path.write_bytes(card_png(
                title=str(getattr(brief, "title", ""))[:40],
                subtitle="",
                bullets=chunk,
                footer=f"{brand} · {repo}",
            ))
            plan.files.append(str(path))
        if not remaining:
            plan.notes.append(
                "one card only: the brief has no bullet facts, so there is no carousel to build"
            )
        plan.storyboard = self.storyboard(brief, ctx)
        return plan

    def storyboard(self, brief: Any, ctx: dict[str, Any] | None = None,
                   *, seconds_per_scene: int = 4) -> str:
        """A Reel *script*: scenes, on-screen text, voice-over, timings - as SVG."""
        ctx = ctx or {}
        brand = str(ctx.get("brand") or self.settings.project.brand)
        repo = str(ctx.get("repo_url") or self.settings.project.repo_url)
        bullets = [str(b) for b in (ctx.get("bullets") or [])][:4]
        scenes: list[dict[str, Any]] = [
            {"text": str(getattr(brief, "title", brand))[:70],
             "voice": "Hook: name the concrete change, no adjectives."},
        ]
        for bullet in bullets:
            scenes.append({
                "text": bullet[:70],
                "voice": "Say what it is and why it was needed. One sentence.",
            })
        scenes.append({
            "text": f"{brand} · {repo}"[:70],
            "voice": "Where to find it, and the honest limit to ask about.",
        })
        total = len(scenes) * seconds_per_scene
        height = 420 * len(scenes) + 120
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="{height}" '
            f'viewBox="0 0 1080 {height}" font-family="Segoe UI, system-ui, sans-serif">',
            f'<rect width="1080" height="{height}" fill="#090d14"/>',
            f'<text x="60" y="70" fill="#22d3ee" font-size="34" font-weight="700">'
            f'{_escape(brand)} — Reel storyboard ({total}s)</text>',
        ]
        for index, scene in enumerate(scenes):
            top = 120 + index * 420
            parts += [
                f'<rect x="60" y="{top}" width="960" height="380" rx="18" fill="#0f1724" '
                f'stroke="#1b2637"/>',
                f'<text x="100" y="{top + 60}" fill="#6b7a91" font-size="24">'
                f'SCENA {index + 1} · {seconds_per_scene}s</text>',
                f'<text x="100" y="{top + 150}" fill="#e2e9f3" font-size="46" font-weight="700">'
                f'{_escape(scene["text"])}</text>',
                f'<text x="100" y="{top + 250}" fill="#9aa8bd" font-size="28">'
                f'{_escape(scene["voice"])}</text>',
                f'<text x="100" y="{top + 330}" fill="#22d3ee" font-size="24">'
                f'grid 1080x1920 · safe area 250px</text>',
            ]
        parts.append("</svg>")
        svg = "\n".join(parts)
        path = self.output_dir / f"{self.safe_name(str(getattr(brief, 'key', 'reel')))}-storyboard.svg"
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(svg, encoding="utf-8")
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            obs.warning("visuals.storyboard_failed", detail={"error": type(exc).__name__})
            return ""
        return str(path)


def _badge_for(category: str) -> str:
    return {
        "release": "RELEASE",
        "dev_update": "UPDATE",
        "build_in_public": "BUILD",
        "tech_explainer": "HOW",
        "experiment": "EXPERIMENT",
        "limits": "LIMITS",
        "dashboard_demo": "DASHBOARD",
        "feedback_request": "FEEDBACK",
        "tutorial": "GUIDE",
        "contribute": "CONTRIBUTE",
    }.get(str(category), "SELFTRADE")


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def write_contact_sheet(paths: list[str], target: str | Path) -> str:
    """An SVG index of the generated cards, for the Storyboard page."""
    rows = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1080" '
            f'height="{120 + 60 * max(1, len(paths))}" font-family="Segoe UI, sans-serif">',
            '<rect width="1080" height="100%" fill="#090d14"/>',
            '<text x="40" y="60" fill="#22d3ee" font-size="32" font-weight="700">'
            'Asset generati</text>']
    for index, item in enumerate(paths):
        rows.append(
            f'<text x="40" y="{110 + index * 60}" fill="#e2e9f3" font-size="26">'
            f'{_escape(Path(item).name)}</text>'
        )
    rows.append("</svg>")
    out = Path(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(rows), encoding="utf-8")
    return str(out)


def manifest(paths: list[str]) -> str:
    return json.dumps({"assets": list(paths)}, ensure_ascii=False, indent=2)
