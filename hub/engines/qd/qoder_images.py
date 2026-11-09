# -*- coding: utf-8 -*-
"""Bounded, deterministic inline-image normalization; never fetches remote URLs.

Pillow is optional for native installations. Missing codecs, unsupported formats,
animations and decode failures retain the original image; transparency stays lossless. No image
is removed, no network request or file write is performed by this module.
"""
import base64
import hashlib
import io
import os
import threading
import warnings
from collections import OrderedDict

_LOCK = threading.Lock()
_CACHE = OrderedDict()
_CACHE_BYTES = 0
_MAX_CACHE_BYTES = 32 * 1024 * 1024
_MAX_CACHE_ITEMS = 32
_MAX_DECODE_BYTES = 16 * 1024 * 1024
_MAX_PIXELS = 32 * 1024 * 1024


def _codec():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def enabled():
    return str(os.environ.get("QD_IMAGE_TRANSCODE", "auto")).strip().lower() not in (
        "0", "off", "false", "no", "disabled")


def status():
    """Content-free operational snapshot, suitable for the public health endpoint."""
    with _LOCK:
        size, count = _CACHE_BYTES, len(_CACHE)
    return {"enabled": enabled(), "codec_available": _codec() is not None,
            "cache_entries": count, "cache_bytes": size}


def _put(key, value):
    global _CACHE_BYTES
    weight = len(value[0])
    if weight > _MAX_CACHE_BYTES:
        return
    with _LOCK:
        previous = _CACHE.pop(key, None)
        if previous:
            _CACHE_BYTES -= len(previous[0])
        _CACHE[key] = value
        _CACHE_BYTES += weight
        while _CACHE and (len(_CACHE) > _MAX_CACHE_ITEMS
                          or _CACHE_BYTES > _MAX_CACHE_BYTES):
            _, old = _CACHE.popitem(last=False)
            _CACHE_BYTES -= len(old[0])


def normalize_url(url):
    """Return (URL, state). No resizing, no removal, stable for equal bytes.

    For opaque PNG/BMP/TIFF >=256 KiB choose optimized PNG or quality-95 JPEG
    only when the result saves at least 20%. Alpha images stay lossless PNG.
    JPEG, remote references, animations and unknown types pass through unchanged.
    """
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return url, "reference"
    if not enabled():
        return url, "disabled"
    header, sep, encoded = url.partition(",")
    mime = header[5:].split(";", 1)[0].lower()
    if not sep or not header.lower().endswith(";base64"):
        return url, "unsupported"
    if mime not in ("image/png", "image/bmp", "image/tiff", "image/x-ms-bmp"):
        return url, "unchanged"
    if len(encoded) > (_MAX_DECODE_BYTES * 4 // 3 + 4):
        return url, "too_large"
    if len(encoded) < 256 * 1024 * 4 // 3:
        return url, "small"
    key = hashlib.sha256(url.encode("utf-8")).digest()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit:
            _CACHE.move_to_end(key)
            return hit[0], "cache_hit"
    Image = _codec()
    if Image is None:
        return url, "codec_missing"
    try:
        raw = base64.b64decode(encoded, validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if (image.width * image.height > _MAX_PIXELS
                        or image.width <= 0 or image.height <= 0):
                    return url, "too_many_pixels"
                if getattr(image, "n_frames", 1) != 1:
                    return url, "animated"
                # Fully decode before saving. Keep pixel dimensions and alpha.
                image.load()
                alpha = "A" in image.getbands() or "transparency" in image.info
                if alpha:
                    alpha = image.convert("RGBA").getchannel("A").getextrema() != (255, 255)
                # Orientation metadata must not be discarded without transforming pixels.
                if image.getexif().get(274, 1) != 1:
                    return url, "orientation"
                # Nontrivial colour profiles/high bit depth need colour-managed
                # conversion. Do not silently reinterpret or quantize those inputs.
                if image.info.get("icc_profile") or image.mode not in (
                        "RGB", "RGBA", "L", "LA", "P", "1"):
                    return url, "colour_profile"
                if (image.getexif() or getattr(image, "text", None)
                        or any(k in image.info for k in ("gamma", "srgb", "chromaticity", "dpi", "aspect"))):
                    # Colour/rendering metadata and provenance must not silently vanish.
                    return url, "metadata"
                converted = image.convert("RGBA" if alpha else "RGB")
                candidates = [(raw, mime)]
                if not alpha:
                    jpg = io.BytesIO()
                    converted.save(jpg, format="JPEG", quality=95, subsampling=0,
                                   optimize=True, progressive=False)
                    candidates.append((jpg.getvalue(), "image/jpeg"))
                # Opaque screenshots usually benefit from JPEG immediately; avoid
                # spending several seconds recompressing a PNG we will not send.
                if alpha or min(len(x[0]) for x in candidates) > len(raw) * 0.8:
                    png = io.BytesIO()
                    converted.save(png, format="PNG", optimize=True)
                    candidates.append((png.getvalue(), "image/png"))
                data, out_mime = min(candidates, key=lambda item: len(item[0]))
                if len(data) > len(raw) * 0.8:
                    result = (url, "unchanged")
                else:
                    result = ("data:%s;base64,%s" % (
                        out_mime, base64.b64encode(data).decode("ascii")), "optimized")
    except Exception:
        # Invalid/unsupported media is not silently deleted or replaced by text.
        return url, "decode_failed"
    _put(key, result)
    return result


def prepare_messages(messages):
    """Copy only changed message/part objects; preserve order and all metadata."""
    output = []
    seen = {}
    stats = {"images": 0, "optimized": 0, "bytes_before": 0,
             "bytes_after": 0, "codec_missing": 0}
    for message in messages:
        if not isinstance(message, dict):
            output.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            output.append(message)
            continue
        parts = []
        changed = False
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                parts.append(part)
                continue
            image_url = part.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            if not isinstance(url, str):
                parts.append(part)
                continue
            stats["images"] += 1
            if url not in seen:
                seen[url] = normalize_url(url)
            replacement, state = seen[url]
            stats["bytes_before"] += len(url)
            stats["bytes_after"] += len(replacement)
            stats["codec_missing"] += int(state == "codec_missing")
            if replacement == url:
                parts.append(part)
                continue
            stats["optimized"] += 1
            changed = True
            new = dict(part)
            new["image_url"] = (dict(image_url, url=replacement)
                                if isinstance(image_url, dict) else replacement)
            parts.append(new)
        output.append(dict(message, content=parts) if changed else message)
    return output, stats
