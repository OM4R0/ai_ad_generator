"""Tests for core.scene_generator (ADR-007): AI background with a guaranteed fallback."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
from PIL import Image

from core.scene_generator import SceneGenerator, gradient_background


def _jpeg(size=(512, 512), color="#336699") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


class Server:
    def __init__(self, *responses):
        self.responses, self.urls = list(responses), []

    def __call__(self, request):
        self.urls.append(str(request.url))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _gen(server, tmp_path: Path, provider="pollinations") -> SceneGenerator:
    return SceneGenerator(provider=provider, cache_dir=tmp_path,
                          transport=httpx.MockTransport(server))


def _ok() -> httpx.Response:
    return httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})


def test_ai_scene_is_returned_at_the_requested_size(tmp_path):
    server = Server(_ok())
    result = _gen(server, tmp_path).generate("dark marble", (1080, 1920), ["#111111", "#D4AF37"])
    assert result.source == "pollinations" and result.note is None
    assert result.image.size == (1080, 1920) and result.image.mode == "RGB"
    url = server.urls[0]
    assert "width=1080" in url and "height=2112" in url and "nologo=true" in url  # +10% for the watermark crop
    assert "dark%20marble" in url


def test_prompt_forbids_products_text_and_people(tmp_path):
    server = Server(_ok())
    _gen(server, tmp_path).generate("studio", (1080, 1080), ["#111111"])
    assert "no%20text" in server.urls[0] and "no%20people" in server.urls[0]


def test_failure_falls_back_to_a_brand_gradient(tmp_path):
    server = Server(httpx.Response(500), httpx.Response(500))
    result = _gen(server, tmp_path).generate("x", (1080, 1080), ["#0B1F3A", "#00C2D1"])
    assert result.source == "gradient" and "500" in result.note
    assert result.image.size == (1080, 1080)


def test_network_error_falls_back_to_a_gradient(tmp_path):
    server = Server(httpx.ConnectError("down"), httpx.ConnectError("down"))
    assert _gen(server, tmp_path).generate("x", (100, 100), ["#000000"]).source == "gradient"


def test_a_non_image_reply_is_rejected(tmp_path):
    server = Server(httpx.Response(200, text="<html>rate limited</html>",
                                   headers={"content-type": "text/html"}),
                    httpx.Response(200, text="<html>", headers={"content-type": "text/html"}))
    assert _gen(server, tmp_path).generate("x", (100, 100), ["#000000"]).source == "gradient"


def test_cache_avoids_a_second_request(tmp_path):
    server = Server(_ok())
    gen = _gen(server, tmp_path)
    gen.generate("same prompt", (1080, 1080), ["#111111"])
    second = gen.generate("same prompt", (1080, 1080), ["#111111"])
    assert second.source == "cache" and len(server.urls) == 1


def test_different_seed_is_a_different_scene(tmp_path):
    server = Server(_ok(), _ok())
    gen = _gen(server, tmp_path)
    gen.generate("p", (1080, 1080), ["#111111"], seed=1)
    gen.generate("p", (1080, 1080), ["#111111"], seed=2)
    assert len(server.urls) == 2


def test_provider_none_never_calls_the_network(tmp_path):
    server = Server()
    result = _gen(server, tmp_path, provider="none").generate("x", (200, 200), ["#123456"])
    assert result.source == "gradient" and server.urls == []


def test_gradient_uses_the_brand_colours():
    img = gradient_background((200, 200), ["#000080", "#FFD700"])
    top, bottom = img.getpixel((100, 2)), img.getpixel((100, 197))
    assert top[2] > top[0]          # navy-ish at the top
    assert bottom[0] > bottom[2]    # gold-ish at the bottom


# --- Found in the first live render (2026-11-05) --------------------------- #

def test_pollinations_watermark_strip_is_cropped_away(tmp_path):
    # Live: the free tier stamps "pollinations.ai" in the bottom-right corner.
    img = Image.new("RGB", (1080, 1188), "#336699")
    img.paste(Image.new("RGB", (1080, 90), "#FF0000"), (0, 1188 - 90))   # fake watermark band
    buffer = io.BytesIO(); img.save(buffer, format="PNG")
    server = Server(httpx.Response(200, content=buffer.getvalue(),
                                   headers={"content-type": "image/png"}))
    result = _gen(server, tmp_path).generate("x", (1080, 1080), ["#000000"])
    assert "height=1188" in server.urls[0]                      # asked for a taller image
    assert result.image.size == (1080, 1080)
    r, g, b = result.image.getpixel((1000, 1075))
    assert r < 120 and b > 120                                  # the red band is gone


def test_gradient_has_a_soft_glow_behind_the_product():
    # Same row: a plain vertical gradient would make these equal.
    img = gradient_background((400, 400), ["#101010", "#202020"])
    center = sum(img.getpixel((200, 160)))
    side = sum(img.getpixel((5, 160)))
    assert center > side + 15
