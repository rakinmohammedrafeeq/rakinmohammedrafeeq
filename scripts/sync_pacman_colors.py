#!/usr/bin/env python3
"""Sync the Pac-Man SVG contribution-cell palette with GitHub's live palette.

The upstream Pac-Man action currently renders fixed GitHub light/dark palettes.
This script changes only SVG fill colours belonging to those five contribution
levels. Pac-Man, ghosts, paths, labels, and backgrounds are left untouched.
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
        isHalloween
      }
    }
  }
}
"""

# The five contribution levels used by the generator's built-in themes:
# NONE, FIRST_QUARTILE, SECOND_QUARTILE, THIRD_QUARTILE, FOURTH_QUARTILE.
BUILT_IN_PALETTES = {
    "github-light": [
        "#ebedf0",
        "#9be9a8",
        "#40c463",
        "#30a14e",
        "#216e39",
    ],
    "github-dark": [
        "#161b22",
        "#0e4429",
        "#006d32",
        "#26a641",
        "#39d353",
    ],
}


def fetch_github_palette(username: str, token: str) -> tuple[list[str], bool]:
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
    if len(colors) != 5 or not all(
        isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color)
        for color in colors
    ):
        raise RuntimeError(
            "Expected GitHub's contribution calendar to return five hex colours; "
            f"received: {colors!r}"
        )

    return [color.lower() for color in colors], bool(calendar.get("isHalloween"))


def replace_fill(svg: str, old: str, new: str) -> tuple[str, int]:
    """Replace a palette colour only when used as an SVG fill."""
    escaped = re.escape(old)
    patterns = (
        # fill="#abcdef" or fill='#abcdef'
        re.compile(rf"(?i)(\bfill\s*=\s*['\"]){escaped}(['\"])") ,
        # inline style="...fill:#abcdef..." or style='...'
        re.compile(rf"(?i)(\bfill\s*:\s*){escaped}(?=\s*[;\"'])"),
    )
    count = 0
    for pattern in patterns:
        svg, replaced = pattern.subn(lambda match: match.group(1) + new + (match.group(2) if len(match.groups()) > 1 else ""), svg)
        count += replaced
    return svg, count


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
        target_colors, is_halloween = fetch_github_palette(username, token)
    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        print(f"Could not fetch GitHub's contribution palette: {exc}", file=sys.stderr)
        return 1

    print(
        "GitHub palette detected: "
        + ("Halloween" if is_halloween else "regular")
        + " — "
        + ", ".join(target_colors)
    )

    source_to_target: dict[str, str] = {}
    for palette in BUILT_IN_PALETTES.values():
        source_to_target.update(dict(zip(palette, target_colors)))

    for svg_path in svg_files:
        original = svg_path.read_text(encoding="utf-8")
        updated = original
        total_replacements = 0
        for old_color, new_color in source_to_target.items():
            updated, count = replace_fill(updated, old_color, new_color)
            total_replacements += count

        if total_replacements == 0:
            print(
                f"No known contribution fills were found in {svg_path}. "
                "The SVG was not changed; check whether the upstream action "
                "has changed its built-in palette.",
                file=sys.stderr,
            )
            return 1

        svg_path.write_text(updated, encoding="utf-8")
        print(f"Updated {svg_path}: {total_replacements} fill(s) recoloured.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
