#!/usr/bin/env python3
"""Mirror GitHub's exact per-day contribution colours in Pac-Man SVGs.

The upstream Pac-Man action uses its own built-in palette. Rather than trying
to infer contribution intensity from those source colours, this script fetches
the colour assigned to every date by GitHub's GraphQL contribution calendar
and applies that exact colour to the matching SVG cell (c-WEEK-WEEKDAY).

Only the first value of each contribution cell's animated fill is changed.
Empty-cell/reset fills, static cell backgrounds, maze walls, Pac-Man, ghosts,
and all other SVG artwork remain untouched.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GRAPHQL_URL = "https://api.github.com/graphql"
QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        colors
        isHalloween
        weeks {
          contributionDays {
            color
            contributionCount
            date
            weekday
          }
        }
      }
    }
  }
}
"""

HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
CELL_RECT_RE = re.compile(
    r"<rect\b(?P<attrs>[^>]*)>(?P<body>.*?)</rect>",
    re.IGNORECASE | re.DOTALL,
)
CELL_ID_RE = re.compile(r"""\bid\s*=\s*(["'])c-(\d+)-(\d+)\1""", re.IGNORECASE)
ANIMATE_TAG_RE = re.compile(r"<animate\b[^>]*>", re.IGNORECASE | re.DOTALL)
ATTRIBUTE_NAME_RE = re.compile(r"""\battributeName\s*=\s*(["'])fill\1""", re.IGNORECASE)
VALUES_RE = re.compile(r"""(\bvalues\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)

MONTH_NAMES = {
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
}
MONTH_TEXT_RE = re.compile(
    r"<text\b(?P<attrs>[^>]*)>(?P<label>[^<]*)</text>",
    re.IGNORECASE | re.DOTALL,
)


def _svg_attr(attrs: str, name: str) -> str | None:
    match = re.search(
        rf"""\b{re.escape(name)}\s*=\s*(["'])(.*?)\1""",
        attrs,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(2) if match else None


def remove_duplicate_boundary_month_label(svg_text: str) -> tuple[str, bool]:
    """Remove the first month label only when it duplicates the final label."""
    month_matches: list[tuple[re.Match[str], str, float]] = []

    for match in MONTH_TEXT_RE.finditer(svg_text):
        label = match.group("label").strip().lower()
        if label not in MONTH_NAMES:
            continue

        attrs = match.group("attrs")
        y_value = _svg_attr(attrs, "y")
        x_value = _svg_attr(attrs, "x")
        if y_value is None or x_value is None:
            continue

        try:
            if abs(float(y_value) - 10.0) > 0.1:
                continue
            x = float(x_value)
        except ValueError:
            continue

        month_matches.append((match, label, x))

    if len(month_matches) < 2:
        return svg_text, False

    first_match, first_month, first_x = month_matches[0]
    _, last_month, last_x = month_matches[-1]
    if first_month == last_month and first_x < last_x:
        return (
            svg_text[:first_match.start()] + svg_text[first_match.end():],
            True,
        )

    return svg_text, False


def fetch_contribution_calendar(username: str, token: str) -> dict[str, Any]:
    """Fetch GitHub's contribution calendar, including each day's exact color."""
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

    try:
        calendar = user["contributionsCollection"]["contributionCalendar"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "GitHub GraphQL response did not contain a contribution calendar."
        ) from exc

    if not isinstance(calendar.get("weeks"), list) or not calendar["weeks"]:
        raise RuntimeError("GitHub returned an empty contribution calendar.")

    return calendar


def build_day_color_map(calendar: dict[str, Any]) -> dict[tuple[int, int], str]:
    """Return {(week_index, weekday_index): GitHub's exact day color}."""
    day_colors: dict[tuple[int, int], str] = {}

    for week_index, week in enumerate(calendar["weeks"]):
        days = week.get("contributionDays", [])
        if not isinstance(days, list):
            raise RuntimeError(f"Invalid contributionDays in week {week_index}.")

        for day in days:
            try:
                weekday = int(day["weekday"])
                color = str(day["color"]).lower()
                contribution_count = int(day["contributionCount"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid contribution day data in week {week_index}: {day!r}"
                ) from exc

            if weekday < 0 or weekday > 6:
                raise RuntimeError(
                    f"GitHub returned invalid weekday {weekday} in week {week_index}."
                )
            if not HEX_COLOR_RE.fullmatch(color):
                raise RuntimeError(
                    f"GitHub returned an invalid day colour {color!r}."
                )

            key = (week_index, weekday)
            if key in day_colors:
                raise RuntimeError(f"Duplicate GitHub contribution day key: {key}.")

            day_colors[key] = color

    if not day_colors:
        raise RuntimeError("GitHub returned no contribution-day colours.")

    return day_colors


def recolor_svg(
    svg_text: str,
    day_colors: dict[tuple[int, int], str],
    file_name: str,
) -> tuple[str, int]:
    """Apply each GitHub day's color to its matching animated SVG cell."""
    matched_cells = 0
    missing_day_keys: set[tuple[int, int]] = set()
    replaced_colors: set[str] = set()

    def update_cell(cell_match: re.Match[str]) -> str:
        nonlocal matched_cells

        attrs = cell_match.group("attrs")
        id_match = CELL_ID_RE.search(attrs)
        if not id_match:
            return cell_match.group(0)

        week_index = int(id_match.group(2))
        weekday = int(id_match.group(3))
        key = (week_index, weekday)
        body = cell_match.group("body")
        day_color = day_colors.get(key)

        if day_color is None:
            missing_day_keys.add(key)
            return cell_match.group(0)

        def update_animation(animation_match: re.Match[str]) -> str:
            nonlocal matched_cells
            tag = animation_match.group(0)

            if not ATTRIBUTE_NAME_RE.search(tag):
                return tag

            values_match = VALUES_RE.search(tag)
            if not values_match:
                return tag

            parts = values_match.group(3).split(";")
            if not parts or not parts[0].strip():
                return tag

            # The first value is the active contribution fill. Keep all later
            # values unchanged; they control the empty/reset animation state.
            parts[0] = day_color
            new_values = ";".join(parts)
            matched_cells += 1
            replaced_colors.add(day_color)

            return (
                tag[: values_match.start(3)]
                + new_values
                + tag[values_match.end(3):]
            )

        new_body = ANIMATE_TAG_RE.sub(update_animation, body)
        return cell_match.group(0)[: cell_match.start("body") - cell_match.start()] + new_body + cell_match.group(0)[cell_match.end("body") - cell_match.start():]

    updated = CELL_RECT_RE.sub(update_cell, svg_text)

    if missing_day_keys:
        preview = ", ".join(
            f"week {week}/weekday {day}"
            for week, day in sorted(missing_day_keys)[:8]
        )
        raise RuntimeError(
            f"{file_name}: SVG cells did not match GitHub calendar days: {preview}. "
            "No SVG files were written."
        )

    if matched_cells == 0:
        raise RuntimeError(
            f"{file_name}: no animated contribution cells were found. "
            "The generated SVG structure may have changed."
        )

    print(
        f"{file_name}: updated {matched_cells} contribution cell(s) "
        f"using {len(replaced_colors)} exact GitHub day colour(s)."
    )
    return updated, matched_cells


def main() -> int:
    username = os.environ.get("GITHUB_USER_NAME", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    dist_dir = Path("dist")

    if not username or not token:
        print(
            "GITHUB_USER_NAME and GITHUB_TOKEN are required.",
            file=sys.stderr,
        )
        return 2

    if not dist_dir.is_dir():
        print(
            "The generator output directory 'dist/' was not found.",
            file=sys.stderr,
        )
        return 2

    svg_files = sorted(dist_dir.glob("*.svg"))
    if not svg_files:
        print("No generated SVG files were found in dist/.", file=sys.stderr)
        return 2

    try:
        calendar = fetch_contribution_calendar(username, token)
        day_colors = build_day_color_map(calendar)
        palette = calendar.get("colors", [])
        print(
            "Fetched GitHub's live contribution calendar: "
            f"{len(calendar['weeks'])} week(s), "
            f"{len(day_colors)} day(s), "
            f"{len(palette)} palette colour(s), "
            f"isHalloween={calendar.get('isHalloween', False)}."
        )

        # Prepare all SVGs in memory first. If one file cannot be mapped,
        # leave every output file untouched.
        prepared: list[tuple[Path, str, int]] = []

        for svg_path in svg_files:
            original = svg_path.read_text(encoding="utf-8")
            updated, count = recolor_svg(
                original,
                day_colors,
                svg_path.name,
            )
            updated, removed_label = remove_duplicate_boundary_month_label(updated)
            if removed_label:
                print(
                    f"{svg_path.name}: removed duplicate leading month label."
                )
            prepared.append((svg_path, updated, count))

    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        print(
            f"Could not synchronize Pac-Man colours: {exc}",
            file=sys.stderr,
        )
        return 1

    for svg_path, updated, _ in prepared:
        svg_path.write_text(updated, encoding="utf-8")
        print(f"Wrote {svg_path}.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
