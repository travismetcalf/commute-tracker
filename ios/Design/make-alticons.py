#!/usr/bin/env python3
"""Build alternate-app-icon asset sets from rendered PNGs.

Input:  Design/icon-exports/{peaks,arc,rush,dart}.png (1024x1024, rendered from
        the flat concept SVGs via rsvg-convert — see the loop in git history)
Output: Assets.xcassets/AppIcon.appiconset            (PRIMARY = peaks, the "M")
        Assets.xcassets/AppIcon{Arc,Dart}.appiconset       (alternates)
        Assets.xcassets/IconPreview{Peaks,Arc,Dart}.imageset (picker thumbs)

Travis's calls (2026-08-03): flat art only (lit versions and ribbons rejected —
"ribbon just looks like a snake"); primary = the two-hump M (Twin Peaks);
alternates = arc, dart-goldtrail, grid-badge, ab-dusk, ab-crimson.
"""

import json
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
EXPORTS = os.path.join(HERE, "icon-exports")
CATALOG = os.path.join(HERE, "..", "Commute", "Assets.xcassets")

PRIMARY = "badge.png"
ICONS = {"Arc": "arc.png", "Dart": "dart.png", "Peaks": "peaks.png",
         "ABDusk": "ab-dusk.png", "ABCrimson": "ab-crimson.png"}


def write_appiconset(name: str, src: str):
    d = os.path.join(CATALOG, f"AppIcon{name}.appiconset")
    os.makedirs(d, exist_ok=True)
    img = Image.open(src).convert("RGB")
    assert img.size == (1024, 1024), f"{src} is {img.size}, need 1024x1024"
    img.save(os.path.join(d, "icon-1024.png"))
    json.dump({
        "images": [{"filename": "icon-1024.png", "idiom": "universal",
                    "platform": "ios", "size": "1024x1024"}],
        "info": {"author": "xcode", "version": 1},
    }, open(os.path.join(d, "Contents.json"), "w"), indent=2)


def write_preview(name: str, src: str):
    d = os.path.join(CATALOG, f"IconPreview{name}.imageset")
    os.makedirs(d, exist_ok=True)
    Image.open(src).convert("RGB").resize((180, 180), Image.LANCZOS).save(
        os.path.join(d, "preview.png"))
    json.dump({
        "images": [{"filename": "preview.png", "idiom": "universal"}],
        "info": {"author": "xcode", "version": 1},
    }, open(os.path.join(d, "Contents.json"), "w"), indent=2)


def main():
    # primary: overwrite AppIcon.appiconset with the peaks art
    primary = os.path.join(EXPORTS, PRIMARY)
    if not os.path.exists(primary):
        sys.exit(f"missing export: {primary}")
    write_appiconset("", primary)          # AppIcon.appiconset
    write_preview("Badge", primary)
    for name, fn in ICONS.items():
        src = os.path.join(EXPORTS, fn)
        if not os.path.exists(src):
            sys.exit(f"missing export: {src}")
        write_appiconset(name, src)
        write_preview(name, src)
    print("asset sets written")


if __name__ == "__main__":
    main()
