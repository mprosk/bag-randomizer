#!/usr/bin/env python3
"""Extract representative bag colors from Lindquist product photography."""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import re
import shutil
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP_FILE = ROOT / "script.js"
CACHE_DIR = ROOT / ".color-cache"
PRODUCTS_URL = "https://lindquist-object.com/products.json?limit=250&page={page}"
CARRY_URL = "https://lindquist-object.com/collections/carry/products.json?limit=250"
EXCLUDED_TITLES = re.compile(r"wallet|duffel|case|belt|card|charm|sunglass", re.IGNORECASE)
BAG_MODELS = re.compile(
    r"(?:^| )(?:Ama|Anni|Big Ama|Eggi|Faba|Gala|La Mama|Lee|Linda|Lina|"
    r"Little Lee|Mezzo|Mini Eggi|Pal|Ray|Savoie|Segui|Val) in ",
    re.IGNORECASE,
)
SWATCH_PATTERN = re.compile(
    r'(^  (?:"(?P<quoted>[^"]+)"|(?P<plain>[A-Za-z][A-Za-z ]*)): \{\n'
    r'    swatch: ")(?P<hex>#[0-9a-fA-F]{6})(")',
    re.MULTILINE,
)


def fetch_json(url: str) -> dict[str, Any]:
    last_error: OSError | None = None
    for attempt in range(3):
        request = urllib.request.Request(url, headers={"User-Agent": "bag-palette-color-audit/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except OSError as error:
            last_error = error
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"Could not download {url}") from last_error


def fetch_products() -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = fetch_json(PRODUCTS_URL.format(page=page)).get("products", [])
        products.extend(batch)
        if len(batch) < 250:
            return products
        page += 1


def eligible_products(products: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    carry_handles = {
        product["handle"]
        for product in fetch_json(CARRY_URL).get("products", [])
        if product.get("handle")
    }
    grouped: dict[str, list[dict[str, str]]] = {}

    for product in products:
        title = str(product.get("title", ""))
        handle = str(product.get("handle", ""))
        images = product.get("images") or []
        match = re.search(r" in ([^|]+)$", title)
        is_bag = handle in carry_handles or bool(BAG_MODELS.search(title))

        if not match or not images or not is_bag or EXCLUDED_TITLES.search(title):
            continue

        color = match.group(1).strip()
        grouped.setdefault(color, []).append(
            {"title": title, "handle": handle, "image": str(images[0]["src"])}
        )

    return grouped


def download_image(product: dict[str, str]) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    destination = CACHE_DIR / f"{product['handle']}.jpg"
    if destination.exists():
        return destination

    separator = "&" if "?" in product["image"] else "?"
    request = urllib.request.Request(
        f"{product['image']}{separator}width=700",
        headers={"User-Agent": "bag-palette-color-audit/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        destination.write_bytes(response.read())
    return destination


def image_pixels(path: Path) -> list[tuple[int, int, int]]:
    command = [
        "magick",
        str(path),
        "-auto-orient",
        "-resize",
        "120x120^",
        "-gravity",
        "center",
        "-extent",
        "120x120",
        "-colorspace",
        "sRGB",
        "rgb:-",
    ]
    result = subprocess.run(command, check=True, capture_output=True)
    raw = result.stdout
    return [(raw[index], raw[index + 1], raw[index + 2]) for index in range(0, len(raw), 3)]


def median_rgb(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    return tuple(
        round(statistics.median(pixel[channel] for pixel in pixels))
        for channel in range(3)
    )  # type: ignore[return-value]


def extract_image_color(path: Path, color_name: str) -> tuple[int, int, int]:
    pixels = image_pixels(path)
    candidates: list[tuple[int, int, int]] = []

    for red, green, blue in pixels:
        _, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        if color_name == "Black":
            if value < 0.42:
                candidates.append((red, green, blue))
        elif saturation >= 0.12 and 0.08 < value < 0.97:
            candidates.append((red, green, blue))

    if len(candidates) < 80:
        candidates = [
            pixel
            for pixel in pixels
            if max(pixel) < 242 and max(pixel) - min(pixel) >= 10
        ]
    if not candidates:
        raise ValueError(f"No foreground color pixels found in {path.name}")

    return median_rgb(candidates)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.removeprefix("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    channels = []
    for value in rgb:
        channel = value / 255
        channels.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    x = (red * 0.4124 + green * 0.3576 + blue * 0.1805) / 0.95047
    y = red * 0.2126 + green * 0.7152 + blue * 0.0722
    z = (red * 0.0193 + green * 0.1192 + blue * 0.9505) / 1.08883

    def transform(value: float) -> float:
        return value ** (1 / 3) if value > 0.008856 else 7.787 * value + 16 / 116

    fx, fy, fz = transform(x), transform(y), transform(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def color_distance(first: str, second: str) -> float:
    first_lab = rgb_to_lab(hex_to_rgb(first))
    second_lab = rgb_to_lab(hex_to_rgb(second))
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(first_lab, second_lab)))


def current_swatches(source: str) -> dict[str, str]:
    return {
        match.group("quoted") or match.group("plain"): match.group("hex").upper()
        for match in SWATCH_PATTERN.finditer(source)
    }


def evenly_spaced(items: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    if len(items) <= limit:
        return items
    indexes = [round(index * (len(items) - 1) / (limit - 1)) for index in range(limit)]
    return [items[index] for index in indexes]


def audit(samples_per_color: int) -> dict[str, dict[str, Any]]:
    source = APP_FILE.read_text()
    existing = current_swatches(source)
    products_by_color = eligible_products(fetch_products())
    report: dict[str, dict[str, Any]] = {}

    for color in sorted(existing):
        products = evenly_spaced(products_by_color.get(color, []), samples_per_color)
        samples = []
        extracted_values = []
        for product in products:
            try:
                extracted = extract_image_color(download_image(product), color)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                samples.append({"product": product["title"], "error": str(error)})
                continue
            extracted_values.append(extracted)
            samples.append({"product": product["title"], "hex": rgb_to_hex(extracted)})

        if not extracted_values:
            report[color] = {"current": existing[color], "error": "No usable product images"}
            continue

        recommended = rgb_to_hex(median_rgb(extracted_values))
        report[color] = {
            "current": existing[color],
            "recommended": recommended,
            "delta_e": round(color_distance(existing[color], recommended), 1),
            "products_found": len(products_by_color.get(color, [])),
            "samples": samples,
        }

    return report


def update_swatches(report: dict[str, dict[str, Any]]) -> None:
    source = APP_FILE.read_text()

    def replace(match: re.Match[str]) -> str:
        color = match.group("quoted") or match.group("plain")
        recommended = report.get(color, {}).get("recommended", match.group("hex"))
        return f"{match.group(1)}{recommended.lower()}{match.group(5)}"

    APP_FILE.write_text(SWATCH_PATTERN.sub(replace, source))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3, help="Images sampled per color")
    parser.add_argument("--output", type=Path, default=ROOT / "color-audit.json")
    parser.add_argument("--update", action="store_true", help="Apply recommended colors to script.js")
    args = parser.parse_args()

    if shutil.which("magick") is None:
        raise SystemExit("ImageMagick is required. Install it with: brew install imagemagick")
    if args.samples < 1:
        raise SystemExit("--samples must be at least 1")

    report = audit(args.samples)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    if args.update:
        update_swatches(report)

    changed = sum(
        details.get("delta_e", 0) >= 8
        for details in report.values()
    )
    print(f"Audited {len(report)} colors; {changed} differ visibly (Delta E >= 8).")
    print(f"Report: {args.output}")
    if args.update:
        print(f"Updated: {APP_FILE}")


if __name__ == "__main__":
    main()
