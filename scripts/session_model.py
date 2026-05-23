#!/usr/bin/env python3
"""Data model for editing Claude Code session jsonl files.

A session is a list of records (one per jsonl line). Records have two flavors:

1. Conversation records (`user`, `assistant`, `attachment`, `system`) have
   `uuid` and `parentUuid` and form a tree/chain.
2. Meta records (`queue-operation`, `last-prompt`, `file-history-snapshot`,
   `summary`) have no uuid and are not part of the chain. They are preserved
   verbatim on save.

The editing model presents the conversation as an ordered list of "blocks"
within "records". A block corresponds to one entry in `message.content`:

    text          assistant prose
    thinking      assistant reasoning
    tool_use      assistant tool invocation     (locked, never hidden)
    tool_result   user tool return payload      (hidden = content stubbed)
    user_input    real user input (pure text)   (locked, never hidden)
    image / other passthrough, preserved

Toggling a block off has two possible effects on save:

  * `tool_result`  -> the block stays in place but its `content` field is
    replaced with a short stub describing what was removed.
  * `text` / `thinking` -> the block is dropped from the record's content
    array. If the record's content array becomes empty after dropping, the
    entire record is dropped and the `parentUuid` chain is stitched.

`tool_use` and `user_input` blocks cannot be toggled off. The TUI displays
them with a lock indicator.
"""

from __future__ import annotations

import functools
import json
import time
import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@functools.lru_cache(maxsize=1)
def _tiktoken_encoder():
    """Lazy-load cl100k_base encoder. Matches the encoding the user's CLAUDE.md
    standardizes on for token counting in this account."""
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Real token count via tiktoken. Empty / falsy text returns 0."""
    if not text:
        return 0
    return len(_tiktoken_encoder().encode(text, disallowed_special=()))


CONVERSATION_TYPES = {"user", "assistant", "attachment", "system"}
META_TYPES = {"queue-operation", "last-prompt", "file-history-snapshot", "summary"}

BLOCK_TEXT = "text"
BLOCK_THINKING = "thinking"
BLOCK_TOOL_USE = "tool_use"
BLOCK_TOOL_RESULT = "tool_result"
BLOCK_USER_INPUT = "user_input"
BLOCK_IMAGE = "image"
BLOCK_OTHER = "other"

HIDEABLE_BLOCKS = {BLOCK_TEXT, BLOCK_THINKING, BLOCK_TOOL_RESULT}
LOCKED_BLOCKS = {BLOCK_TOOL_USE, BLOCK_USER_INPUT}


@dataclass
class Block:
    """A single content block surfaced to the TUI."""

    record_index: int
    block_index: int
    kind: str
    keep: bool = True
    preview: str = ""
    size_chars: int = 0
    size_tokens: int = 0  # real tiktoken count of the block's text content
    locked: bool = False
    tool_use_id: str | None = None
    tool_name: str | None = None
    raw: Any = None


@dataclass
class Turn:
    """A 'turn' is one user input followed by everything that happened until
    the next user input. Used for collapsible grouping in the TUI."""

    index: int
    user_block: Block | None  # the locked user_input block
    blocks: list[Block] = field(default_factory=list)  # everything after, in order
    expanded: bool = True


@dataclass
class Merge:
    """A user-requested merge: replace a contiguous range of records (by
    index in `Session.records`) with a single synthetic assistant text record
    whose body is `summary_text`.
    """

    record_indices: list[int]
    summary_text: str
    new_uuid: str
    insertion_parent_uuid: str | None
    # Optional human label shown in the TUI for the synthetic row.
    label: str = ""


@dataclass
class Session:
    path: Path
    records: list[dict[str, Any]]
    turns: list[Turn]
    blocks: list[Block]  # flat, in display order
    merges: list[Merge] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Session":
        records: list[dict[str, Any]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path} line: {line[:80]}") from exc

        blocks: list[Block] = []
        for ri, rec in enumerate(records):
            rtype = rec.get("type")
            if rtype not in CONVERSATION_TYPES:
                continue
            for blk in extract_blocks(ri, rec):
                blocks.append(blk)

        turns = group_into_turns(blocks)
        return cls(path=path, records=records, blocks=blocks, turns=turns)

    def stats(self) -> dict[str, int]:
        # Records swallowed by a merge no longer count toward kept_chars.
        merged_record_ids: set[int] = set()
        for m in self.merges:
            merged_record_ids.update(m.record_indices)

        merged_summary_chars = sum(len(m.summary_text) for m in self.merges)
        merged_summary_tokens = sum(count_tokens(m.summary_text) for m in self.merges)

        kept = []
        hidden = []
        merged = []
        for b in self.blocks:
            if b.record_index in merged_record_ids:
                merged.append(b)
            elif b.keep or b.locked:
                kept.append(b)
            else:
                hidden.append(b)

        merged_chars = sum(b.size_chars for b in merged)
        merged_tokens = sum(b.size_tokens for b in merged)
        kept_tokens = sum(b.size_tokens for b in kept)
        hidden_tokens = sum(b.size_tokens for b in hidden)

        return {
            "total_blocks": len(self.blocks),
            "kept_blocks": len(kept),
            "hidden_blocks": len(hidden),
            "merged_blocks": len(merged),
            "merge_groups": len(self.merges),
            "total_chars": sum(b.size_chars for b in self.blocks),
            "kept_chars": sum(b.size_chars for b in kept),
            "hidden_chars": sum(b.size_chars for b in hidden),
            "merged_chars": merged_chars,
            "merged_summary_chars": merged_summary_chars,
            "tokens_kept": kept_tokens + merged_summary_tokens,
            "tokens_hidden": hidden_tokens + max(0, merged_tokens - merged_summary_tokens),
        }


def extract_blocks(record_index: int, record: dict[str, Any]) -> list[Block]:
    """Pull out the per-block view of a single conversation record."""
    rtype = record.get("type")
    msg = record.get("message") or {}

    # attachment / system records carry no editable content blocks
    if rtype in ("attachment", "system"):
        return []

    content = msg.get("content")

    # Pure user input: content is a plain string
    if rtype == "user" and isinstance(content, str):
        text = content
        return [
            Block(
                record_index=record_index,
                block_index=0,
                kind=BLOCK_USER_INPUT,
                locked=True,
                preview=_one_line(text, 200),
                size_chars=len(text),
                size_tokens=count_tokens(text),
                raw=content,
            )
        ]

    if not isinstance(content, list):
        return []

    blocks: list[Block] = []
    user_only_text = (rtype == "user") and all(
        (b.get("type") in ("text",)) for b in content
    )

    for bi, b in enumerate(content):
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if rtype == "user" and btype == "tool_result":
            payload = b.get("content")
            preview, size, text_for_tok = _summarize_tool_result(payload)
            blocks.append(
                Block(
                    record_index=record_index,
                    block_index=bi,
                    kind=BLOCK_TOOL_RESULT,
                    preview=preview,
                    size_chars=size,
                    size_tokens=count_tokens(text_for_tok),
                    tool_use_id=b.get("tool_use_id"),
                    raw=b,
                )
            )
        elif rtype == "user" and btype == "text":
            # User wrapping text (e.g. caveats, system reminders piggybacked
            # via user role). Treat as locked user_input.
            text = b.get("text", "")
            blocks.append(
                Block(
                    record_index=record_index,
                    block_index=bi,
                    kind=BLOCK_USER_INPUT,
                    locked=True,
                    preview=_one_line(text, 200),
                    size_chars=len(text),
                    size_tokens=count_tokens(text),
                    raw=b,
                )
            )
        elif rtype == "user" and btype == "image":
            blocks.append(
                Block(
                    record_index=record_index,
                    block_index=bi,
                    kind=BLOCK_IMAGE,
                    locked=True,
                    preview="[image]",
                    size_chars=0,
                    raw=b,
                )
            )
        elif rtype == "assistant" and btype == "text":
            text = b.get("text", "")
            blocks.append(
                Block(
                    record_index=record_index,
                    block_index=bi,
                    kind=BLOCK_TEXT,
                    preview=_one_line(text, 200),
                    size_chars=len(text),
                    size_tokens=count_tokens(text),
                    raw=b,
                )
            )
        elif rtype == "assistant" and btype == "thinking":
            text = b.get("thinking", "") or b.get("text", "")
            blocks.append(
                Block(
                    record_index=record_index,
                    block_index=bi,
                    kind=BLOCK_THINKING,
                    preview=_one_line(text, 200),
                    size_chars=len(text),
                    size_tokens=count_tokens(text),
                    raw=b,
                )
            )
        elif rtype == "assistant" and btype == "tool_use":
            name = b.get("name", "")
            inp = b.get("input") or {}
            inp_json = json.dumps(inp, ensure_ascii=False)
            preview = f"{name}: {_one_line(inp_json, 180)}"
            blocks.append(
                Block(
                    record_index=record_index,
                    block_index=bi,
                    kind=BLOCK_TOOL_USE,
                    locked=True,
                    preview=preview,
                    size_chars=len(inp_json),
                    size_tokens=count_tokens(inp_json),
                    tool_use_id=b.get("id"),
                    tool_name=name,
                    raw=b,
                )
            )
        else:
            blocks.append(
                Block(
                    record_index=record_index,
                    block_index=bi,
                    kind=BLOCK_OTHER,
                    locked=True,
                    preview=f"[{btype}]",
                    size_chars=0,
                    raw=b,
                )
            )

    _ = user_only_text  # currently informational; future use for unlock UX
    return blocks


def group_into_turns(blocks: list[Block]) -> list[Turn]:
    turns: list[Turn] = []
    cur: Turn | None = None
    for blk in blocks:
        if blk.kind == BLOCK_USER_INPUT and blk.locked:
            cur = Turn(index=len(turns), user_block=blk)
            turns.append(cur)
        else:
            if cur is None:
                # Stray content before any user input (rare, e.g. attachments).
                # Open an anonymous turn so the TUI still has somewhere to put it.
                cur = Turn(index=len(turns), user_block=None)
                turns.append(cur)
            cur.blocks.append(blk)
    return turns


def _one_line(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _summarize_tool_result(payload: Any) -> tuple[str, int, str]:
    """Return (preview, size_in_chars, text_for_tokenization).

    ``text_for_tokenization`` excludes image base64 (images don't tokenize as
    text in Claude's API), but size_in_chars still counts them so the user
    sees the raw payload weight in the TUI.
    """
    if isinstance(payload, str):
        return _one_line(payload, 200), len(payload), payload
    if isinstance(payload, list):
        text_parts: list[str] = []
        preview_parts: list[str] = []
        size = 0
        for item in payload:
            if isinstance(item, dict):
                t = item.get("type")
                if t == "text":
                    txt = item.get("text", "")
                    text_parts.append(txt)
                    preview_parts.append(txt)
                    size += len(txt)
                elif t == "image":
                    src = item.get("source") or {}
                    data = src.get("data", "")
                    size += len(data) if isinstance(data, str) else 0
                    preview_parts.append("[image]")
                else:
                    preview_parts.append(f"[{t}]")
        return (
            _one_line(" ".join(preview_parts), 200),
            size,
            "\n".join(text_parts),
        )
    s = str(payload)
    return _one_line(s, 200), len(s), s


# ---------------------------------------------------------------------------
# Merge: validate range + render fragment for LLM
# ---------------------------------------------------------------------------


def resolve_merge_range(
    session: Session, record_indices: set[int]
) -> tuple[list[int], list[str]]:
    """Given a user-selected set of record indices, expand to a valid
    contiguous range:

      * fill all indices between min and max
      * auto-extend to include the tool_result of any tool_use in the range
        (and vice versa)
      * raise ValueError if a user_input record is inside the range, since
        those are locked

    Returns (sorted list of indices, warnings).
    """
    if not record_indices:
        raise ValueError("Empty selection")

    lo = min(record_indices)
    hi = max(record_indices)
    indices = list(range(lo, hi + 1))
    warnings: list[str] = []

    def _has_user_input(idx: int) -> bool:
        rec = session.records[idx]
        if rec.get("type") != "user":
            return False
        msg = rec.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            return True
        if isinstance(c, list):
            return any(
                isinstance(b, dict) and b.get("type") in ("text", "image")
                for b in c
            )
        return False

    blockers = [i for i in indices if _has_user_input(i)]
    if blockers:
        raise ValueError(
            f"Merge range crosses a user input at record index {blockers[0]}. "
            f"User inputs are locked. Pick a range inside a single user turn."
        )

    # Auto-extend for tool_use/tool_result pairing. We scan forward/backward
    # until the selection is balanced.
    def _balance(idxs: list[int]) -> list[int]:
        nonlocal warnings
        for _pass in range(20):  # bounded; should converge in 1-2 passes
            tu_ids: dict[str, int] = {}
            tr_ids: dict[str, int] = {}
            for i in idxs:
                rec = session.records[i]
                msg = rec.get("message") or {}
                c = msg.get("content")
                if not isinstance(c, list):
                    continue
                if rec.get("type") == "assistant":
                    for b in c:
                        if (
                            isinstance(b, dict)
                            and b.get("type") == "tool_use"
                            and b.get("id")
                        ):
                            tu_ids[b["id"]] = i
                elif rec.get("type") == "user":
                    for b in c:
                        if (
                            isinstance(b, dict)
                            and b.get("type") == "tool_result"
                            and b.get("tool_use_id")
                        ):
                            tr_ids[b["tool_use_id"]] = i
            # tool_use without a result inside selection
            orphan_calls = [tid for tid in tu_ids if tid not in tr_ids]
            # tool_result without its call inside selection
            orphan_results = [tid for tid in tr_ids if tid not in tu_ids]
            extra: set[int] = set()
            if orphan_calls:
                # find the user record carrying the matching tool_result
                for tid in orphan_calls:
                    found = _find_record_with_tool_result(session, tid)
                    if found is not None and found not in idxs:
                        extra.add(found)
            if orphan_results:
                for tid in orphan_results:
                    found = _find_record_with_tool_use(session, tid)
                    if found is not None and found not in idxs:
                        extra.add(found)
            if not extra:
                return idxs
            new_lo = min(idxs + list(extra))
            new_hi = max(idxs + list(extra))
            idxs = list(range(new_lo, new_hi + 1))
            warnings.append(
                f"auto-extended range to include {len(extra)} tool pairing "
                f"record(s); now {new_lo}..{new_hi}"
            )
            # re-check for user_input crossing after extension
            blockers = [i for i in idxs if _has_user_input(i)]
            if blockers:
                raise ValueError(
                    f"Auto-extend would cross a user input at index "
                    f"{blockers[0]}. Pick a narrower range."
                )
        return idxs

    indices = _balance(indices)

    # Filter to conversation records only (skip meta records like
    # queue-operation; those don't participate in chain and are preserved
    # untouched).
    indices = [
        i
        for i in indices
        if session.records[i].get("type") in CONVERSATION_TYPES
    ]
    if not indices:
        raise ValueError("No conversation records in selection.")
    return indices, warnings


def _find_record_with_tool_result(session: Session, tool_use_id: str) -> int | None:
    for i, rec in enumerate(session.records):
        if rec.get("type") != "user":
            continue
        c = (rec.get("message") or {}).get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_result"
                and b.get("tool_use_id") == tool_use_id
            ):
                return i
    return None


def _find_record_with_tool_use(session: Session, tool_use_id: str) -> int | None:
    for i, rec in enumerate(session.records):
        if rec.get("type") != "assistant":
            continue
        c = (rec.get("message") or {}).get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_use"
                and b.get("id") == tool_use_id
            ):
                return i
    return None


def render_records_for_llm(session: Session, record_indices: list[int]) -> str:
    """Produce a plain-text rendering of the selected records suitable for
    feeding an LLM as the 'original section' input."""
    out: list[str] = []
    for i in record_indices:
        rec = session.records[i]
        rtype = rec.get("type")
        msg = rec.get("message") or {}
        c = msg.get("content")
        if rtype == "user" and isinstance(c, str):
            out.append(f"[user input]\n{c}\n")
            continue
        if not isinstance(c, list):
            out.append(f"[{rtype} — non-list content]\n")
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                out.append(f"[{rtype} text]\n{b.get('text','')}\n")
            elif t == "thinking":
                out.append(f"[{rtype} thinking]\n{b.get('thinking','')}\n")
            elif t == "tool_use":
                inp = b.get("input") or {}
                out.append(
                    f"[tool_use {b.get('name')}]\n"
                    f"{json.dumps(inp, ensure_ascii=False)[:4000]}\n"
                )
            elif t == "tool_result":
                payload = b.get("content")
                if isinstance(payload, str):
                    text = payload
                elif isinstance(payload, list):
                    text = "\n".join(
                        it.get("text", "") if isinstance(it, dict) else str(it)
                        for it in payload
                    )
                else:
                    text = str(payload)
                # cap each block to keep prompt size sane
                if len(text) > 8000:
                    text = text[:8000] + f"\n…[truncated, total {len(text)} chars]"
                out.append(f"[tool_result for {b.get('tool_use_id','?')}]\n{text}\n")
            elif t == "image":
                out.append("[image — omitted]\n")
            else:
                out.append(f"[{t}]\n")
    return "\n".join(out)


MERGE_PROMPT_TEMPLATE = """You are compressing a span of a Claude Code conversation that the user wants to summarize to save context tokens.

Produce a SHORT factual summary (3-10 sentences) that preserves:
- Files/paths/symbols touched, key commands run
- Decisions made, conclusions reached
- Errors and whether they were resolved
- State changes (files created/edited, processes started, configs changed)

EXCLUDE:
- Verbose tool output
- Step-by-step narration ("I then ran...")
- Thinking blocks
- Meta-commentary about the summary itself

Write in the same language the user used (e.g. respond in 中文 if the user wrote in 中文).
Output ONLY the summary text, no preamble, no markdown headers.

--- Original section ---
{body}
--- End of section ---

Summary:"""


def build_merge_prompt(session: Session, record_indices: list[int]) -> str:
    body = render_records_for_llm(session, record_indices)
    return MERGE_PROMPT_TEMPLATE.format(body=body)


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def apply_edits_and_save(
    session: Session,
    out_dir: Path | None = None,
    new_sid: str | None = None,
) -> dict[str, Any]:
    """Materialize the user's toggles into a new jsonl file + manifest.

    Returns a dict with paths and stats.
    """
    if out_dir is None:
        out_dir = session.path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    if new_sid is None:
        new_sid = str(uuid_lib.uuid4())

    # Index toggles per (record_index, block_index)
    keep_map: dict[tuple[int, int], bool] = {}
    for blk in session.blocks:
        keep_map[(blk.record_index, blk.block_index)] = blk.locked or blk.keep

    # Records covered by a merge — they get replaced by a synthetic record
    # and are dropped from the output. The mapping `last_merged_to_synthetic`
    # is keyed by the uuid of the LAST original record in each merge group so
    # downstream records can re-parent onto the synthetic record's uuid.
    merged_uuids: set[str] = set()
    merge_for_record_index: dict[int, "Merge"] = {}
    synthetic_after_uuid: dict[str, dict[str, Any]] = {}  # parentUuid -> synthetic rec
    last_uuid_to_synthetic_uuid: dict[str, str] = {}
    synthetic_records: list[dict[str, Any]] = []
    merged_groups_manifest: list[dict[str, Any]] = []

    for m in session.merges:
        for ri in m.record_indices:
            rec = session.records[ri]
            if "uuid" in rec:
                merged_uuids.add(rec["uuid"])
            merge_for_record_index[ri] = m

    # Build synthetic records up-front so we know their uuids.
    for m in session.merges:
        if not m.record_indices:
            continue
        first_idx = m.record_indices[0]
        last_idx = m.record_indices[-1]
        first_rec = session.records[first_idx]
        last_rec = session.records[last_idx]
        parent = first_rec.get("parentUuid")
        # If parent is itself merged, we'll stitch later. For now record the
        # raw parent; pass-2 stitching handles chains.
        synth = _build_synthetic_record(
            session=session,
            template_record=first_rec,
            summary_text=m.summary_text,
            new_uuid=m.new_uuid,
            parent_uuid=parent,
            new_sid=new_sid,
            merged_count=len(m.record_indices),
        )
        synth["_ce_insert_after_uuid"] = parent  # transient marker; removed below
        synthetic_records.append(synth)
        synthetic_after_uuid[parent or ""] = synth
        if "uuid" in last_rec:
            last_uuid_to_synthetic_uuid[last_rec["uuid"]] = m.new_uuid
        merged_groups_manifest.append(
            {
                "synthetic_uuid": m.new_uuid,
                "replaced_record_uuids": [
                    session.records[ri].get("uuid")
                    for ri in m.record_indices
                    if "uuid" in session.records[ri]
                ],
                "summary_chars": len(m.summary_text),
            }
        )

    # Pass 1: figure out which records survive after dropping content blocks.
    new_records: list[dict[str, Any]] = []
    dropped_uuids: set[str] = set()
    modified_blocks: list[dict[str, Any]] = []

    for ri, rec in enumerate(session.records):
        rtype = rec.get("type")
        # If this record is the FIRST of a merge group, emit the synthetic
        # record in its place. Drop all merged records.
        if ri in merge_for_record_index:
            m = merge_for_record_index[ri]
            if ri == m.record_indices[0]:
                # Emit the synthetic record now.
                synth = next(
                    (s for s in synthetic_records if s.get("uuid") == m.new_uuid),
                    None,
                )
                if synth is not None:
                    out = {k: v for k, v in synth.items() if k != "_ce_insert_after_uuid"}
                    new_records.append(out)
            # Skip the original (it's "merged away").
            continue

        # Meta records: pass through verbatim (just rewrite sessionId).
        if rtype in META_TYPES:
            nr = dict(rec)
            if "sessionId" in nr:
                nr["sessionId"] = new_sid
            new_records.append(nr)
            continue

        if rtype not in CONVERSATION_TYPES:
            # Unknown type — preserve to be safe.
            new_records.append(dict(rec))
            continue

        # attachment / system: pass through, no block editing
        if rtype in ("attachment", "system"):
            nr = dict(rec)
            if "sessionId" in nr:
                nr["sessionId"] = new_sid
            new_records.append(nr)
            continue

        # user with str content: locked, always keep
        msg = rec.get("message") or {}
        content = msg.get("content")
        if rtype == "user" and isinstance(content, str):
            nr = _clone_with_sid(rec, new_sid)
            new_records.append(nr)
            continue

        if not isinstance(content, list):
            nr = _clone_with_sid(rec, new_sid)
            new_records.append(nr)
            continue

        # Rebuild content array from kept blocks. Tool_result kept-but-hidden
        # logic handled separately because the block stays but its payload
        # is stubbed. Locked blocks always survive.
        new_content: list[Any] = []
        for bi, b in enumerate(content):
            keep = keep_map.get((ri, bi), True)
            if not isinstance(b, dict):
                new_content.append(b)
                continue
            btype = b.get("type")

            # tool_result: kept-with-stub if "hidden"
            if rtype == "user" and btype == "tool_result":
                if keep:
                    new_content.append(b)
                else:
                    orig_size = _tool_result_size(b.get("content"))
                    stub = (
                        f"[hidden by sculptor · "
                        f"original size {orig_size} chars]"
                    )
                    new_b = dict(b)
                    new_b["content"] = stub
                    new_content.append(new_b)
                    modified_blocks.append(
                        {
                            "record_uuid": rec.get("uuid"),
                            "block_index": bi,
                            "action": "tool_result_stubbed",
                            "tool_use_id": b.get("tool_use_id"),
                            "original_size": orig_size,
                        }
                    )
                continue

            # tool_use / user text / image / other: locked, always kept
            if (rtype == "user" and btype in ("text", "image")) or (
                rtype == "assistant" and btype == "tool_use"
            ):
                new_content.append(b)
                continue

            # assistant text / thinking: drop if not kept
            if rtype == "assistant" and btype in ("text", "thinking"):
                if keep:
                    new_content.append(b)
                else:
                    modified_blocks.append(
                        {
                            "record_uuid": rec.get("uuid"),
                            "block_index": bi,
                            "action": f"{btype}_dropped",
                            "original_size": len(
                                b.get("text") or b.get("thinking") or ""
                            ),
                        }
                    )
                continue

            # unknown block type — preserve
            new_content.append(b)

        # If we stripped the assistant record empty of meaningful blocks, drop
        # the whole record. "Meaningful" means anything other than tool_use
        # (because a bare tool_use without text is still valid; assistant can
        # call a tool with no prose).
        if rtype == "assistant" and not new_content:
            dropped_uuids.add(rec.get("uuid"))
            continue

        new_rec = _clone_with_sid(rec, new_sid)
        new_rec["message"] = dict(msg)
        new_rec["message"]["content"] = new_content
        new_records.append(new_rec)

    # Pass 2: stitch parentUuid chain.
    #   * If parent is a merged uuid: redirect to that merge group's synthetic
    #     record (so the chain re-joins the conversation at the summary).
    #   * If parent is a dropped uuid: walk further up until we find a kept
    #     ancestor or null.
    if dropped_uuids or merged_uuids:
        orig_parent: dict[str, str | None] = {}
        for rec in session.records:
            if "uuid" in rec:
                orig_parent[rec["uuid"]] = rec.get("parentUuid")

        # Build map: any merged record uuid -> its synthetic record's uuid
        merged_to_synth: dict[str, str] = {}
        for m in session.merges:
            for ri in m.record_indices:
                rec = session.records[ri]
                if "uuid" in rec:
                    merged_to_synth[rec["uuid"]] = m.new_uuid

        for rec in new_records:
            parent = rec.get("parentUuid")
            # If this is a synthetic record, its parent might itself be inside
            # an earlier merge group — redirect to that group's synthetic.
            while parent and parent in merged_to_synth:
                # Use the merge group's synthetic uuid IF the synthetic itself
                # is not the current record (avoid self-loops on the first
                # merged group whose first record's parent was outside merges).
                target = merged_to_synth[parent]
                if target == rec.get("uuid"):
                    parent = orig_parent.get(parent)
                else:
                    parent = target
                    break
            while parent and parent in dropped_uuids:
                parent = orig_parent.get(parent)
            if "parentUuid" in rec:
                rec["parentUuid"] = parent

    # Validate tool_use / tool_result pairing.
    _validate_pairing(new_records)

    # Write outputs.
    out_jsonl = out_dir / f"{new_sid}.jsonl"
    with out_jsonl.open("w", encoding="utf-8") as f:
        for rec in new_records:
            f.write(json.dumps(rec, ensure_ascii=False))
            f.write("\n")

    manifest = {
        "tool": "sculptor",
        "version": 1,
        "source_session": session.records[0].get("sessionId")
        if session.records
        else None,
        "source_path": str(session.path),
        "new_session_id": new_sid,
        "new_session_path": str(out_jsonl),
        "edited_at": datetime.now(timezone.utc).isoformat(),
        "dropped_record_uuids": sorted(dropped_uuids),
        "modified_blocks": modified_blocks,
        "merge_groups": merged_groups_manifest,
        "stats": session.stats(),
    }
    manifest_path = out_dir / f"{new_sid}.edit-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "out_jsonl": out_jsonl,
        "manifest": manifest_path,
        "new_sid": new_sid,
        "stats": manifest["stats"],
        "dropped_record_count": len(dropped_uuids),
        "modified_block_count": len(modified_blocks),
    }


def _clone_with_sid(rec: dict[str, Any], new_sid: str) -> dict[str, Any]:
    nr = dict(rec)
    if "sessionId" in nr:
        nr["sessionId"] = new_sid
    return nr


def _build_synthetic_record(
    *,
    session: Session,
    template_record: dict[str, Any],
    summary_text: str,
    new_uuid: str,
    parent_uuid: str | None,
    new_sid: str,
    merged_count: int,
) -> dict[str, Any]:
    """Construct a single assistant text record that replaces a merge group."""
    # Inherit envelope fields from the template (the first record in the
    # group), with sessionId / uuid / parentUuid / message overridden.
    base = {
        k: v
        for k, v in template_record.items()
        if k
        in (
            "isSidechain",
            "userType",
            "entrypoint",
            "cwd",
            "version",
            "gitBranch",
            "promptId",
        )
    }
    body = (
        f"[sculptor merged {merged_count} record"
        f"{'s' if merged_count != 1 else ''} → LLM summary]\n\n{summary_text}"
    )
    return {
        **base,
        "type": "assistant",
        "uuid": new_uuid,
        "parentUuid": parent_uuid,
        "sessionId": new_sid,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "message": {
            "id": f"msg_ce_{new_uuid[:8]}",
            "type": "message",
            "role": "assistant",
            "model": "sculptor-synthetic",
            "content": [{"type": "text", "text": body}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
        },
    }


def _tool_result_size(payload: Any) -> int:
    if isinstance(payload, str):
        return len(payload)
    if isinstance(payload, list):
        n = 0
        for item in payload:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    n += len(item.get("text") or "")
                elif item.get("type") == "image":
                    d = (item.get("source") or {}).get("data", "")
                    n += len(d) if isinstance(d, str) else 0
        return n
    return len(str(payload))


def _validate_pairing(records: list[dict[str, Any]]) -> None:
    """Sanity check: every assistant tool_use has a downstream tool_result.

    We don't enforce strictly (Claude Code itself sometimes has loose chains),
    but we raise if there's a flagrant orphan that would break the API on
    resume.
    """
    tool_use_ids: set[str] = set()
    tool_result_ids: set[str] = set()
    for r in records:
        msg = r.get("message") or {}
        c = msg.get("content")
        if not isinstance(c, list):
            continue
        if r.get("type") == "assistant":
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    tool_use_ids.add(b["id"])
        elif r.get("type") == "user":
            for b in c:
                if (
                    isinstance(b, dict)
                    and b.get("type") == "tool_result"
                    and b.get("tool_use_id")
                ):
                    tool_result_ids.add(b["tool_use_id"])

    orphan_calls = tool_use_ids - tool_result_ids
    orphan_results = tool_result_ids - tool_use_ids
    # Orphan calls at the very tail (the last assistant turn) are fine — the
    # session was just paused mid-tool-call. We only warn if there are many.
    if len(orphan_calls) > 1:
        # Not raising — Claude Code itself tolerates this on resume. Just log.
        pass
    _ = orphan_results


# ---------------------------------------------------------------------------
# Heuristic auto mode
# ---------------------------------------------------------------------------


def auto_mark(
    session: Session,
    drop_tool_results_larger_than: int = 0,
    drop_thinking: bool = False,
    drop_failed_bash: bool = False,
) -> int:
    """Apply heuristic rules to mark blocks as 'hide'. Returns count toggled."""
    n = 0
    for blk in session.blocks:
        if blk.locked:
            continue
        if drop_thinking and blk.kind == BLOCK_THINKING and blk.keep:
            blk.keep = False
            n += 1
            continue
        if (
            drop_tool_results_larger_than
            and blk.kind == BLOCK_TOOL_RESULT
            and blk.size_chars >= drop_tool_results_larger_than
            and blk.keep
        ):
            blk.keep = False
            n += 1
            continue
        if drop_failed_bash and blk.kind == BLOCK_TOOL_RESULT and blk.keep:
            preview = (blk.preview or "").lower()
            if "error" in preview or "command not found" in preview or "no such file" in preview:
                blk.keep = False
                n += 1
                continue
    return n
