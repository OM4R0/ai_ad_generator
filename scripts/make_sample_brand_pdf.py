"""Build a small brand-guidelines PDF for demos and tests (no extra dependencies).

Usage:
    python -m scripts.make_sample_brand_pdf            # writes data/sample_brand_guidelines.pdf

The PDF is written by hand in raw PDF syntax (one page, Helvetica), which is
enough for `pypdf` to extract the text. Latin text only.
"""

from __future__ import annotations

from pathlib import Path

SAMPLE_LINES: list[str] = [
    "AeroSonic Audio - Brand Guidelines 2026",
    "",
    "1. Brand voice",
    "Confident, precise and quietly futuristic. We speak like an engineer",
    "who loves music: concrete numbers first, hype never.",
    "",
    "2. Colour palette",
    "Primary: Midnight Navy #0B1F3A",
    "Accent: Signal Cyan #00C2D1",
    "Text on dark: Soft White #F4F6F8",
    "",
    "3. Audience",
    "Remote professionals and frequent flyers aged 25-45 in the US and EU.",
    "",
    "4. Language",
    "All consumer advertising is written in English.",
]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(lines: list[str]) -> bytes:
    """Return the bytes of a one-page PDF containing `lines` (Latin-1 text)."""
    ops = ["BT", "/F1 12 Tf", "16 TL", "56 780 Td"]
    for line in lines:
        ops.append(f"({_escape(line)}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref_at,
    )
    return bytes(out)


def main() -> None:
    target = Path(__file__).resolve().parent.parent / "data" / "sample_brand_guidelines.pdf"
    target.write_bytes(build_pdf(SAMPLE_LINES))
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
