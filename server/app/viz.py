"""Server-rendered SVG for the quantile dotplots (Kay et al., CHI 2016).

20 dots = the CDF inverted at even probabilities, binned Wilkinson-style so
overlapping values stack. Reading is counting: "late 1 day in 20" = the
rightmost dot. Rendered inline so the hero cards need no chart library.
"""

W, H, R = 280, 46, 4.5
BINS = 16


def dotplot_svg(dots: list) -> str:
    dots = [d for d in (dots or []) if d is not None]
    if len(dots) < 2:
        return ""
    lo, hi = min(dots), max(dots)
    span = (hi - lo) or 1

    # bin → stack
    bins: dict[int, int] = {}
    circles = []
    for d in dots:
        b = min(BINS - 1, int((d - lo) / span * BINS))
        level = bins.get(b, 0)
        bins[b] = level + 1
        cx = (b + 0.5) / BINS * (W - 2 * R) + R
        cy = H - R - 1 - level * (2 * R + 1)
        circles.append(
            f'<circle cx="{cx:.1f}" cy="{max(R, cy):.1f}" r="{R}" '
            f'fill="var(--series-predicted)" stroke="var(--paper-raised)" stroke-width="2"/>'
        )

    lo_min, hi_min = round(lo / 60), round(hi / 60)
    return (
        f'<svg viewBox="0 0 {W} {H + 14}" role="img" '
        f'aria-label="Distribution of travel times, {lo_min} to {hi_min} minutes. '
        f'Each of the 20 dots is a 1-in-20 chance.">'
        + "".join(circles)
        + f'<text x="0" y="{H + 12}" font-size="10" font-weight="600" '
          f'fill="var(--ink-faint)" font-family="var(--font-ui)">{lo_min} min</text>'
        + f'<text x="{W}" y="{H + 12}" font-size="10" font-weight="600" text-anchor="end" '
          f'fill="var(--ink-faint)" font-family="var(--font-ui)">{hi_min} min</text>'
        + "</svg>"
    )


def fmt_min(sec) -> str:
    """`47 min` up to 99, then `1 h 47 m` — colon notation is for stopwatches."""
    if sec is None:
        return "—"
    m = round(sec / 60)
    return f"{m // 60} h {m % 60} m" if m > 99 else f"{m} min"


def mins(sec):
    """Bare rounded minutes, for composing sentences ('+27 min each way')."""
    return "—" if sec is None else round(sec / 60)
