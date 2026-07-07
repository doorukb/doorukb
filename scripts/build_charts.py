#!/usr/bin/env python3
"""Build the profile charts for github.com/doorukb.

Pulls live language statistics from the GitHub API, aggregates them across
all public (non-fork) repositories, and renders four theme-aware SVGs:

    assets/langs-dark.svg      donut chart - languages by bytes of code
    assets/langs-light.svg
    assets/concepts-dark.svg   bar chart   - concepts by share of projects
    assets/concepts-light.svg

Concept weights come from data/concepts.json: every repo contributes one
vote, split equally across the concepts it is mapped to.

Runs on the Python standard library only. Used by the weekly GitHub Action
(.github/workflows/refresh-charts.yml), and runnable locally:

    python scripts/build_charts.py             # fetch live data
    python scripts/build_charts.py --offline   # reuse data/languages.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ASSETS_DIR = os.path.join(ROOT, "assets")
SNAPSHOT_PATH = os.path.join(DATA_DIR, "languages.json")
CONCEPTS_PATH = os.path.join(DATA_DIR, "concepts.json")

API = "https://api.github.com"

# GitHub linguist colors for the languages that show up in this account,
# plus a neutral fallback cycle for anything new.
LANG_COLORS = {
    "Python": "#3572A5",
    "C": "#8b949e",
    "C++": "#f34b7d",
    "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a",
    "HTML": "#e34c26",
    "CSS": "#663399",
    "Shell": "#89e051",
    "PEG.js": "#234d6b",
    "Makefile": "#427819",
    "Dockerfile": "#384d54",
    "Jupyter Notebook": "#DA5B0B",
    "Go": "#00ADD8",
    "Rust": "#dea584",
    "Java": "#b07219",
    "Other": "#6e7681",
}
FALLBACK_COLORS = ["#22d3ee", "#a78bfa", "#f778ba", "#ffa657", "#7ee787"]

THEMES = {
    "dark": {
        "bg": "#0d1117",
        "border": "#30363d",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "track": "#21262d",
        "accent": "#22d3ee",
        "accent2": "#a78bfa",
    },
    "light": {
        "bg": "#ffffff",
        "border": "#d0d7de",
        "text": "#1f2328",
        "muted": "#656d76",
        "track": "#eaeef2",
        "accent": "#0969da",
        "accent2": "#8250df",
    },
}


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------

def fetch_json(url: str, token: str | None):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "doorukb-profile-charts")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def collect_languages(user: str, token: str | None, exclude_repos: set[str]):
    """Return {repo_name: {language: bytes}} for all public non-fork repos."""
    repos = fetch_json(f"{API}/users/{user}/repos?per_page=100&type=owner", token)
    per_repo: dict[str, dict[str, int]] = {}
    for repo in repos:
        name = repo["name"]
        if repo.get("fork") or repo.get("archived") or name in exclude_repos:
            continue
        langs = fetch_json(f"{API}/repos/{user}/{name}/languages", token)
        if langs:
            per_repo[name] = langs
    return per_repo


def aggregate(per_repo: dict, exclude_languages: set[str], min_slice_pct: float):
    """Merge per-repo byte counts into an ordered [(lang, bytes, pct)] list,
    grouping slices below min_slice_pct into 'Other'."""
    totals: dict[str, int] = {}
    for langs in per_repo.values():
        for lang, nbytes in langs.items():
            if lang in exclude_languages:
                continue
            totals[lang] = totals.get(lang, 0) + nbytes

    grand = sum(totals.values())
    if grand == 0:
        raise SystemExit("No language data collected - nothing to draw.")

    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    main, other = [], 0
    for lang, nbytes in ordered:
        pct = 100.0 * nbytes / grand
        if pct < min_slice_pct:
            other += nbytes
        else:
            main.append((lang, nbytes, pct))
    if other:
        main.append(("Other", other, 100.0 * other / grand))
    return main, grand


def concept_weights(per_repo: dict, concepts: dict[str, list[str]]):
    """Each repo casts one vote, split evenly across its mapped concepts.
    Returns ordered [(concept, share_pct)] plus the number of mapped repos."""
    repo_to_concepts: dict[str, list[str]] = {}
    for concept, repos in concepts.items():
        for repo in repos:
            repo_to_concepts.setdefault(repo, []).append(concept)

    weights = {c: 0.0 for c in concepts}
    mapped = 0
    for repo in per_repo:
        cs = repo_to_concepts.get(repo)
        if not cs:
            print(f"  note: repo '{repo}' has no concept mapping", file=sys.stderr)
            continue
        mapped += 1
        for c in cs:
            weights[c] += 1.0 / len(cs)

    total = sum(weights.values()) or 1.0
    ordered = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    return [(c, 100.0 * w / total) for c, w in ordered if w > 0], mapped


# --------------------------------------------------------------------------
# SVG helpers
# --------------------------------------------------------------------------

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def lang_color(lang: str, i: int) -> str:
    return LANG_COLORS.get(lang, FALLBACK_COLORS[i % len(FALLBACK_COLORS)])


def polar(cx: float, cy: float, r: float, deg: float):
    rad = math.radians(deg)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def arc_path(cx, cy, r, start_deg, end_deg):
    x0, y0 = polar(cx, cy, r, start_deg)
    x1, y1 = polar(cx, cy, r, end_deg)
    large = 1 if (end_deg - start_deg) > 180 else 0
    return f"M {x0:.2f} {y0:.2f} A {r} {r} 0 {large} 1 {x1:.2f} {y1:.2f}"


FONT = "'Segoe UI', system-ui, -apple-system, sans-serif"


def card_shell(width, height, theme, inner, extra_css=""):
    t = THEMES[theme]
    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}"
     xmlns="http://www.w3.org/2000/svg" role="img">
  <style>
    .txt   {{ font: 600 15px {FONT}; fill: {t['text']}; }}
    .sub   {{ font: 400 11.5px {FONT}; fill: {t['muted']}; }}
    .lbl   {{ font: 500 12.5px {FONT}; fill: {t['text']}; }}
    .pct   {{ font: 600 12.5px {FONT}; fill: {t['muted']}; }}
    .big   {{ font: 700 30px {FONT}; fill: {t['text']}; }}
    {extra_css}
    @media (prefers-reduced-motion: reduce) {{
      * {{ animation: none !important; }}
    }}
  </style>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10"
        fill="{t['bg']}" stroke="{t['border']}"/>
  {inner}
</svg>
"""


def render_langs(slices, grand, n_repos, theme):
    t = THEMES[theme]
    W, H = 780, 320
    cx, cy, r, stroke = 170, 178, 96, 30
    gap_deg = 3.0

    css = """
    .seg {
      stroke-dasharray: 100 100;
      stroke-dashoffset: 100;
      animation: draw 0.9s ease-out forwards;
    }
    @keyframes draw { to { stroke-dashoffset: 0; } }
    .fade { opacity: 0; animation: fadein 0.5s ease-out forwards; }
    @keyframes fadein { to { opacity: 1; } }
    """

    parts = []
    parts.append(f'<text class="txt" x="24" y="34">Languages</text>')
    parts.append(
        f'<text class="sub" x="24" y="52">by bytes of code, across '
        f'{n_repos} public repositories</text>'
    )

    angle = -90.0
    delay = 0.0
    for i, (lang, nbytes, pct) in enumerate(slices):
        span = 360.0 * pct / 100.0
        a0 = angle + gap_deg / 2
        a1 = angle + span - gap_deg / 2
        if a1 > a0:
            d = arc_path(cx, cy, r, a0, a1)
            parts.append(
                f'<path class="seg" d="{d}" pathLength="100" fill="none" '
                f'stroke="{lang_color(lang, i)}" stroke-width="{stroke}" '
                f'style="animation-delay:{delay:.2f}s"/>'
            )
        angle += span
        delay += 0.12

    kb = grand / 1024.0
    size_label = f"{kb / 1024.0:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"
    parts.append(
        f'<text class="big fade" x="{cx}" y="{cy - 2}" text-anchor="middle" '
        f'style="animation-delay:0.5s">{n_repos}</text>'
    )
    parts.append(
        f'<text class="sub fade" x="{cx}" y="{cy + 18}" text-anchor="middle" '
        f'style="animation-delay:0.5s">repos &#183; {size_label}</text>'
    )

    # Legend
    lx, ly, row = 360, 92, 30
    for i, (lang, nbytes, pct) in enumerate(slices):
        col = i // 7
        x = lx + col * 210
        y = ly + (i % 7) * row
        d = 0.15 + i * 0.1
        parts.append(
            f'<g class="fade" style="animation-delay:{d:.2f}s">'
            f'<circle cx="{x}" cy="{y - 4}" r="6" fill="{lang_color(lang, i)}"/>'
            f'<text class="lbl" x="{x + 16}" y="{y}">{esc(lang)}</text>'
            f'<text class="pct" x="{x + 150}" y="{y}" text-anchor="end">'
            f'{pct:.1f}%</text></g>'
        )

    today = datetime.date.today().isoformat()
    parts.append(
        f'<text class="sub" x="{W - 24}" y="{H - 16}" text-anchor="end">'
        f'auto-generated &#183; {today}</text>'
    )
    return card_shell(W, H, theme, "\n  ".join(parts), css)


def render_concepts(weights, mapped, theme):
    t = THEMES[theme]
    W = 780
    top, row_h = 84, 36
    H = top + len(weights) * row_h + 40
    label_w, bar_x, bar_w = 250, 274, 420

    css = f"""
    .bar {{
      transform: scaleX(0);
      transform-origin: {bar_x}px 0;
      animation: grow 0.8s cubic-bezier(0.22, 1, 0.36, 1) forwards;
    }}
    @keyframes grow {{ to {{ transform: scaleX(1); }} }}
    .fade {{ opacity: 0; animation: fadein 0.5s ease-out forwards; }}
    @keyframes fadein {{ to {{ opacity: 1; }} }}
    """

    parts = []
    parts.append(f'<text class="txt" x="24" y="34">Concepts</text>')
    parts.append(
        f'<text class="sub" x="24" y="52">share of projects touching each area '
        f'({mapped} projects, equal vote each)</text>'
    )
    parts.append(
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{t["accent"]}"/>'
        f'<stop offset="1" stop-color="{t["accent2"]}"/>'
        f'</linearGradient></defs>'
    )

    max_pct = max(p for _, p in weights)
    for i, (concept, pct) in enumerate(weights):
        y = top + i * row_h
        w = bar_w * pct / max_pct
        d = 0.1 + i * 0.12
        parts.append(
            f'<text class="lbl fade" x="24" y="{y + 15}" '
            f'style="animation-delay:{d:.2f}s">{esc(concept)}</text>'
        )
        parts.append(
            f'<rect x="{bar_x}" y="{y + 2}" width="{bar_w}" height="16" rx="8" '
            f'fill="{t["track"]}"/>'
        )
        parts.append(
            f'<rect class="bar" x="{bar_x}" y="{y + 2}" width="{w:.1f}" '
            f'height="16" rx="8" fill="url(#g)" '
            f'style="animation-delay:{d:.2f}s"/>'
        )
        parts.append(
            f'<text class="pct fade" x="{bar_x + bar_w + 14}" y="{y + 15}" '
            f'style="animation-delay:{d + 0.3:.2f}s">{pct:.0f}%</text>'
        )

    today = datetime.date.today().isoformat()
    parts.append(
        f'<text class="sub" x="{W - 24}" y="{H - 14}" text-anchor="end">'
        f'auto-generated &#183; {today}</text>'
    )
    return card_shell(W, H, theme, "\n  ".join(parts), css)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true",
                    help="render from data/languages.json instead of the API")
    args = ap.parse_args()

    with open(CONCEPTS_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    config = cfg["config"]
    user = config["user"]
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    if args.offline:
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            per_repo = json.load(f)["repos"]
        print(f"offline mode: {len(per_repo)} repos from snapshot")
    else:
        try:
            per_repo = collect_languages(
                user, token, set(config.get("exclude_repos", [])))
            snapshot = {
                "generated": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "user": user,
                "repos": per_repo,
            }
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, sort_keys=True)
                f.write("\n")
            print(f"fetched language data for {len(per_repo)} repos")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            if not os.path.exists(SNAPSHOT_PATH):
                raise
            print(f"API unavailable ({e}); falling back to snapshot",
                  file=sys.stderr)
            with open(SNAPSHOT_PATH, encoding="utf-8") as f:
                per_repo = json.load(f)["repos"]

    slices, grand = aggregate(
        per_repo,
        set(config.get("exclude_languages", [])),
        float(config.get("min_slice_pct", 2.0)),
    )
    weights, mapped = concept_weights(per_repo, cfg["concepts"])

    os.makedirs(ASSETS_DIR, exist_ok=True)
    for theme in THEMES:
        for name, svg in (
            (f"langs-{theme}.svg", render_langs(slices, grand, len(per_repo), theme)),
            (f"concepts-{theme}.svg", render_concepts(weights, mapped, theme)),
        ):
            path = os.path.join(ASSETS_DIR, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
            print(f"wrote {os.path.relpath(path, ROOT)}")

    print("\nlanguage share:")
    for lang, nbytes, pct in slices:
        print(f"  {lang:<14s} {pct:5.1f}%  ({nbytes:,} bytes)")


if __name__ == "__main__":
    main()
