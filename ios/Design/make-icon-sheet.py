#!/usr/bin/env python3
"""Assemble icon-concepts/*.svg into a single review sheet (HTML).

The sheet is the real test: every candidate at 180 / 120 / 60 px with iOS
corner masking, on the dusk ground and on a light ground, beside the current
shipping icon. An icon that only works at 1024 px fails here, visibly."""

import base64
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "icon-concepts")
OUT = os.path.join(SRC, "review-sheet.html")

# iOS masks icons with a superellipse; border-radius ~22.4% is the accepted
# visual approximation for previews.
CELL = """
<div class="cand">
  <div class="name">{name}</div>
  <div class="rationale">{rationale}</div>
  <div class="row dusk">{imgs}</div>
  <div class="row light">{imgs}</div>
</div>"""

IMG = '<img src="{src}" style="width:{px}px;height:{px}px" alt="">'


def svg_to_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def main():
    metas = json.load(open(os.path.join(SRC, "concepts.json")))
    cards = []
    for m in metas:
        path = os.path.join(SRC, m["file"])
        if m["file"].endswith(".svg"):
            src = svg_to_data_uri(open(path).read())
        else:  # current icon is a PNG
            src = "data:image/png;base64," + base64.b64encode(open(path, "rb").read()).decode()
        imgs = "".join(IMG.format(src=src, px=px) for px in (180, 120, 60))
        cards.append(CELL.format(name=m["name"], rationale=m.get("rationale", ""), imgs=imgs))

    html = f"""<!doctype html><meta charset="utf-8">
<title>Commute icon concepts</title>
<style>
  body {{ margin:0; padding:28px; background:#f4f1ea; color:#1a1b2e;
         font: 15px/1.5 -apple-system, 'Inter Tight', sans-serif; }}
  h1 {{ font-family: 'New York', Georgia, serif; font-size:26px; margin:0 0 4px; }}
  .sub {{ color:#635f7d; margin-bottom:24px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:22px; }}
  .cand {{ background:#fff; border:1px solid #e0dbd0; border-radius:14px; padding:18px;
           box-shadow:0 1px 2px rgba(26,27,46,.06); }}
  .name {{ font-weight:700; margin-bottom:2px; }}
  .rationale {{ color:#635f7d; font-size:13px; min-height:38px; margin-bottom:12px; }}
  .row {{ display:flex; align-items:flex-end; flex-wrap:wrap; gap:16px; padding:16px;
          border-radius:10px; }}
  .row.dusk {{ background:#0f1020; margin-bottom:8px; }}
  .row.light {{ background:#e8e4da; }}
  img {{ border-radius:22.4%; display:block; }}
</style>
<h1>Commute — icon concepts</h1>
<div class="sub">Each candidate at 180 / 120 / 60&nbsp;px with iOS corner masking,
on dark and light wallpaper. Judge at 60&nbsp;px — that's the Home Screen.</div>
<div class="grid">{"".join(cards)}</div>"""
    open(OUT, "w").write(html)
    print("sheet:", OUT)


if __name__ == "__main__":
    sys.exit(main())
