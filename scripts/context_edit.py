#!/usr/bin/env python3
"""Interactive context editor for Claude Code sessions.

Loads a `~/.claude/projects/<encoded>/<sid>.jsonl`, lets the user toggle
individual blocks on/off in a curses TUI, and writes a new (edited) session
file that `claude --resume` can pick up.

Usage:
    context_edit.py                            # auto-pick latest session in cwd
    context_edit.py path/to/session.jsonl
    context_edit.py --auto                     # non-interactive, heuristic
    context_edit.py --auto --drop-tool-results-larger-than 5000
    context_edit.py --dry-run                  # show what would be hidden

The output file is written to the SAME directory as the source jsonl so
`claude --resume` can find it. The original file is never modified.
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

# Make session_model importable when called directly.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from session_model import (  # noqa: E402
    BLOCK_TEXT,
    BLOCK_THINKING,
    BLOCK_TOOL_RESULT,
    BLOCK_TOOL_USE,
    BLOCK_USER_INPUT,
    Merge,
    Session,
    apply_edits_and_save,
    auto_mark,
    build_merge_prompt,
    count_tokens,
    resolve_merge_range,
)


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
ASK_LLM_PATH = Path.home() / "ask_llm.py"


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def encode_cwd(cwd: Path) -> str:
    return str(cwd.resolve()).replace("/", "-")


def _project_cwd_from_session(session: Session) -> str | None:
    """Pull the original working directory from any record that carries it.

    Each conversation record stores a `cwd` field (the project the user was
    in when the session was created). The encoded directory-name form is
    lossy, so we use this canonical value instead.
    """
    for rec in session.records:
        cwd = rec.get("cwd")
        if cwd:
            return cwd
    return None


def latest_session_for_cwd(cwd: Path) -> Path | None:
    project_dir = CLAUDE_PROJECTS_DIR / encode_cwd(cwd)
    if not project_dir.is_dir():
        return None
    candidates = [
        p
        for p in project_dir.glob("*.jsonl")
        # Skip our own edited outputs by default; user can pass --include-edited
        if "edit-manifest" not in p.name
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------


KIND_LABEL = {
    BLOCK_USER_INPUT: "user",
    BLOCK_TEXT: "asst",
    BLOCK_THINKING: "think",
    BLOCK_TOOL_USE: "call",
    BLOCK_TOOL_RESULT: "result",
}


KIND_COLOR = {
    BLOCK_USER_INPUT: 1,
    BLOCK_TEXT: 2,
    BLOCK_THINKING: 5,
    BLOCK_TOOL_USE: 4,
    BLOCK_TOOL_RESULT: 3,
}


def run_tui(session: Session, merge_model: str = "gemini") -> bool:
    """Returns True if the user chose to save."""

    state = {"saved": False, "merge_model": merge_model}

    def _main(stdscr):
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        try:
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_WHITE, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_MAGENTA, -1)
            curses.init_pair(5, curses.COLOR_BLUE, -1)
            curses.init_pair(6, curses.COLOR_GREEN, -1)
            curses.init_pair(7, curses.COLOR_RED, -1)
            curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_WHITE)
        except curses.error:
            pass

        cursor = 0
        scroll = 0
        visual_anchor: int | None = None
        message = "↑↓ move · space toggle · enter fold/preview · v visual · m merge · M auto-merge-turns · a all results · A thinking · ? help · s save · q quit"

        while True:
            rows = _build_visible_rows(session)
            h, w = stdscr.getmaxyx()
            body_height = h - 3  # header(1) + status(1) + help(1)

            if cursor < 0:
                cursor = 0
            if cursor >= len(rows):
                cursor = max(0, len(rows) - 1)
            if cursor < scroll:
                scroll = cursor
            if cursor >= scroll + body_height:
                scroll = cursor - body_height + 1

            sel_lo, sel_hi = None, None
            if visual_anchor is not None:
                sel_lo = min(visual_anchor, cursor)
                sel_hi = max(visual_anchor, cursor)

            stdscr.erase()

            # Header
            st = session.stats()
            hdr_left = f" sculptor · {session.path.name}"
            hdr_right = (
                f"{st['kept_blocks']}/{st['total_blocks']} kept · "
                f"{st.get('merge_groups', 0)} merges · "
                f"{st['tokens_kept']:,} tok (-{st['tokens_hidden']:,})"
            )
            hdr = (hdr_left + "  " + hdr_right).ljust(w)
            _safe_addstr(stdscr, 0, 0, hdr, w, curses.A_REVERSE)

            # Body
            for i in range(body_height):
                idx = scroll + i
                if idx >= len(rows):
                    break
                row = rows[idx]
                in_visual = (
                    sel_lo is not None and sel_lo <= idx <= sel_hi
                )
                _draw_row(
                    stdscr,
                    i + 1,
                    w,
                    row,
                    selected=(idx == cursor),
                    in_visual=in_visual,
                )

            # Status
            if visual_anchor is not None:
                sel_record_count = _count_records_in_selection(rows, sel_lo, sel_hi)
                status = (
                    f"VISUAL · {sel_hi - sel_lo + 1} rows / "
                    f"{sel_record_count} records · "
                    f"press m to merge into LLM summary · esc to cancel"
                )
                _safe_addstr(stdscr, h - 2, 0, status.ljust(w), w, curses.A_REVERSE)
            else:
                _safe_addstr(stdscr, h - 2, 0, message, w)

            help_line = (
                "  space:toggle  v:visual  m:merge  M:merge-all-turns  "
                "a:all results  A:thinking  p:preview  s:save  q:quit"
            )
            _safe_addstr(stdscr, h - 1, 0, help_line.ljust(w), w, curses.A_DIM)

            stdscr.refresh()

            k = stdscr.getch()

            if k == 27:  # esc
                if visual_anchor is not None:
                    visual_anchor = None
                    message = "Visual mode cancelled."
                else:
                    if _confirm(stdscr, "Quit without saving? [y/N] "):
                        return
            elif k == ord("q"):
                if _confirm(stdscr, "Quit without saving? [y/N] "):
                    return
            elif k in (curses.KEY_UP, ord("k")):
                cursor -= 1
            elif k in (curses.KEY_DOWN, ord("j")):
                cursor += 1
            elif k == curses.KEY_PPAGE:
                cursor -= body_height
            elif k == curses.KEY_NPAGE:
                cursor += body_height
            elif k == curses.KEY_HOME:
                cursor = 0
            elif k == curses.KEY_END:
                cursor = len(rows) - 1
            elif k == ord(" "):
                if visual_anchor is not None:
                    message = "Toggle disabled in visual mode. Press esc first."
                else:
                    r = rows[cursor]
                    if r["kind"] == "block":
                        blk = r["block"]
                        if not blk.locked:
                            blk.keep = not blk.keep
            elif k in (curses.KEY_ENTER, 10, 13):
                if visual_anchor is None:
                    r = rows[cursor]
                    if r["kind"] == "turn_header":
                        t = r["turn"]
                        t.expanded = not t.expanded
                    else:
                        _preview_block(stdscr, rows, cursor)
            elif k == ord("v"):
                if visual_anchor is None:
                    visual_anchor = cursor
                    message = "Visual mode: extend with ↑↓, m to merge, esc to cancel"
                else:
                    visual_anchor = None
                    message = "Visual mode cancelled."
            elif k == ord("m"):
                if visual_anchor is None:
                    message = "Press v first to enter visual mode, then m to merge."
                else:
                    sel_record_indices = _selected_record_indices(rows, sel_lo, sel_hi)
                    visual_anchor = None
                    try:
                        result = _do_merge(
                            stdscr, session, sel_record_indices, state["merge_model"]
                        )
                        message = result
                    except _UserAbort:
                        message = "Merge cancelled."
                    except Exception as exc:  # noqa: BLE001
                        message = f"Merge failed: {exc}"
            elif k == ord("M"):
                try:
                    message = _do_merge_all_turns(
                        stdscr, session, state["merge_model"]
                    )
                except Exception as exc:  # noqa: BLE001
                    message = f"Merge-all failed: {exc}"
            elif k == ord("a"):
                _bulk_toggle(session, BLOCK_TOOL_RESULT)
            elif k == ord("A"):
                _bulk_toggle(session, BLOCK_THINKING)
            elif k == ord("T"):
                _bulk_toggle(session, BLOCK_TEXT)
            elif k == ord("p"):
                _preview_block(stdscr, rows, cursor)
            elif k == ord("?"):
                _show_help(stdscr)
            elif k == ord("s"):
                if _confirm(stdscr, "Save edited session to new jsonl? [y/N] "):
                    state["saved"] = True
                    return
            elif k == ord("u"):
                for b in session.blocks:
                    if not b.locked:
                        b.keep = True
                # Also drop any pending merges? Be conservative and keep them
                # since merges represent significant work (an LLM call). User
                # can drop a specific merge with future commands.
                message = "All hidden blocks restored to keep."

    try:
        curses.wrapper(_main)
    except KeyboardInterrupt:
        return False
    return state["saved"]


class _UserAbort(Exception):
    pass


def _build_visible_rows(session: Session) -> list[dict]:
    """Build the displayable rows. Records swallowed by a merge are folded
    behind a single 'Σ' summary row (one per merge group) so the user can see
    the result of their merges without the original noise."""
    rows: list[dict] = []
    merged_record_to_merge: dict[int, "Merge"] = {}
    for m in session.merges:
        for ri in m.record_indices:
            merged_record_to_merge[ri] = m
    rendered_merges: set[int] = set()

    for t in session.turns:
        if t.user_block is not None:
            rows.append({"kind": "turn_header", "turn": t, "block": t.user_block})
        if not t.expanded:
            continue
        for b in t.blocks:
            if b.record_index in merged_record_to_merge:
                m = merged_record_to_merge[b.record_index]
                key = id(m)
                if key in rendered_merges:
                    continue
                rendered_merges.add(key)
                # Build a fake block describing the merge group.
                from session_model import Block as _Block  # local import to avoid cycle

                summary_preview = (m.summary_text or "").replace("\n", " ").strip()
                fake = _Block(
                    record_index=b.record_index,
                    block_index=-1,
                    kind=BLOCK_TEXT,
                    keep=True,
                    locked=True,
                    preview=f"[merged {len(m.record_indices)} records] {summary_preview[:160]}",
                    size_chars=len(m.summary_text),
                )
                rows.append(
                    {
                        "kind": "merged",
                        "block": fake,
                        "turn": t,
                        "merge": m,
                    }
                )
            else:
                rows.append({"kind": "block", "block": b, "turn": t})
    return rows


def _selected_record_indices(rows: list[dict], lo: int, hi: int) -> set[int]:
    """Map visible-row range to underlying record_indices."""
    out: set[int] = set()
    for idx in range(lo, hi + 1):
        if idx >= len(rows):
            break
        row = rows[idx]
        if row["kind"] == "block":
            out.add(row["block"].record_index)
        elif row["kind"] == "turn_header":
            # A turn header IS the user input record — adding it would make
            # the merge cross a user input. Skip; resolve_merge_range will
            # reject if user input is actually inside [min..max].
            out.add(row["block"].record_index)
        elif row["kind"] == "merged":
            # Already merged — can't re-merge into another group from here.
            raise ValueError(
                "Selection includes a previously-merged block (Σ row). "
                "Cancel and pick a fresh range."
            )
    return out


def _count_records_in_selection(rows: list[dict], lo: int, hi: int) -> int:
    seen: set[int] = set()
    for idx in range(lo, min(hi + 1, len(rows))):
        row = rows[idx]
        if "block" in row:
            seen.add(row["block"].record_index)
    return len(seen)


def _do_merge(
    stdscr,
    session: Session,
    record_indices: set[int],
    merge_model: str,
) -> str:
    """Validate + confirm + LLM-merge a user-selected range. Returns status."""
    if not record_indices:
        return "Empty selection; nothing to merge."

    try:
        indices, warnings = resolve_merge_range(session, record_indices)
    except ValueError as exc:
        return f"Merge rejected: {exc}"

    if len(indices) < 2:
        return "Merge needs at least 2 records."

    prompt = build_merge_prompt(session, indices)
    if len(prompt) > 400_000:
        return (
            f"Selection too large ({len(prompt):,} prompt chars). "
            f"Try a smaller range."
        )

    confirm_msg = (
        f"Merge {len(indices)} records (~{count_tokens(prompt):,} tokens → "
        f"{merge_model})? [y/N] "
    )
    if not _confirm(stdscr, confirm_msg):
        raise _UserAbort()

    _flash_status(
        stdscr,
        f"Calling LLM ({merge_model}) on {len(indices)} records "
        f"({len(prompt):,} chars)…",
    )

    summary = _execute_llm_merge(session, indices, prompt, merge_model)
    if summary is None:
        return "LLM returned empty response; merge aborted."

    warn_suffix = f" ({'; '.join(warnings)})" if warnings else ""
    return (
        f"✓ merged {len(indices)} records into {len(summary):,}-char "
        f"summary{warn_suffix}"
    )


def _execute_llm_merge(
    session: Session,
    indices: list[int],
    prompt: str,
    merge_model: str,
) -> str | None:
    """Call the LLM and append the resulting Merge to session. Returns the
    raw summary text, or None if the LLM returned nothing."""
    summary = _call_llm(prompt, merge_model)
    if not summary or not summary.strip():
        return None
    summary_clean = summary.strip()
    parent = session.records[indices[0]].get("parentUuid")
    session.merges.append(
        Merge(
            record_indices=indices,
            summary_text=summary_clean,
            summary_tokens=count_tokens(summary_clean),
            new_uuid=str(uuid.uuid4()),
            insertion_parent_uuid=parent,
            label=f"merged {len(indices)} records",
        )
    )
    return summary_clean


def _flash_status(stdscr, text: str) -> None:
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 2, 0, text.ljust(w), w, curses.A_REVERSE)
    stdscr.refresh()


def _eligible_turns_for_auto_merge(
    session: Session, min_tokens: int
) -> list[tuple[int, list[int], int]]:
    """For each turn that contains at least 2 mergeable assistant/tool_result
    records totaling >= min_tokens, return (turn_index, record_indices, tokens).
    Skips records that are already part of a saved merge."""
    already_merged: set[int] = set()
    for m in session.merges:
        already_merged.update(m.record_indices)

    eligible: list[tuple[int, list[int], int]] = []
    for turn in session.turns:
        if turn.user_block is None:
            continue
        rec_indices: list[int] = []
        rec_tokens = 0
        seen: set[int] = set()
        for b in turn.blocks:
            ri = b.record_index
            if ri in already_merged or ri in seen:
                continue
            seen.add(ri)
            rec_indices.append(ri)
            rec_tokens += sum(
                bb.size_tokens for bb in turn.blocks if bb.record_index == ri
            )
        if len(rec_indices) >= 2 and rec_tokens >= min_tokens:
            eligible.append((turn.index, rec_indices, rec_tokens))
    return eligible


def _do_merge_all_turns(
    stdscr,
    session: Session,
    merge_model: str,
    min_tokens: int = 1500,
) -> str:
    """One LLM call per eligible turn. Skips turns smaller than min_tokens
    and turns whose records are already inside an existing merge."""
    eligible = _eligible_turns_for_auto_merge(session, min_tokens)
    if not eligible:
        return (
            f"No eligible turns (need ≥2 records and ≥{min_tokens:,} tokens of "
            f"assistant content per turn)."
        )

    total_tokens = sum(t[2] for t in eligible)
    confirm_msg = (
        f"Merge {len(eligible)} turn(s) (~{total_tokens:,} tokens total → "
        f"{len(eligible)}× {merge_model} calls)? [y/N] "
    )
    if not _confirm(stdscr, confirm_msg):
        return "Merge-all cancelled."

    done = 0
    failed = 0
    for n, (turn_idx, rec_indices, tokens) in enumerate(eligible, 1):
        try:
            indices, _warnings = resolve_merge_range(session, set(rec_indices))
        except ValueError as exc:
            failed += 1
            _flash_status(
                stdscr, f"turn {turn_idx}: skipped ({exc}) [{n}/{len(eligible)}]"
            )
            continue
        if len(indices) < 2:
            continue
        prompt = build_merge_prompt(session, indices)
        if len(prompt) > 400_000:
            failed += 1
            _flash_status(
                stdscr,
                f"turn {turn_idx}: too large ({len(prompt):,} chars), skipped "
                f"[{n}/{len(eligible)}]",
            )
            continue
        _flash_status(
            stdscr,
            f"[{n}/{len(eligible)}] turn {turn_idx}: calling {merge_model} on "
            f"{len(indices)} records (~{tokens:,} tok)…",
        )
        try:
            summary = _execute_llm_merge(session, indices, prompt, merge_model)
            if summary is None:
                failed += 1
            else:
                done += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _flash_status(
                stdscr,
                f"turn {turn_idx}: LLM error ({exc}) [{n}/{len(eligible)}]",
            )

    return f"✓ merged {done} turns" + (f", {failed} failed/skipped" if failed else "")


def _call_llm(prompt: str, model: str) -> str:
    """Run ~/ask_llm.py as a subprocess. Returns the LLM's response."""
    if not ASK_LLM_PATH.is_file():
        raise RuntimeError(
            f"{ASK_LLM_PATH} not found. sculptor needs ~/ask_llm.py "
            f"(the ask-gemini backend) to do LLM-powered merges."
        )
    if not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError(
            "ANTHROPIC_AUTH_TOKEN env var not set; ask_llm.py needs it."
        )
    proc = subprocess.run(
        [sys.executable, str(ASK_LLM_PATH), "--model", model, "--stdin"],
        input=prompt,
        text=True,
        capture_output=True,
        timeout=300,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:500]
        raise RuntimeError(f"ask_llm.py exit {proc.returncode}: {err}")
    return proc.stdout


def _draw_row(
    stdscr,
    y: int,
    w: int,
    row: dict,
    selected: bool,
    in_visual: bool = False,
) -> None:
    blk = row["block"]
    is_header = row["kind"] == "turn_header"
    is_merged = row.get("kind") == "merged"

    arrow = "▼" if is_header and row["turn"].expanded else ("▶" if is_header else " ")
    if is_merged:
        mark = "[Σ]"
    elif blk.locked:
        mark = "[🔒]"
    elif blk.keep:
        mark = "[✓]"
    else:
        mark = "[ ]"
    kind_label = KIND_LABEL.get(blk.kind, blk.kind[:6]).ljust(6)
    size = f"{blk.size_tokens:>6}t"
    preview = blk.preview or ""

    indent = "" if is_header else "  "
    line = f"{arrow} {mark} {kind_label} {size}  {indent}{preview}"

    attr = curses.A_NORMAL
    if selected:
        attr |= curses.A_REVERSE
    elif in_visual:
        attr |= curses.A_STANDOUT
    if not blk.keep and not blk.locked and not is_merged:
        attr |= curses.A_DIM
    if is_merged:
        attr |= curses.A_BOLD

    color = curses.color_pair(KIND_COLOR.get(blk.kind, 2))
    if is_merged:
        color = curses.color_pair(6)  # green for synthetic merged
    _safe_addstr(stdscr, y, 0, line.ljust(w), w, attr | color)


def _safe_addstr(win, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    """addnstr that swallows the standard "wrote past last cell" error."""
    if width <= 0:
        return
    # Avoid writing into the very last cell of the bottom-right corner, which
    # raises in some curses backends. Reserve one cell.
    try:
        win.addnstr(y, x, text, max(0, width - 1), attr)
    except curses.error:
        pass


def _bulk_toggle(session: Session, kind: str) -> None:
    """If any block of this kind is currently kept-and-not-locked, hide all of
    them. Otherwise restore all of them to kept."""
    candidates = [b for b in session.blocks if b.kind == kind and not b.locked]
    if not candidates:
        return
    any_kept = any(b.keep for b in candidates)
    target = not any_kept  # hide if any kept, restore otherwise
    for b in candidates:
        b.keep = target


def _confirm(stdscr, prompt: str) -> bool:
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 2, 0, prompt.ljust(w), w, curses.A_REVERSE)
    stdscr.refresh()
    k = stdscr.getch()
    return k in (ord("y"), ord("Y"))


def _preview_block(stdscr, rows: list[dict], cursor: int) -> None:
    if cursor >= len(rows):
        return
    row = rows[cursor]
    if row["kind"] == "merged":
        m = row["merge"]
        text = (
            f"[merged {len(m.record_indices)} records → LLM summary "
            f"({len(m.summary_text)} chars)]\n\n{m.summary_text}"
        )
        blk = row["block"]
    else:
        blk = row["block"]
        raw = blk.raw
        text = _full_text_of_block(blk, raw)
    h, w = stdscr.getmaxyx()
    pad_h = max(50, len(text.splitlines()) + 4)
    pad_w = max(w, 200)
    pad = curses.newpad(pad_h, pad_w)
    pad.addstr(0, 0, f"=== {blk.kind} · {blk.size_tokens} tok · {blk.size_chars} chars ===\n\n")
    try:
        pad.addstr(text[: pad_h * pad_w - 200])
    except curses.error:
        pass

    pos = 0
    while True:
        stdscr.erase()
        _safe_addstr(stdscr, 0, 0, " preview — q to close, ↑↓ to scroll".ljust(w), w, curses.A_REVERSE)
        stdscr.refresh()
        try:
            pad.refresh(pos, 0, 1, 0, h - 1, w - 1)
        except curses.error:
            pass
        k = stdscr.getch()
        if k in (ord("q"), 27):
            return
        elif k in (curses.KEY_UP, ord("k")):
            pos = max(0, pos - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            pos = min(pad_h - (h - 1), pos + 1)
        elif k == curses.KEY_PPAGE:
            pos = max(0, pos - (h - 2))
        elif k == curses.KEY_NPAGE:
            pos = min(max(0, pad_h - (h - 1)), pos + (h - 2))


def _full_text_of_block(blk, raw) -> str:
    if blk.kind == BLOCK_USER_INPUT:
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            return raw.get("text") or ""
    if blk.kind == BLOCK_TEXT and isinstance(raw, dict):
        return raw.get("text") or ""
    if blk.kind == BLOCK_THINKING and isinstance(raw, dict):
        return raw.get("thinking") or raw.get("text") or ""
    if blk.kind == BLOCK_TOOL_USE and isinstance(raw, dict):
        return f"{raw.get('name')}\n\n{json.dumps(raw.get('input') or {}, ensure_ascii=False, indent=2)}"
    if blk.kind == BLOCK_TOOL_RESULT and isinstance(raw, dict):
        c = raw.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for it in c:
                if isinstance(it, dict):
                    if it.get("type") == "text":
                        parts.append(it.get("text") or "")
                    elif it.get("type") == "image":
                        parts.append("[image]")
                    else:
                        parts.append(f"[{it.get('type')}]")
            return "\n".join(parts)
    return repr(raw)[:5000]


def _show_help(stdscr) -> None:
    h, w = stdscr.getmaxyx()
    lines = [
        "sculptor help",
        "",
        "Movement:",
        "  ↑/↓ or j/k       move cursor",
        "  pgup/pgdn        page up/down",
        "  home/end         jump to top/bottom",
        "",
        "Hide/keep:",
        "  space            toggle keep/hide on current block",
        "  a                bulk toggle ALL tool_results",
        "  A                bulk toggle ALL thinking",
        "  T                bulk toggle ALL assistant text",
        "  u                restore everything (un-hide all)",
        "",
        "Merge (LLM summary):",
        "  v                start/cancel VISUAL selection at cursor",
        "  ↑/↓ (in visual)  extend selection",
        "  m (in visual)    merge selected records → LLM summary",
        "  M                auto-merge every eligible user turn (one LLM call",
        "                   per turn; min 1500 tokens of assistant content)",
        "  esc              cancel visual mode",
        "",
        "View:",
        "  enter on header  expand/collapse the current user turn",
        "  enter on block   preview full content (also bound to p)",
        "  p                preview full content of current block (or merged Σ)",
        "  ?                this help",
        "",
        "Save/quit:",
        "  s                save edited session to new jsonl",
        "  q or esc         quit (asks for confirmation)",
        "",
        "Legend:",
        "  [✓] kept    [ ] hidden    [🔒] locked    [Σ] merged group",
        "",
        "On save, a NEW jsonl is written to the same project directory with",
        "a fresh sessionId. The original file is never touched. Run",
        "    claude --resume",
        "in the same project to pick up the edited session.",
        "",
        "Press any key to dismiss.",
    ]
    stdscr.erase()
    for i, ln in enumerate(lines[:h]):
        _safe_addstr(stdscr, i, 0, ln, w)
    stdscr.refresh()
    stdscr.getch()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Interactive context editor for Claude Code sessions."
    )
    p.add_argument("input", nargs="?", help="Path to a .jsonl session file.")
    p.add_argument(
        "--out-dir",
        help="Write the edited jsonl here (defaults to same directory as input).",
    )
    p.add_argument("--auto", action="store_true", help="Non-interactive heuristic mode.")
    p.add_argument(
        "--drop-tool-results-larger-than",
        type=int,
        default=0,
        help="Auto: hide tool_results whose payload is >= N chars.",
    )
    p.add_argument(
        "--drop-thinking", action="store_true", help="Auto: hide all thinking blocks."
    )
    p.add_argument(
        "--drop-failed-bash",
        action="store_true",
        help="Auto: hide tool_results that look like errors.",
    )
    p.add_argument(
        "--merge-turns",
        action="store_true",
        help="Auto: for every user turn with assistant content above "
        "--merge-turns-min-tokens, send the whole turn to the merge LLM and "
        "replace it with one synthetic summary record. Implies --auto.",
    )
    p.add_argument(
        "--merge-turns-min-tokens",
        type=int,
        default=1500,
        help="Minimum token count of a turn's assistant content for "
        "--merge-turns to consider it (default: 1500).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write anything; just print what would change.",
    )
    p.add_argument(
        "--print-path",
        action="store_true",
        help="Print only the new jsonl path on success (for shell scripting).",
    )
    p.add_argument(
        "--merge-model",
        default="gemini",
        help="Model preset passed to ~/ask_llm.py when you merge records "
        "(default: gemini). Accepts any preset ask_llm.py supports, e.g. "
        "gemini, gpt, or a raw model name.",
    )
    return p.parse_args()


def _cli_merge_all_turns(
    session: Session, *, merge_model: str, min_tokens: int, quiet: bool
) -> int:
    """Non-interactive per-turn auto merge. Returns number of merges added."""
    eligible = _eligible_turns_for_auto_merge(session, min_tokens)
    if not eligible:
        if not quiet:
            print(
                f"No eligible turns for --merge-turns (need ≥2 records and "
                f"≥{min_tokens:,} tokens of assistant content).",
                file=sys.stderr,
            )
        return 0

    done = 0
    for n, (turn_idx, rec_indices, tokens) in enumerate(eligible, 1):
        try:
            indices, _w = resolve_merge_range(session, set(rec_indices))
        except ValueError as exc:
            if not quiet:
                print(
                    f"[{n}/{len(eligible)}] turn {turn_idx}: skipped ({exc})",
                    file=sys.stderr,
                )
            continue
        if len(indices) < 2:
            continue
        prompt = build_merge_prompt(session, indices)
        if len(prompt) > 400_000:
            if not quiet:
                print(
                    f"[{n}/{len(eligible)}] turn {turn_idx}: too large "
                    f"({len(prompt):,} chars), skipped",
                    file=sys.stderr,
                )
            continue
        if not quiet:
            print(
                f"[{n}/{len(eligible)}] turn {turn_idx}: calling {merge_model} "
                f"on {len(indices)} records (~{tokens:,} tok)…",
                file=sys.stderr,
            )
        try:
            summary = _execute_llm_merge(session, indices, prompt, merge_model)
            if summary is not None:
                done += 1
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(
                    f"[{n}/{len(eligible)}] turn {turn_idx}: LLM error ({exc})",
                    file=sys.stderr,
                )
    return done


def main() -> int:
    args = parse_args()
    if args.input:
        path = Path(args.input).expanduser().resolve()
    else:
        latest = latest_session_for_cwd(Path.cwd())
        if not latest:
            print(
                f"No session found for {Path.cwd()}. "
                f"Pass a path explicitly.",
                file=sys.stderr,
            )
            return 1
        path = latest

    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 1

    session = Session.load(path)
    if not args.print_path:
        print(
            f"Loaded {path}\n"
            f"  records: {len(session.records)}  blocks: {len(session.blocks)}  "
            f"turns: {len(session.turns)}\n"
            f"  tokens: {session.stats()['tokens_kept']:,}",
            file=sys.stderr,
        )

    if args.auto or args.merge_turns:
        n = auto_mark(
            session,
            drop_tool_results_larger_than=args.drop_tool_results_larger_than,
            drop_thinking=args.drop_thinking,
            drop_failed_bash=args.drop_failed_bash,
        )
        merged_count = 0
        if args.merge_turns:
            merged_count = _cli_merge_all_turns(
                session,
                merge_model=args.merge_model,
                min_tokens=args.merge_turns_min_tokens,
                quiet=args.print_path,
            )
        if not args.print_path:
            print(
                f"Auto-marked {n} blocks for hide; merged {merged_count} turns.",
                file=sys.stderr,
            )
        do_save = (n > 0) or (merged_count > 0)
    else:
        do_save = run_tui(session, merge_model=args.merge_model)

    if args.dry_run:
        st = session.stats()
        print(json.dumps(st, indent=2))
        return 0

    if not do_save:
        if not args.print_path:
            print("No changes saved.", file=sys.stderr)
        return 0

    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else path.parent
    result = apply_edits_and_save(session, out_dir=out_dir)

    if args.print_path:
        print(result["out_jsonl"])
    else:
        st = result["stats"]
        resume_cwd = _project_cwd_from_session(session)
        new_sid = Path(result["out_jsonl"]).stem
        resume_cmd = f"claude --resume {new_sid}"
        resume_hint = (
            f"cd {resume_cwd} && {resume_cmd}" if resume_cwd else resume_cmd
        )
        print(
            f"\n✓ wrote {result['out_jsonl']}\n"
            f"  manifest: {result['manifest']}\n"
            f"  dropped {result['dropped_record_count']} records, "
            f"stubbed {result['modified_block_count']} blocks\n"
            f"  {st['tokens_kept']:,} tok kept  "
            f"(-{st['tokens_hidden']:,} tok hidden)\n"
            f"\nResume with:  {resume_hint}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
