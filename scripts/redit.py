#!/usr/bin/env python3
"""redit.py — range-based Edit helper.

Why this exists
---------------
Claude Code's built-in `Edit` tool requires `old_string` to be the **exact full
text** of what you want to replace. That means deleting an N-byte region costs:

  - N output tokens for the agent to "type out" the region as the tool argument
  - N input tokens for every subsequent turn in the session (the tool_use
    record carrying that N-byte string stays in context forever)

For large markdown editing (e.g. trimming 50KB sections in a sculptor edit.md)
this is catastrophically expensive: deleting 1MB worth of sections costs ~250k
output tokens AND adds ~250k tokens to every future API call in the session.

`redit.py` solves this by taking only the **short boundary markers** (a prefix
and a suffix) and computing the region itself locally:

    redit.py <file> --start "<prefix>" --end "<suffix>" [--new "<replacement>"]

Boundary markers are usually a few dozen tokens; the region itself never has
to flow through the LLM. Deleting a 50KB section now costs ~30 tokens instead
of ~12500.

Safety
------
The tool fails fast unless the operation is unambiguous:

  - `--start` must occur **exactly once** in the whole file
  - `--end` must occur **at least once after `--start`** (the first occurrence
    after `--start` is used)
  - If `--end` matches multiple times after `--start`, a warning is printed
    (the first match is still used; pass a more specific marker if that's
    not what you want, or use --dry-run to preview)

The region includes both markers in the deletion. The replacement (`--new`,
default empty) is inserted in their place.

Usage
-----
Delete a sculptor section by anchor + next-heading boundary (default behavior:
--start is included in the deletion, --end is kept as a sentinel for the next
section):

    redit.py edit.md \\
      --start "### turn 5 · think · b0123" \\
      --end "### turn 5 · asst · b0124"

Preview first (no write):

    redit.py edit.md --start "..." --end "..." --dry-run

Replace a region instead of deleting (still keeps --end):

    redit.py file.md --start "BEGIN" --end "END" --new "[redacted]"

Flags to override marker inclusion:
  --include-end       also delete --end marker (rare, usually not what you want)
  --exclude-start     keep --start marker (only delete content after it)

Boundaries with newlines / special chars: use bash $'...\\n...' or pass via
--start-file / --end-file (reads from a file instead of CLI arg).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _read_marker(arg_value: str | None, arg_file: str | None, name: str) -> str:
    if arg_value is None and arg_file is None:
        print(f"error: must supply --{name} or --{name}-file", file=sys.stderr)
        sys.exit(2)
    if arg_value is not None and arg_file is not None:
        print(f"error: --{name} and --{name}-file are mutually exclusive", file=sys.stderr)
        sys.exit(2)
    if arg_file is not None:
        return Path(arg_file).expanduser().read_text(encoding="utf-8")
    return arg_value or ""


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("file", help="File to modify in place (use --dry-run to preview)")
    p.add_argument("--start", help="Start boundary marker (must occur exactly once)")
    p.add_argument("--start-file", help="Read --start from this file instead")
    p.add_argument("--end", help="End boundary marker (first occurrence after --start)")
    p.add_argument("--end-file", help="Read --end from this file instead")
    p.add_argument(
        "--new",
        default=None,
        help="Replacement text (default: empty = delete the region including markers)",
    )
    p.add_argument(
        "--new-file",
        help="Read --new from this file instead",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write; print what would change with a region preview",
    )
    p.add_argument(
        "--include-end",
        action="store_true",
        help="Also delete the --end marker itself (default: --end is treated as "
        "a sentinel and kept, since typically it marks the start of the NEXT "
        "section that should be preserved)",
    )
    p.add_argument(
        "--exclude-start",
        action="store_true",
        help="Don't delete the --start marker itself (only the content after it)",
    )
    args = p.parse_args()

    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 1

    start = _read_marker(args.start, args.start_file, "start")
    end = _read_marker(args.end, args.end_file, "end")
    new = _read_marker(args.new, args.new_file, "new") if (args.new is not None or args.new_file is not None) else ""

    if not start:
        print("error: --start is empty", file=sys.stderr)
        return 2
    if not end:
        print("error: --end is empty", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")

    # --- start uniqueness check ---
    start_count = text.count(start)
    if start_count == 0:
        print("error: --start marker not found in file", file=sys.stderr)
        return 3
    if start_count > 1:
        # Show first few line numbers for diagnostic
        line_numbers: list[int] = []
        pos = 0
        while True:
            p_ = text.find(start, pos)
            if p_ < 0:
                break
            line_numbers.append(text.count("\n", 0, p_) + 1)
            pos = p_ + 1
            if len(line_numbers) >= 10:
                break
        print(
            f"error: --start matches {start_count} times in file (must be unique).",
            file=sys.stderr,
        )
        print(f"  matched starting at lines: {line_numbers}", file=sys.stderr)
        print(
            "  hint: extend the start marker with more surrounding context "
            "to make it unique.",
            file=sys.stderr,
        )
        return 3

    start_pos = text.index(start)
    after_start = text[start_pos + len(start):]

    # --- end search (after start) ---
    if end not in after_start:
        if end in text:
            print("error: --end found in file but only BEFORE --start", file=sys.stderr)
        else:
            print("error: --end marker not found in file", file=sys.stderr)
        return 4

    end_offset_in_after = after_start.index(end)
    end_pos = start_pos + len(start) + end_offset_in_after
    region_end = end_pos + len(end)

    # warn if --end has multiple matches after --start
    end_total_after = after_start.count(end)
    if end_total_after > 1:
        print(
            f"warning: --end matches {end_total_after} times after --start; "
            "using the FIRST occurrence. Use --dry-run to verify, or pass a "
            "more specific end marker.",
            file=sys.stderr,
        )

    # --- decide region to replace ---
    # Defaults:
    #   --start is INCLUDED (it's the head of the region being deleted)
    #   --end is EXCLUDED   (it's a sentinel marking the start of the next region,
    #                        which should be preserved)
    # Flags can override either side.
    replace_from = (start_pos + len(start)) if args.exclude_start else start_pos
    replace_to = region_end if args.include_end else end_pos

    region = text[replace_from:replace_to]
    region_chars = len(region)
    region_lines = region.count("\n") + (1 if region else 0)

    new_text = text[:replace_from] + new + text[replace_to:]

    # --- report ---
    delta_chars = region_chars - len(new)
    if args.dry_run:
        print(
            f"DRY RUN: would replace {region_chars} chars ({region_lines} lines) "
            f"with {len(new)} chars (Δ -{delta_chars} chars)"
        )
        print(f"  byte offset: {replace_from} → {replace_to}")
        print(f"  start line:  {text.count(chr(10), 0, replace_from) + 1}")
        print(f"  end line:    {text.count(chr(10), 0, replace_to) + 1}")
        # show region preview (head + tail)
        if region_chars <= 240:
            print("--- region content ---")
            print(region)
            print("--- end of region ---")
        else:
            print("--- region head (first 120 chars) ---")
            print(region[:120])
            print("    ... " f"({region_chars - 240} chars omitted) ..." "    ")
            print("--- region tail (last 120 chars) ---")
            print(region[-120:])
            print("--- end of region ---")
        return 0

    path.write_text(new_text, encoding="utf-8")
    print(
        f"✓ replaced {region_chars} chars ({region_lines} lines) with "
        f"{len(new)} chars (Δ -{delta_chars} chars)"
    )
    print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
