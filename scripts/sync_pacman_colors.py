#!/usr/bin/env python3
"""Mirror GitHub's live contribution-calendar colours in Pac-Man SVGs.

Only the animated contribution-cell colours are changed. Static empty-cell
fills, SVG backgrounds, maze walls, Pac-Man, ghosts, and labels are untouched.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

GRAPHQL_URL = "https://api.github.com/graphql"
QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        colors
      }
    }
  }
}
"""

# Source contribution-level colours emitted by the upstream Pac-Man action.
# Each maps to a GitHub intensity level in ascending order: L1, L2, L3, L4.
# These are the action's current built-in GitHub light and dark palettes.
SOURCE_LEVELS = {
    "#9be9a8": 0,  # light L1
    "#40c463": 1,  # light L2
    "#30a14e": 2,  # light L3
    "#216e39": 3,  # light L4
    "#0e4429": 0,  # dark L1
    "#006d32": 1,  # dark L2
    "#26a641": 2,  # dark L3
    "#39d353": 3,  # dark L4
}

# The upstream generator identifies contribution-day rectangles as c-WEEK-DAY.
CELL_RECT_RE = re.compile(
    r"<rect\b(?=[^>]*\bid\s*=\s*(['\"])c-\d+-\d+\1)[^>]*>.*?</rect>",
    re.IGNORECASE | re.DOTALL,
)
ANIMATE_TAG_RE = re.compile(r"<animate\b[^>]*>", re.IGNORECASE | re.DOTALL)
ATTRIBUTE_NAME_RE = re.compile(
    r"\battributeName\s*=\s*(['\"])fill\1", re.IGNORECASE
)
VALUES_RE = re.compile(
    r"(\bvalues\s*=\s*)(['\"])(.*?)\2", re.IGNORECASE | re.DOTALL
)
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def fetch_github_palette(username: str, token: str) -> list[str]:
    """Fetch the four live contribution intensity colours from GitHub."""
    payload = json.dumps(
        {"query": QUERY, "variables": {"login": username}}
    ).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "pacman-contribution-color-sync",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub GraphQL returned HTTP {exc.code}: {details}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach GitHub GraphQL: {exc}") from exc

    if result.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {result['errors']}")

    user = (result.get("data") or {}).get("user")
    if not user:
        raise RuntimeError(f"GitHub user '{username}' was not found.")

    calendar = user["contributionsCollection"]["contributionCalendar"]
    colors = calendar.get("colors", [])

    # GitHub's calendar exposes four non-empty contribution intensity shades.
    if len(colors) != 4 or not all(
        isinstance(color, str) and HEX_COLOR_RE.fullmatch(color)
        for color in colors
    ):
        raise RuntimeError(
            "Expected GitHub's contribution calendar to return four "
            f"hex intensity colours (L1-L4); received: {colors!r}"
        )

    # API order is least to most contribution intensity.
    return [color.lower() for color in colors]


def recolor_svg(svg_text: str, target_colors: list[str], file_name: str) -> tuple[str, int]:
    """Replace only the first animated fill on each contribution-cell rect."""
    stats = [0, 0, 0, 0]
    matched_cells = 0
    unknown_colors: set[str] = set()

    def update_cell(cell_match: re.Match[str]) -> str:
        nonlocal matched_cells
        cell_block = cell_match.group(0)

        def update_animation(animation_match: re.Match[str]) -> str:
            nonlocal matched_cells
            tag = animation_match.group(0)

            if not ATTRIBUTE_NAME_RE.search(tag):
                return tag

            values_match = VALUES_RE.search(tag)
            if not values_match:
                return tag

            values_text = values_match.group(3)
            values = values_text.split(";")
            if not values or not values[0].strip():
                return tag

            original_first = values[0]
            source_color = original_first.strip().lower()
            level = SOURCE_LEVELS.get(source_color)
            if level is None:
                unknown_colors.add(source_color)
                return tag

            # Replace ONLY the animated active-contribution colour (the first
            # item). Later values are the empty-cell/reset colour and stay put.
            leading = original_first[: len(original_first) - len(original_first.lstrip())]
            trailing = original_first[len(original_first.rstrip()):]
            values[0] = f"{leading}{target_colors[level]}{trailing}"
            new_values_text = ";".join(values)

            new_tag = (
                tag[: values_match.start(3)]
                + new_values_text
                + tag[values_match.end(3):]
            )
            matched_cells += 1
            stats[level] += 1
            return new_tag

        return ANIMATE_TAG_RE.sub(update_animation, cell_block)

    updated = CELL_RECT_RE.sub(update_cell, svg_text)

    if matched_cells == 0:
        detail = (
            f" Unrecognized animated contribution colours: "
            f"{', '.join(sorted(unknown_colors))}."
            if unknown_colors else ""
        )
        raise RuntimeError(
            f"No known animated contribution cells were found in {file_name}."
            " The SVG was not changed; the upstream generator's SVG structure "
            f"or built-in palette may have changed.{detail}"
        )

    if unknown_colors:
        raise RuntimeError(
            f"Unknown contribution-cell source colours in {file_name}: "
            f"{', '.join(sorted(unknown_colors))}. No files were written."
        )

    return updated, matched_cells


def main() -> int:
    username = os.environ.get("GITHUB_USER_NAME", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    dist_dir = Path("dist")

    if not username or not token:
        print("GITHUB_USER_NAME and GITHUB_TOKEN are required.", file=sys.stderr)
        return 2
    if not dist_dir.is_dir():
        print("The generator output directory 'dist/' was not found.", file=sys.stderr)
        return 2

    svg_files = sorted(dist_dir.glob("*.svg"))
    if not svg_files:
        print("No generated SVG files were found in dist/.", file=sys.stderr)
        return 2

    try:
        target_colors = fetch_github_palette(username, token)
        print("GitHub's live contribution palette (L1 → L4): " + ", ".join(target_colors))

        # Prepare every file in memory first. If any SVG is incompatible, no
        # output file is partially updated.
        prepared: list[tuple[Path, str, int]] = []
        for svg_path in svg_files:
            original = svg_path.read_text(encoding="utf-8")
            updated, count = recolor_svg(original, target_colors, svg_path.name)
            prepared.append((svg_path, updated, count))

    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        print(f"Could not synchronize Pac-Man colours: {exc}", file=sys.stderr)
        return 1

    for svg_path, updated, count in prepared:
        svg_path.write_text(updated, encoding="utf-8")
        print(f"Updated {svg_path}: {count} animated contribution cell(s) recoloured.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
