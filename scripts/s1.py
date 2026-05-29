#!/usr/bin/env python3
"""S1(Stage 1): 把 Claude Code session jsonl 预处理成可编辑的中间 markdown。

工作流:
    s1.py <session.jsonl>           # 产出 <session>.edit.md 和 .sidecar.json
    [agent 直接 read/edit edit.md] (删段 = hide, 改 body = merge 改写, 不动 = keep)
    s2.py <edit.md>                 # 产出新 jsonl 给 claude --resume

中间 markdown 格式:
- 每段一个 `### turn N · kind · b00NN · Nt · meta` heading
- tool_use 跟对应 tool_result 打包成一段(删一起删,避免配对孤儿)
- 不需要状态 marker,agent 直接破坏性 edit
- agent 不要改 `b00NN` id,这是 round-trip 用的 anchor

sidecar `<edit.md>.sidecar.json` 存 b00NN 到原 jsonl record_index 的映射 + body md5,
后处理(s2)用它判断每段:
- 还在且 body 一致 → keep
- 还在但 body 改了 → merge replacement
- 消失 → hide
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from session_model import (  # noqa: E402
    BLOCK_TEXT,
    BLOCK_THINKING,
    BLOCK_TOOL_RESULT,
    BLOCK_TOOL_USE,
    BLOCK_USER_INPUT,
    Session,
)


KIND_LABEL = {
    BLOCK_USER_INPUT: "user",
    BLOCK_TEXT: "asst",
    BLOCK_THINKING: "think",
    BLOCK_TOOL_USE: "call",
    BLOCK_TOOL_RESULT: "result",
}


def extract_block_text(blk) -> str:
    """提取 block 的可读文本。

    与 context_edit._block_content_text 行为对齐,但在这里独立实现避免循环依赖。
    特殊处理:
    - Bash tool_use: 还原 `\\n` 为真换行,渲染成 `$ <command>` 形式
    - 空 thinking(只有 signature)显示警告: signature 是加密的完整推理, 不要删
    """
    raw = blk.raw
    if blk.kind == BLOCK_TOOL_USE and isinstance(raw, dict):
        name = raw.get("name") or "?"
        inp = raw.get("input") or {}
        if isinstance(inp, dict) and inp.get("_sculptor_hidden"):
            return (
                f"tool: {name}\n\n"
                f"(input 已被 sculptor hide;原始 input 长度 "
                f"{inp.get('_original_size', '?')} chars)"
            )
        if name == "Bash" and isinstance(inp, dict) and "command" in inp:
            cmd = inp.get("command", "") or ""
            desc = inp.get("description", "") or ""
            parts = [f"tool: Bash"]
            if desc:
                parts.append(f"description: {desc}")
            parts.append("")
            parts.append(f"$ {cmd}")
            return "\n".join(parts)
        parts = [f"tool: {name}"]
        if isinstance(inp, dict):
            for k, v in inp.items():
                if isinstance(v, str):
                    if "\n" in v or len(v) > 80:
                        parts.append(f"{k}:")
                        parts.append(v)
                    else:
                        parts.append(f"{k}: {v}")
                else:
                    try:
                        parts.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
                    except Exception:  # noqa: BLE001
                        parts.append(f"{k}: {v!r}")
        return "\n".join(parts)

    if blk.kind == BLOCK_THINKING and isinstance(raw, dict):
        body = raw.get("thinking") or raw.get("text") or ""
        if not body.strip():
            sig = raw.get("signature")
            sig_note = f"signature {len(sig)} chars" if sig else "no signature"
            # 重要: signature 不是 verification hash, 是加密的完整 thinking 内容,
            # server 端会解码使用 (Anthropic 官方文档明确说明)。本地看着是空 ≠
            # server 端看不到。删 signature = 让 server 端失去原推理 condition。
            # 详见 SKILL.md "❌ 反模式"。
            return (
                f"[thinking · {sig_note} · ⚠️ signature 是加密的完整推理内容, "
                f"server 端会解码使用 — **不要删这段**]"
            )
        return body

    if blk.kind == BLOCK_USER_INPUT:
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            return raw.get("text") or ""

    if blk.kind == BLOCK_TEXT and isinstance(raw, dict):
        return raw.get("text") or ""

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


def build_sections(session: Session) -> list[dict]:
    """走 session.turns / blocks,产出 sections。

    每个 section 表示中间 MD 里的一个 `### ` 块。tool_use 跟对应的 tool_result
    打包成同一个 section,delete 时一起删,避免 API 配对孤儿。

    Returns: list of section dicts:
      {
        "section_id": "s0001",
        "turn_idx": int,            # 1-based
        "kind_label": "user/think/asst/call+result/...",
        "block_ids": ["b0001"],     # 1~2 个 (单 block 或 call+result 配对)
        "blocks": [Block, ...],
        "header_meta": "Bash" 等     # 可选
      }
    """
    sections: list[dict] = []
    section_counter = 1
    block_counter = 1
    used_keys: set[tuple[int, int]] = set()

    tool_results_by_id: dict[str, object] = {}
    for b in session.blocks:
        if b.kind == BLOCK_TOOL_RESULT and b.tool_use_id:
            tool_results_by_id[b.tool_use_id] = b

    for t in session.turns:
        block_list = []
        if t.user_block is not None:
            block_list.append(t.user_block)
        block_list.extend(t.blocks)

        for b in block_list:
            key = (b.record_index, b.block_index)
            if key in used_keys:
                continue
            used_keys.add(key)

            bid = f"b{block_counter:04d}"
            block_counter += 1

            section = {
                "section_id": f"s{section_counter:04d}",
                "turn_idx": t.index + 1,
                "blocks": [b],
                "block_ids": [bid],
                "kind_label": KIND_LABEL.get(b.kind, b.kind),
                "header_meta": None,
            }

            if b.kind == BLOCK_TOOL_USE:
                section["header_meta"] = b.tool_name
                result_b = tool_results_by_id.get(b.tool_use_id)
                if result_b is not None:
                    result_key = (result_b.record_index, result_b.block_index)
                    if result_key not in used_keys:
                        section["blocks"].append(result_b)
                        used_keys.add(result_key)
                        section["kind_label"] = "call+result"
                        bid_r = f"b{block_counter:04d}"
                        section["block_ids"].append(bid_r)
                        block_counter += 1

            sections.append(section)
            section_counter += 1

    return sections


def render_section_heading(section: dict) -> str:
    """生成 ### heading 单行。

    格式:`### turn N · KIND · bIDS · TOKENS · META`
    示例:
      `### turn 1 · user · b0001 · 925t`
      `### turn 3 · call+result · b0042+b0043 · 66+58t · Bash`
    """
    bids = "+".join(section["block_ids"])
    toks = "+".join(f"{b.size_tokens}" for b in section["blocks"]) + "t"
    parts = [
        f"turn {section['turn_idx']}",
        section["kind_label"],
        bids,
        toks,
    ]
    if section["header_meta"]:
        parts.append(section["header_meta"])
    return "### " + " · ".join(parts)


def normalize_body(s: str) -> str:
    """统一规范化 body,保证 preprocess 和 postprocess 看到的内容字节一致:
    - 换行统一成 \\n(去掉 \\r 和 \\r\\n)
    - 去首尾空白
    """
    return s.replace("\r\n", "\n").replace("\r", "\n").strip()


def render_section_body(section: dict) -> str:
    """生成 section 内容(不含 heading)。

    单 block 直接渲染。call+result 用 `**call** (b00NN):` + `**result** (b00NN):`
    分两段。最终统一 normalize 保证 round-trip md5 稳定。
    """
    if len(section["blocks"]) == 1:
        return normalize_body(extract_block_text(section["blocks"][0]))

    parts = []
    for b, bid in zip(section["blocks"], section["block_ids"]):
        kind_word = "call" if b.kind == BLOCK_TOOL_USE else "result"
        parts.append(f"**{kind_word}** ({bid}):")
        parts.append(extract_block_text(b))
        parts.append("")
    return normalize_body("\n".join(parts))


PREAMBLE_TEMPLATE = """# sculptor edit · sid {sid} · {tok:,} tokens · {turns} turns

> **指南**(详见 `~/.claude/skills/sculptor/SKILL.md` 的 9 个 pattern):
> - 删整段 = 该 record 在新 jsonl 里被 hide
> - 改 body = 该 record 被替换成 merged synthetic record(你写什么就是什么)
> - 不动 = keep
> - **不要改动** `### ` heading 里的 `b00NN` id,这是后处理定位 anchor
> - tool_use + tool_result 已打包成一段,删一起删
> - user 段也可以删 / 改
>
> 来源:`{source_path}`
> records: {records} · blocks: {blocks} · turns: {turns}
> 目标:按 SKILL.md 的优先级流程裁到 ~50% token

---

"""


def preprocess(session_path: Path, out_md_path: Path) -> dict:
    """主入口:读 jsonl,产出 markdown + sidecar JSON。"""
    session = Session.load(session_path)
    sections = build_sections(session)

    st = session.stats()
    md_lines: list[str] = []
    md_lines.append(
        PREAMBLE_TEMPLATE.format(
            sid=session_path.stem[:8],
            tok=st["tokens_kept"],
            turns=len(session.turns),
            source_path=session_path,
            records=len(session.records),
            blocks=len(session.blocks),
        )
    )

    sidecar = {
        "tool": "sculptor",
        "stage": "s1",
        "source_jsonl": str(session_path),
        "session_id": session_path.stem,
        "sections": [],
    }

    for section in sections:
        heading = render_section_heading(section)
        body = render_section_body(section)
        md_lines.append(heading)
        md_lines.append("")
        md_lines.append(body)
        md_lines.append("")

        sidecar["sections"].append(
            {
                "section_id": section["section_id"],
                "turn_idx": section["turn_idx"],
                "kind_label": section["kind_label"],
                "block_ids": section["block_ids"],
                "body_md5": hashlib.md5(body.encode("utf-8")).hexdigest(),
                "blocks": [
                    {
                        "block_id": bid,
                        "record_index": b.record_index,
                        "block_index": b.block_index,
                        "kind": b.kind,
                        "tool_use_id": b.tool_use_id,
                        "tool_name": b.tool_name,
                        "size_chars": b.size_chars,
                        "size_tokens": b.size_tokens,
                    }
                    for b, bid in zip(section["blocks"], section["block_ids"])
                ],
            }
        )

    md_content = "\n".join(md_lines)
    out_md_path.write_text(md_content, encoding="utf-8")

    sidecar_path = out_md_path.with_suffix(out_md_path.suffix + ".sidecar.json")
    sidecar_path.write_text(
        json.dumps(sidecar, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "out_md": out_md_path,
        "sidecar": sidecar_path,
        "stats": {
            "sections": len(sections),
            "records": len(session.records),
            "tokens": st["tokens_kept"],
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="S1: 把 Claude Code session jsonl 预处理成可编辑的 markdown。"
    )
    p.add_argument("input", help="jsonl 文件路径")
    p.add_argument(
        "-o",
        "--output",
        help="输出 markdown 路径(默认放在 jsonl 同目录,后缀 .edit.md)",
    )
    args = p.parse_args()

    in_path = Path(args.input).expanduser().resolve()
    if not in_path.is_file():
        print(f"找不到文件: {in_path}", file=sys.stderr)
        return 1

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
    else:
        # <name>.jsonl → <name>.edit.md
        out_path = in_path.with_suffix(".edit.md")

    result = preprocess(in_path, out_path)
    print(f"✓ wrote {result['out_md']}")
    print(f"  sidecar: {result['sidecar']}")
    s = result["stats"]
    print(f"  {s['sections']} sections · {s['records']} records · {s['tokens']:,} tokens")
    print()
    print(f"下一步:")
    print(f"  1. 让 agent 编辑 {result['out_md']}")
    print(f"     (删段=hide · 改段=merge · 不动=keep · 不要改 b00NN heading id)")
    print(f"  2. 后处理: s2.py {result['out_md']} → 新 jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
