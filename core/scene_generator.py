"""Scene backgrounds (ADR-001, ADR-007): an AI-painted scene, never the product.

Primary provider: Pollinations (FLUX over a plain GET, no key). The prompt
comes from the vision analysis (`scene_prompt`) plus fixed guard rails: no
product, no text, no people. Results are cached on disk, so regenerating the
copy doesn't repaint the backgrounds.

Guaranteed fallback: a vertical gradient in the brand colours, drawn locally.
The pipeline never depends on the image service being up.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/"
SCENE_GUARD_RAILS = (
    ", empty background scene for a product advertisement, no product, no text, "
    "no people, no hands, no logos, no watermark, photorealistic, soft light, high detail"
)
ATTEMPTS = 2
# The free tier stamps a watermark at the bottom: ask for a taller image, keep the top.
WATERMARK_CROP = 0.10
TIMEOUT_S = 90.0


@dataclass
class SceneResult:
    image: Image.Image
    source: str                 # "pollinations" | "cache" | "gradient"
    note: str | None = None     # why the gradient was used, if it was


class SceneGenerator:
    def __init__(
        self,
        provider: str = "pollinations",
        *,
        cache_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self.provider = provider
        self._cache_dir = cache_dir / "scenes" if cache_dir else None
        self._transport = transport
        self._timeout = timeout

    def generate(
        self, prompt: str, size: tuple[int, int], palette: list[str], seed: int = 0
    ) -> SceneResult:
        """An AI scene of exactly `size`, or the brand gradient if that isn't possible."""
        if self.provider == "none":
            return SceneResult(gradient_background(size, palette), "gradient")

        cache_file = self._cache_file(prompt, size, seed)
        if cache_file and cache_file.exists():
            with Image.open(cache_file) as cached:
                return SceneResult(cached.convert("RGB"), "cache")

        try:
            image = self._fetch(prompt, size, seed)
        except _SceneError as exc:
            logger.warning("Scene fallback (gradient): %s", exc)
            return SceneResult(gradient_background(size, palette), "gradient",
                               note=f"scene: gradient ({exc})")
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            image.save(cache_file, format="JPEG", quality=92)
        return SceneResult(image, self.provider)

    # ----------------------------------------------------------------- private

    def _fetch(self, prompt: str, size: tuple[int, int], seed: int) -> Image.Image:
        if self.provider != "pollinations":
            raise _SceneError(f"unknown image provider {self.provider!r}")
        url = POLLINATIONS_URL + quote(prompt.strip() + SCENE_GUARD_RAILS, safe="")
        tall = (size[0], round(size[1] * (1 + WATERMARK_CROP)))
        params = {"width": tall[0], "height": tall[1], "seed": seed,
                  "nologo": "true", "model": "flux"}
        last = "no attempt"
        with httpx.Client(timeout=self._timeout, transport=self._transport,
                          follow_redirects=True) as client:
            for _ in range(ATTEMPTS):
                try:
                    response = client.get(url, params=params)
                except httpx.HTTPError as exc:
                    last = f"network error: {exc!r}"
                    continue
                if response.status_code != 200:
                    last = f"HTTP {response.status_code}"
                    continue
                if not response.headers.get("content-type", "").startswith("image/"):
                    last = f"not an image ({response.headers.get('content-type', '?')})"
                    continue
                try:
                    with Image.open(io.BytesIO(response.content)) as img:
                        # Providers may round the size: scale to the tall format, then
                        # drop the bottom strip (where the watermark is).
                        fitted = ImageOps.fit(img.convert("RGB"), tall, Image.Resampling.LANCZOS)
                        return fitted.crop((0, 0, size[0], size[1]))
                except (UnidentifiedImageError, OSError) as exc:
                    last = f"undecodable image: {exc}"
        raise _SceneError(last)

    def _cache_file(self, prompt: str, size: tuple[int, int], seed: int) -> Path | None:
        if self._cache_dir is None:
            return None
        key = hashlib.sha256(f"{self.provider}|{prompt}|{size}|{seed}".encode()).hexdigest()[:24]
        return self._cache_dir / f"{key}.jpg"


class _SceneError(Exception):
    pass


def gradient_background(size: tuple[int, int], palette: list[str]) -> Image.Image:
    """Vertical gradient: first brand colour at the top, second at the bottom."""
    top = palette[0] if palette else "#111827"
    bottom = palette[1] if len(palette) > 1 else _shade(top, 0.55)
    mask = Image.linear_gradient("L").resize(size)  # black (top) → white (bottom)
    base = Image.composite(Image.new("RGB", size, bottom), Image.new("RGB", size, top), mask)
    return _add_glow(base, _mix(top, "#FFFFFF", 0.28))


def _add_glow(image: Image.Image, color: str) -> Image.Image:
    """A soft light spot where the product sits, so a plain gradient looks like a studio."""
    w, h = image.size
    r = int(min(w, h) * 0.40)
    cx, cy = w // 2, int(h * 0.38)
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).ellipse((cx - r, cy - r, cx + r, cy + r), fill=150)
    mask = mask.filter(ImageFilter.GaussianBlur(r * 0.45))
    return Image.composite(Image.new("RGB", image.size, color), image, mask)


def _mix(a: str, b: str, t: float) -> str:
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#{:02X}{:02X}{:02X}".format(*(round(x + (y - x) * t) for x, y in zip(ca, cb)))


def _shade(hex_color: str, factor: float) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02X}{:02X}{:02X}".format(*(int(c * factor) for c in (r, g, b)))
