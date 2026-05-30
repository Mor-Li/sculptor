#!/usr/bin/env python3
"""S2(Stage 2): 把 agent 改过的中间 markdown 转回 jsonl,产出新 session。

工作流:
    s1.py <session.jsonl>         # 产出 edit.md + sidecar
    [agent 直接 edit edit.md]  # 删段=hide, 改 body=merge改写, 不动=keep
    s2.py <edit.md>               # 读 edit.md + sidecar,产出新 jsonl

后处理逻辑(三种意图):
  1. sidecar 里的 section 在改后 MD 里 **没找到** → hide
     (对该 section 内所有 block 设 keep=False,走 apply_edits_and_save 的现有 hide 逻辑)
  2. 在改后 MD 里 **找到了且 body md5 一致** → keep(什么都不做)
  3. 在改后 MD 里 **找到了但 body 改了** → merge replacement
     - 单 block section: 直接 mutate session.records 里那条 record 的对应 content block 文本
     - call+result section: 走 Merge 把两条 record 替换成一条 synthetic text record

后处理还做的 deterministic 修正(agent 不用操心):
  - tool_use ↔ tool_result 配对孤儿: session_model.apply_edits_and_save 已经处理
  - parentUuid 链断裂: 同上自动 stitch
  - 顺序保证: 一律按 sidecar 里的 section 顺序遍历(agent 不能改 section 顺序)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

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
    count_tokens,
)


# 解析 ### heading: `### turn N · KIND · b0001+b0002 · TOKENS · META?`
HEADING_RE = re.compile(
    r"^###\s+turn\s+\d+\s+·\s+\S+\s+·\s+(b\d{4}(?:\+b\d{4})*)\s+·\s+\S+(?:\s+·\s+.+)?$"
)


def _normalize_body(s: str) -> str:
    """跟 preprocess.normalize_body 完全一致:统一换行 + strip,
    保证两边 md5 比对在字节级别对称。"""
    return s.replace("\r\n", "\n").replace("\r", "\n").strip()


def parse_edited_markdown(md_text: str) -> dict[str, str]:
    """从 agent 改后的 markdown 提取每个 section 的 body。

    Returns: dict mapping `tuple(block_ids)` → body_text
        key 是 (b0001,) 或 (b0042, b0043) 这种 tuple,跟 sidecar 一一对应
    """
    sections: dict[tuple[str, ...], str] = {}
    current_ids: tuple[str, ...] | None = None
    current_body: list[str] = []

    def flush():
        if current_ids is not None:
            sections[current_ids] = _normalize_body("\n".join(current_body))

    for line in md_text.splitlines():
        m = HEADING_RE.match(line.strip())
        if m:
            flush()
            ids_str = m.group(1)
            current_ids = tuple(ids_str.split("+"))
            current_body = []
        elif current_ids is not None:
            current_body.append(line)
    flush()
    return sections


def replace_block_content(record: dict, block_index: int, new_text: str, kind: str) -> bool:
    """直接 mutate session.records 里某 record 的 message.content[block_index]
    为新文本。返回 True 表示成功。

    根据 block kind 决定改哪个字段:
      text → content[i]["text"] = new_text
      thinking → content[i]["thinking"] = new_text
      user_input(user record str message) → message = new_text
    """
    msg = record.get("message")
    if not isinstance(msg, dict):
        # user record 的 message 可能直接是 string
        if kind == BLOCK_USER_INPUT and isinstance(msg, str):
            record["message"] = new_text
            return True
        return False

    content = msg.get("content")
    if isinstance(content, str):
        # user input 用纯字符串表示
        if kind == BLOCK_USER_INPUT:
            msg["content"] = new_text
            return True
        return False

    if not isinstance(content, list) or block_index >= len(content):
        return False

    block = content[block_index]
    if not isinstance(block, dict):
        return False

    if kind == BLOCK_TEXT:
        block["text"] = new_text
    elif kind == BLOCK_THINKING:
        block["thinking"] = new_text
        # signature 已经无意义,删掉(原 signature 是 server 端 cache 句柄)
        block.pop("signature", None)
    elif kind == BLOCK_USER_INPUT:
        if "text" in block:
            block["text"] = new_text
        else:
            return False
    elif kind == BLOCK_TOOL_RESULT:
        # tool_result 的 content 可能是 string 或 list,统一替换成 string
        block["content"] = new_text
    elif kind == BLOCK_TOOL_USE:
        # tool_use 的 input 不允许改写(agent 修改成自然语言会破坏 API)
        return False
    else:
        return False
    return True


def postprocess(md_path: Path, out_jsonl_path: Path | None = None) -> dict:
    """主入口:读 edited markdown 和 sidecar,产出新 jsonl。"""
    sidecar_path = md_path.with_suffix(md_path.suffix + ".sidecar.json")
    if not sidecar_path.is_file():
        raise FileNotFoundError(f"找不到 sidecar: {sidecar_path}")

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    source_jsonl = Path(sidecar["source_jsonl"]).expanduser().resolve()
    if not source_jsonl.is_file():
        raise FileNotFoundError(f"找不到原 jsonl: {source_jsonl}")

    md_text = md_path.read_text(encoding="utf-8")
    edited_sections = parse_edited_markdown(md_text)

    session = Session.load(source_jsonl)

    # 建 block_id → (block, sidecar_block_info) 的映射
    bid_to_block: dict[str, object] = {}
    bid_to_info: dict[str, dict] = {}
    for b in session.blocks:
        for sc_section in sidecar["sections"]:
            for sc_block in sc_section["blocks"]:
                if (
                    sc_block["record_index"] == b.record_index
                    and sc_block["block_index"] == b.block_index
                ):
                    bid_to_block[sc_block["block_id"]] = b
                    bid_to_info[sc_block["block_id"]] = sc_block

    stats = {
        "sections_total": len(sidecar["sections"]),
        "sections_kept": 0,
        "sections_hidden": 0,
        "sections_rewritten": 0,
        "blocks_hidden": 0,
        "blocks_rewritten": 0,
        "merges_added": 0,
    }

    for sc_section in sidecar["sections"]:
        section_key = tuple(sc_section["block_ids"])
        old_md5 = sc_section["body_md5"]

        if section_key not in edited_sections:
            # 整段不在 → hide
            stats["sections_hidden"] += 1
            for sc_block in sc_section["blocks"]:
                blk = bid_to_block.get(sc_block["block_id"])
                if blk is not None and not blk.locked:
                    blk.keep = False
                    stats["blocks_hidden"] += 1
                elif blk is not None and blk.locked:
                    # locked user_input: drop 整 record(走 keep=False 路径要求支持)
                    # session_model 的 user_input 是 locked=True,这里直接置 keep=False 让
                    # apply_edits_and_save 决定行为(目前 apply 会保留 locked block)。
                    # 真要删 user record,需要扩 session_model 支持 user drop。
                    # v0 先把 user 段视为"删不掉",忽略 hide 请求,警告用户。
                    print(
                        f"警告: section {sc_section['section_id']} 是 user 段,"
                        f"当前后处理不支持删 user record。section 会保留。",
                        file=sys.stderr,
                    )
            continue

        new_body = edited_sections[section_key]
        new_md5 = hashlib.md5(new_body.encode("utf-8")).hexdigest()

        if new_md5 == old_md5:
            # 未改 → keep
            stats["sections_kept"] += 1
            continue

        # 改了 body → merge replacement
        stats["sections_rewritten"] += 1

        if len(sc_section["blocks"]) == 1:
            # 单 block section: 直接 mutate session.records 那条 record 的 block 文本
            sc_block = sc_section["blocks"][0]
            ri = sc_block["record_index"]
            bi = sc_block["block_index"]
            kind = sc_block["kind"]
            ok = replace_block_content(session.records[ri], bi, new_body, kind)
            if ok:
                stats["blocks_rewritten"] += 1
                # 更新 Block 对象的 raw 引用对应的 size 估算(对后续 stats 显示有影响)
                blk = bid_to_block.get(sc_block["block_id"])
                if blk is not None:
                    blk.size_chars = len(new_body)
                    blk.size_tokens = count_tokens(new_body)
            else:
                print(
                    f"警告: section {sc_section['section_id']} 单块改写失败,"
                    f"kind={kind} 不支持改写。已跳过。",
                    file=sys.stderr,
                )
        else:
            # call+result section(2 blocks): 走 Merge,把两条 record 替换成一条 synthetic text
            record_indices = sorted({sb["record_index"] for sb in sc_section["blocks"]})
            parent = session.records[record_indices[0]].get("parentUuid")
            session.merges.append(
                Merge(
                    record_indices=record_indices,
                    summary_text=new_body,
                    summary_tokens=count_tokens(new_body),
                    new_uuid=str(uuid.uuid4()),
                    insertion_parent_uuid=parent,
                    label=f"agent rewrote {sc_section['section_id']}",
                )
            )
            stats["merges_added"] += 1

    # 输出新 jsonl
    out_dir = (out_jsonl_path.parent if out_jsonl_path else source_jsonl.parent)
    result = apply_edits_and_save(session, out_dir=out_dir)

    return {
        "out_jsonl": result["out_jsonl"],
        "manifest": result["manifest"],
        "stats": stats,
        "apply_stats": result["stats"],
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="S2: 把 agent 改过的中间 markdown 转回新的 jsonl session。"
    )
    p.add_argument("edited_md", help="agent 改过的 markdown 路径(同目录需有 .sidecar.json)")
    p.add_argument(
        "-o",
        "--out-dir",
        help="新 jsonl 输出目录(默认放在原 jsonl 同目录,这样 claude --resume 能扫到)",
    )
    args = p.parse_args()

    md_path = Path(args.edited_md).expanduser().resolve()
    if not md_path.is_file():
        print(f"找不到文件: {md_path}", file=sys.stderr)
        return 1

    out_dir_path = None
    if args.out_dir:
        out_dir_path = Path(args.out_dir).expanduser().resolve()
        out_dir_path.mkdir(parents=True, exist_ok=True)
        # 让 postprocess 知道要写到这里;这里传一个伪造的 jsonl 路径(只用其 parent)
        fake_jsonl = out_dir_path / "out.jsonl"
        result = postprocess(md_path, fake_jsonl)
    else:
        result = postprocess(md_path)

    s = result["stats"]
    a = result["apply_stats"]
    print(f"✓ wrote {result['out_jsonl']}")
    print(f"  manifest: {result['manifest']}")
    print(
        f"  sections: kept={s['sections_kept']} · "
        f"hidden={s['sections_hidden']} · "
        f"rewritten={s['sections_rewritten']} "
        f"(of {s['sections_total']})"
    )
    print(
        f"  blocks: hidden={s['blocks_hidden']} · "
        f"rewritten={s['blocks_rewritten']} · "
        f"merges={s['merges_added']}"
    )
    print(
        f"  tokens kept: {a['tokens_kept']:,}  "
        f"(hidden -{a['tokens_hidden']:,})"
    )
    print()
    new_sid = Path(result["out_jsonl"]).stem

    # 拼一条 ready-to-paste 的 resume 命令。
    # 需要: (1) cwd (从新 jsonl 任一条 record 的 cwd 字段读), (2) Claude Code
    # 期望 jsonl 落在 ~/.claude/projects/<encoded-cwd>/, 若 s2 输出在别处,
    # 提示先 cp。
    out_jsonl = Path(result["out_jsonl"])
    cwd = None
    try:
        with open(out_jsonl) as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("cwd"):
                    cwd = rec["cwd"]
                    break
    except Exception:  # noqa: BLE001
        pass
    if cwd:
        encoded = str(Path(cwd).resolve()).replace("/", "-")
        project_dir = Path.home() / ".claude" / "projects" / encoded
        # 如果 s2 已经落在 project 目录, claude --resume 能直接扫到
        if out_jsonl.parent.resolve() == project_dir.resolve():
            print("恢复用 (直接复制即可):")
            print(
                f"  cd {cwd} && claude --dangerously-skip-permissions -r {new_sid}"
            )
        else:
            print("恢复用 (先 cp 到 project 目录再 resume):")
            print(f"  cp {out_jsonl} {project_dir}/ && \\")
            print(
                f"  cd {cwd} && claude --dangerously-skip-permissions -r {new_sid}"
            )
    else:
        print(
            f"恢复用 (未能从 session 读出 cwd): "
            f"claude --dangerously-skip-permissions -r {new_sid}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
