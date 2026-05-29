#!/usr/bin/env python3
"""sculptor 的核心数据模型: 解析、编辑、回写 Claude Code 的 session jsonl 文件。

== 被谁引用 ==

本模块是内部 lib, **不是用户入口**, 只被 sculptor 的两个用户脚本 import:

  - `s1.py` (preprocess): jsonl -> 中间 markdown
       用到 Session.load / Block / Turn / Merge / count_tokens
  - `s2.py` (postprocess): 改过的 markdown -> 新 jsonl
       用到 Session.load / Merge / apply_edits_and_save / count_tokens

两边都用 Session.load 把 jsonl 拆成 Block 列表; s1 渲染成 markdown 让
agent 编辑; agent 编辑后 s2 解析意图 (hide / merge / keep) 并调
apply_edits_and_save 写出一个新的 jsonl, 让 `claude --resume <new-sid>`
接着干。原 jsonl 永远不动。

抽出这个文件的唯一理由是 s1 / s2 都要 ~700 行同样的解析/保存代码, 复制
两份不优雅。直接调本模块的不是用户, 是另两个 .py。

== Record 的两种形态 ==

1. 对话 record (`user` / `assistant` / `attachment` / `system`): 有 `uuid`
   和 `parentUuid`, 形式上是链 (实际上 parallel tool_use 会让它分叉成树)。
2. 元数据 record (`queue-operation` / `last-prompt` / `file-history-snapshot`
   / `mode` / `permission-mode` / `ai-title` / `agent-name` / `custom-title`
   等): 没有 uuid, 不在链上。保存时原样透传, 不参与编辑。
   (历史上曾以为有 `summary` 类型, 实测不存在; `/compact` 的产物是
   `system/compact_boundary` + 紧跟一条带 `isCompactSummary: true` 的
   user record, 详见 docs/jsonl-anatomy.md。)

== Block 模型 ==

编辑的最小单位是 Block, 一个 record 的 `message.content` 列表里每一项就
是一个 Block。块类型:

    text          assistant 的正文
    thinking      assistant 的推理 (reasoning) 内容; signature 字段是
                  加密的完整 thinking, 不要删 (详见 SKILL.md 反模式)
    tool_use      assistant 的工具调用
    tool_result   user 的工具返回
    user_input    真实的用户输入
    image / other 其它直接透传

`keep=False` (hide) 在保存时有两种处理路径:
  * `tool_result` -> 块保留, 但 `content` 字段替换为一段简短桩文本说明
    原始体积。这样 tool_use/tool_result 的配对关系不会被破坏。
  * `text` / `thinking` -> 块从 content 列表里直接删除。若 record 的
    content 因此变空, 整条 record 也会被丢掉, 然后 `parentUuid` 链被
    重新缝合 (jump 到最近的存活祖先)。

== Turn 与 Merge ==

Turn = 一次用户输入 + 直到下一次用户输入之间的所有 assistant 块。s1 用它
组织 markdown 段落 (`### turn N · ...`) 让 agent 有结构可循。

Merge = agent 在中间 markdown 里改写了某段 body 的意图。s2 把 Merge 实例
写成 record_indices + 新 summary_text + 新 uuid。保存时这些 records 被
合成 assistant text record (`model: "sculptor-synthetic"`) 替换,
parentUuid 链同样被缝合。
"""

from __future__ import annotations

import functools
import json
import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@functools.lru_cache(maxsize=1)
def _tiktoken_encoder():
    """惰性加载 cl100k_base encoder。该编码与用户全局 CLAUDE.md 约定的
    token 计数标准一致。"""
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """用 tiktoken 真算 token 数。空串 / 假值返回 0。"""
    if not text:
        return 0
    return len(_tiktoken_encoder().encode(text, disallowed_special=()))


CONVERSATION_TYPES = {"user", "assistant", "attachment", "system"}
# Meta record types: jsonl 里有 uuid 但不属于对话链, 保存时原样透传不参与编辑。
# 注: "summary" 历史上以为存在, 实测不在 (详见 docs/jsonl-anatomy.md);
# /compact 的产物是 system/compact_boundary + 紧跟一条带 isCompactSummary=true
# 的 user record。
META_TYPES = {
    "queue-operation",
    "last-prompt",
    "file-history-snapshot",
    "mode",
    "permission-mode",
    "ai-title",
    "agent-name",
    "custom-title",
    "progress",
}

BLOCK_TEXT = "text"
BLOCK_THINKING = "thinking"
BLOCK_TOOL_USE = "tool_use"
BLOCK_TOOL_RESULT = "tool_result"
BLOCK_USER_INPUT = "user_input"
BLOCK_IMAGE = "image"
BLOCK_OTHER = "other"

HIDEABLE_BLOCKS = {BLOCK_TEXT, BLOCK_THINKING, BLOCK_TOOL_RESULT, BLOCK_TOOL_USE}
LOCKED_BLOCKS = {BLOCK_USER_INPUT}


@dataclass
class Block:
    """暴露给 s1/s2 处理的最小内容单元 (一个 record 的 message.content 里的一项)。

    一个 Block 对应一个 record 的 `message.content` 列表里的一项。

    字段:
        record_index: 它属于 `Session.records` 里的第几条 record。
        block_index:  它在该 record `message.content` 数组里的下标。
        kind:         块类型 (BLOCK_TEXT / BLOCK_THINKING / ...)。
        keep:         用户决定是否保留 (False = 隐藏, 保存时会被丢掉
                      或桩化)。
        preview:      一行截断预览, s1 渲染 markdown 时用作 fallback 文本。
        size_chars:   原始字符数, 给 UI 显示体积用。
        size_tokens:  用 tiktoken 算出的真实 token 数 (thinking 块例外,
                      由 _reconcile_thinking_tokens 事后修正)。
        locked:       True 表示此块不能被隐藏 (user_input / tool_use 等)。
        tool_use_id:  tool_use / tool_result 用来配对的 id。
        tool_name:    tool_use 的工具名。
        raw:          原始 dict, 保存时回写用。
    """

    record_index: int
    block_index: int
    kind: str
    keep: bool = True
    preview: str = ""
    size_chars: int = 0
    size_tokens: int = 0  # 该块文本内容的真实 tiktoken 计数
    locked: bool = False
    tool_use_id: str | None = None
    tool_name: str | None = None
    raw: Any = None


@dataclass
class Turn:
    """一个 "turn" = 一次用户输入 + 紧随其后、直到下一次用户输入之前的
    所有 assistant / tool 块。s1 用它组织 markdown 段落 (`### turn N · ...`)。

    字段:
        index:      在 `Session.turns` 里的序号 (0-based)。
        user_block: 这一 turn 起点处那条 locked 的 user_input 块, 可能为
                    None (如果会话最前面有孤儿块)。
        blocks:     user_block 之后、下一个 user_input 之前的所有 Block,
                    按显示顺序排列。
        expanded:   折叠状态字段, 当前 s1/s2 流程未使用 (TUI 时代遗留)。
    """

    index: int
    user_block: Block | None  # 那条锁定的 user_input 块
    blocks: list[Block] = field(default_factory=list)  # 之后的所有块, 按顺序
    expanded: bool = True


@dataclass
class Merge:
    """一次用户请求的 "合并": 把一段连续的 records (用它们在
    `Session.records` 里的下标表示) 替换成一条合成的 assistant text record,
    内容是 `summary_text` (通常由 LLM 生成)。

    字段:
        record_indices:       被替换掉的 record 下标列表 (连续区间)。
        summary_text:         合成 record 的正文 (LLM 总结)。
        summary_tokens:       summary_text 的 token 数。
        new_uuid:             合成 record 的新 uuid (新生成的)。
        insertion_parent_uuid: 合成 record 应当挂在哪个 parentUuid 之下,
                              一般是被替换区间首条 record 原本的 parent。
        label:                可选的人类可读标签, 用于标记合成 record 的来源。
    """

    record_indices: list[int]
    summary_text: str
    summary_tokens: int
    new_uuid: str
    insertion_parent_uuid: str | None
    # 可选的人类可读标签, 用于标记合成 record 的来源。
    label: str = ""


@dataclass
class Session:
    """一次 Claude Code 会话的内存表示。

    `records` 保留原始 jsonl 的全部行 (元数据 record 也在内), 用于保存时
    原样回写。`blocks` 是从对话型 record 抽出来的扁平 Block 列表, 按显示
    顺序排列, 供 s1 渲染 markdown。`turns` 把 blocks 切成 turn 级分组。
    `merges` 由 s2 从 agent 改写 body 的意图推断, 在 `apply_edits_and_save`
    阶段一次性兑现。

    字段:
        path:    源 jsonl 文件路径 (只读, 不会被改)。
        records: 原始 record 列表 (每行 1 条 jsonl, 解析成 dict)。
        turns:   按 turn 分组后的 Block 视图。
        blocks:  扁平的 Block 列表, 按显示顺序。
        merges:  用户累积的合并操作列表。
    """

    path: Path
    records: list[dict[str, Any]]
    turns: list[Turn]
    blocks: list[Block]  # 扁平的 Block 列表, 按显示顺序
    merges: list[Merge] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Session":
        """从 jsonl 路径加载一个 Session 实例。

        逐行解析 JSON, 抽取对话型 record 里的 Block, 给 thinking 块算上
        真实 token 数, 再按 user_input 切成 turn。元数据 record 也会被
        读入 records 数组, 保存时原样透传。
        """
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

        cls._reconcile_thinking_tokens(records, blocks)
        turns = group_into_turns(blocks)
        return cls(path=path, records=records, blocks=blocks, turns=turns)

    @staticmethod
    def _reconcile_thinking_tokens(
        records: list[dict[str, Any]], blocks: list[Block]
    ) -> None:
        """给 thinking 块分摊真实 token 数。

        一次 API 调用的 `usage.output_tokens` 涵盖了所有生成内容
        (thinking + text + tool_use)。Claude Code 会把每个 content 块各存
        一条 record, 但同一次调用的几条 record 上的 `output_tokens` 是
        相同的复制值。为了把成本归到 thinking 块上: 对每个请求 (共享同
        一个 `message.id` 的多条 record), 减去那些可以直接 tiktoken 出来
        的兄弟块 (text 和 tool_use 的参数本地存的是原文), 剩下的额度均摊
        到 thinking 块上 -- thinking 块本地文本只是脱敏占位, 无法直接算。
        """
        groups: dict[str, list[Block]] = {}
        for blk in blocks:
            rec = records[blk.record_index]
            if rec.get("type") != "assistant":
                continue
            msg_id = (rec.get("message") or {}).get("id")
            if not msg_id:
                continue
            groups.setdefault(msg_id, []).append(blk)

        for group_blocks in groups.values():
            thinking_blocks = [b for b in group_blocks if b.kind == BLOCK_THINKING]
            if not thinking_blocks:
                continue
            rec = records[group_blocks[0].record_index]
            output_tokens = int(
                ((rec.get("message") or {}).get("usage") or {}).get("output_tokens") or 0
            )
            non_thinking_tokens = sum(
                b.size_tokens for b in group_blocks if b.kind != BLOCK_THINKING
            )
            per_thinking = max(0, output_tokens - non_thinking_tokens) // len(
                thinking_blocks
            )
            for b in thinking_blocks:
                b.size_tokens = per_thinking

    def stats(self) -> dict[str, int]:
        """汇总当前 session 的统计信息 (块数 / 字符数 / token 数)。

        被 merge 吞掉的块不算 kept, 而是单独算到 merged 桶里; 它们的
        summary 文本则作为新增 tokens 计入 tokens_kept。"""
        # 被 merge 吃掉的 record 不再计入 kept_chars。
        merged_record_ids: set[int] = set()
        for m in self.merges:
            merged_record_ids.update(m.record_indices)

        merged_summary_chars = sum(len(m.summary_text) for m in self.merges)
        merged_summary_tokens = sum(m.summary_tokens for m in self.merges)

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
    """把单条对话 record 拆成 Block 列表。

    根据 record 的 type (user / assistant) 和 message.content 的形态
    (str / list of dict), 生成对应的 Block 实例。其中 user_input 和
    tool_use 块会被打 `locked=True`, 因为它跟对应 tool_result 必须配对存在。

    参数:
        record_index: 该 record 在 `Session.records` 里的下标。
        record:       原始 record dict。

    返回: 该 record 拆出来的 Block 列表 (可能为空)。
    """
    rtype = record.get("type")
    msg = record.get("message") or {}

    # attachment / system 类型不携带可编辑内容块
    if rtype in ("attachment", "system"):
        return []

    content = msg.get("content")

    # 纯用户输入: content 是普通字符串
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
            # 走 user 角色的 wrap 文本 (例如 caveats、夹带的 system
            # reminder), 也按 locked 的 user_input 对待。
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
            # 本地存的 `thinking` 文本只是占位符; 真实 token 成本由
            # Session._reconcile_thinking_tokens 在 extract 完成后, 利用
            # 同次调用兄弟 record 共享的 usage.output_tokens 推算分摊。
            blocks.append(
                Block(
                    record_index=record_index,
                    block_index=bi,
                    kind=BLOCK_THINKING,
                    preview=_one_line(text, 200),
                    size_chars=len(text),
                    size_tokens=0,
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

    _ = user_only_text  # 当前只是信息性变量, 保留供未来扩展。
    return blocks


def group_into_turns(blocks: list[Block]) -> list[Turn]:
    """把扁平 Block 列表切分成 Turn 列表。

    切分规则: 每碰到一个 locked 的 BLOCK_USER_INPUT 就开一条新的 Turn。
    没有 user_input 引领的孤儿块 (罕见, 比如最前面的 attachment) 会归到
    一条匿名 Turn 里, 保证 s1 渲染 markdown 时所有 block 都有归属。"""
    turns: list[Turn] = []
    cur: Turn | None = None
    for blk in blocks:
        if blk.kind == BLOCK_USER_INPUT and blk.locked:
            cur = Turn(index=len(turns), user_block=blk)
            turns.append(cur)
        else:
            if cur is None:
                # 没有任何 user_input 在前的孤儿块 (罕见, 比如 attachments)。
                # 开一条匿名 turn, 让 s1 渲染时所有 block 都有归属。
                cur = Turn(index=len(turns), user_block=None)
                turns.append(cur)
            cur.blocks.append(blk)
    return turns


def _one_line(text: str, limit: int) -> str:
    """把文本压成一行, 超长则裁断加省略号。生成 Block.preview 用。"""
    text = (text or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _summarize_tool_result(payload: Any) -> tuple[str, int, str]:
    """生成 tool_result 的展示用三元组。

    返回 (preview, size_in_chars, text_for_tokenization)。

    ``text_for_tokenization`` 不包含 image 的 base64 (Claude API 里图片
    不按文本 tokenize), 但 size_in_chars 仍然把它们算进去, 这样 s1
    用户能看到原始 payload 的真实体积。
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
# Merge: 范围校验 + 为 LLM 渲染待合并的内容片段
# ---------------------------------------------------------------------------


def _find_record_with_tool_result(session: Session, tool_use_id: str) -> int | None:
    """在 session 里找出携带给定 tool_use_id 的 tool_result 那条 user record
    的下标; 找不到返回 None。"""
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
    """与上一个相反: 找出发起给定 tool_use_id 的那条 assistant record 下标,
    找不到返回 None。"""
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



# ---------------------------------------------------------------------------
# 保存阶段
# ---------------------------------------------------------------------------


def apply_edits_and_save(
    session: Session,
    out_dir: Path | None = None,
    new_sid: str | None = None,
) -> dict[str, Any]:
    """把用户的所有编辑 (keep 切换 + merges) 兑现成一个新的 jsonl 文件。

    步骤概览:
      1. 为每个 Merge 预先构造合成 record (assistant text), 记录新旧
         uuid 之间的映射。
      2. 第一遍扫描: 逐条决定每条原始 record 是保留 / 删除 / 替换;
         tool_result 隐藏 -> 桩化 content; tool_use 隐藏 -> 桩化 input;
         text / thinking 隐藏 -> 直接从 content 数组里删。
      3. 第二遍扫描: 缝合 parentUuid 链 -- 被 merge 吃掉的 uuid 指向
         其合成 record, 被删除的 uuid 一路向上找最近的活祖先。
      4. 校验 tool_use / tool_result 配对 (宽松检查)。
      5. 写出 `<new_sid>.jsonl` 和 `<new_sid>.edit-manifest.json`。

    参数:
        session: 已经被用户编辑过的 Session 实例。
        out_dir: 输出目录, 默认与源文件同目录 (这样 `claude --resume`
                 能直接找到新 sid)。
        new_sid: 新 sessionId; 默认随机生成一个 uuid4。

    返回: 包含 out_jsonl / manifest / new_sid / stats /
          dropped_record_count / modified_block_count 的 dict。
    """
    if out_dir is None:
        out_dir = session.path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    if new_sid is None:
        new_sid = str(uuid_lib.uuid4())

    # 把每个 Block 的 keep 决定按 (record_index, block_index) 建索引
    keep_map: dict[tuple[int, int], bool] = {}
    for blk in session.blocks:
        keep_map[(blk.record_index, blk.block_index)] = blk.locked or blk.keep

    # 被 merge 覆盖的 records -- 它们会被合成 record 替换并从输出里删掉。
    # `last_uuid_to_synthetic_uuid` 用每个合并组里最后一条原 record 的 uuid
    # 做 key, 这样下游 record 在缝合 parentUuid 时知道挂到合成 record 上。
    merged_uuids: set[str] = set()
    merge_for_record_index: dict[int, "Merge"] = {}
    synthetic_after_uuid: dict[str, dict[str, Any]] = {}  # parentUuid -> 合成 rec
    last_uuid_to_synthetic_uuid: dict[str, str] = {}
    synthetic_records: list[dict[str, Any]] = []
    merged_groups_manifest: list[dict[str, Any]] = []

    for m in session.merges:
        for ri in m.record_indices:
            rec = session.records[ri]
            if "uuid" in rec:
                merged_uuids.add(rec["uuid"])
            merge_for_record_index[ri] = m

    # 先建好所有合成 records, 这样下面缝合时可以直接用它们的 uuid。
    for m in session.merges:
        if not m.record_indices:
            continue
        first_idx = m.record_indices[0]
        last_idx = m.record_indices[-1]
        first_rec = session.records[first_idx]
        last_rec = session.records[last_idx]
        parent = first_rec.get("parentUuid")
        # 如果 parent 本身也被另一个 merge 吃掉, 由 pass-2 缝合处理;
        # 这里只先记原始 parent。
        synth = _build_synthetic_record(
            session=session,
            template_record=first_rec,
            summary_text=m.summary_text,
            summary_tokens=m.summary_tokens,
            new_uuid=m.new_uuid,
            parent_uuid=parent,
            new_sid=new_sid,
            merged_count=len(m.record_indices),
        )
        synth["_ce_insert_after_uuid"] = parent  # 临时标记, 下面会被剥掉
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

    # 第 1 遍扫描: 决定每条 record 在删除 content 块后是否仍然存活。
    new_records: list[dict[str, Any]] = []
    dropped_uuids: set[str] = set()
    modified_blocks: list[dict[str, Any]] = []

    for ri, rec in enumerate(session.records):
        rtype = rec.get("type")
        # 如果这条 record 是某个合并组的"首条", 用合成 record 替代它输出;
        # 合并组里其它原始 records 全部丢弃。
        if ri in merge_for_record_index:
            m = merge_for_record_index[ri]
            if ri == m.record_indices[0]:
                # 此时输出合成 record。
                synth = next(
                    (s for s in synthetic_records if s.get("uuid") == m.new_uuid),
                    None,
                )
                if synth is not None:
                    out = {k: v for k, v in synth.items() if k != "_ce_insert_after_uuid"}
                    new_records.append(out)
            # 跳过原 record (已经被"合并掉")。
            continue

        # 元数据 record: 原样透传 (只改 sessionId)。
        if rtype in META_TYPES:
            nr = dict(rec)
            if "sessionId" in nr:
                nr["sessionId"] = new_sid
            new_records.append(nr)
            continue

        if rtype not in CONVERSATION_TYPES:
            # 未知类型, 保险起见原样保留。
            new_records.append(dict(rec))
            continue

        # attachment / system: 透传, 不参与块级编辑
        if rtype in ("attachment", "system"):
            nr = dict(rec)
            if "sessionId" in nr:
                nr["sessionId"] = new_sid
            new_records.append(nr)
            continue

        # content 为字符串的 user record: 锁定, 永远保留
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

        # 用 keep 列表重建 content。tool_result 的 "保留但隐藏" 单独处理:
        # 块本身留着 (保留 tool_use_id 配对), 只是 payload 替成桩。锁定的
        # 块永远存活。
        new_content: list[Any] = []
        for bi, b in enumerate(content):
            keep = keep_map.get((ri, bi), True)
            if not isinstance(b, dict):
                new_content.append(b)
                continue
            btype = b.get("type")

            # tool_result: 隐藏时保留外壳但 payload 桩化
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

            # tool_use: 要么原样保留, 要么把 input 替成隐藏标记
            # (tool_use 外壳必须留住, 才能让它的 tool_use_id 仍能与下游
            # 对应的 tool_result 配对; 只有 args payload 被隐藏)。
            if rtype == "assistant" and btype == "tool_use":
                if keep:
                    new_content.append(b)
                else:
                    orig_json = json.dumps(b.get("input") or {}, ensure_ascii=False)
                    new_b = dict(b)
                    new_b["input"] = {
                        "_sculptor_hidden": True,
                        "_original_size": len(orig_json),
                    }
                    new_content.append(new_b)
                    modified_blocks.append(
                        {
                            "record_uuid": rec.get("uuid"),
                            "block_index": bi,
                            "action": "tool_use_stubbed",
                            "tool_use_id": b.get("id"),
                            "tool_name": b.get("name"),
                            "original_size": len(orig_json),
                        }
                    )
                continue

            # user 角色下的 text / image: 锁定, 永远保留
            if rtype == "user" and btype in ("text", "image"):
                new_content.append(b)
                continue

            # assistant text / thinking: 隐藏则直接从 content 删除
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

            # 未知块类型: 原样保留
            new_content.append(b)

        # 如果 assistant record 被剥到没有任何"有效"块, 整条 record 也丢掉。
        # "有效"在这里包含 tool_use -- 一个光秃秃的 tool_use (没有文本)
        # 仍然是合法的, assistant 可以只调工具不说话。
        if rtype == "assistant" and not new_content:
            dropped_uuids.add(rec.get("uuid"))
            continue

        new_rec = _clone_with_sid(rec, new_sid)
        new_rec["message"] = dict(msg)
        new_rec["message"]["content"] = new_content
        new_records.append(new_rec)

    # 第 2 遍扫描: 缝合 parentUuid 链。
    #   * 如果 parent 指向某个被合并的 uuid: 改指到该合并组的合成 record
    #     (这样链条在合成 summary 处重新接上)。
    #   * 如果 parent 指向被删掉的 uuid: 向上一路追溯, 直到找到一个仍然
    #     存活的祖先, 或者一路追到顶变成 null。
    if dropped_uuids or merged_uuids:
        orig_parent: dict[str, str | None] = {}
        for rec in session.records:
            if "uuid" in rec:
                orig_parent[rec["uuid"]] = rec.get("parentUuid")

        # 映射: 任一被合并的 record uuid -> 它所属合并组的合成 record uuid
        merged_to_synth: dict[str, str] = {}
        for m in session.merges:
            for ri in m.record_indices:
                rec = session.records[ri]
                if "uuid" in rec:
                    merged_to_synth[rec["uuid"]] = m.new_uuid

        for rec in new_records:
            parent = rec.get("parentUuid")
            # 如果当前是合成 record, 它的 parent 自己可能也属于另一个更
            # 早的合并组 -- 重定向到那个组的合成 record。
            while parent and parent in merged_to_synth:
                # 只有当目标合成 record 不是当前 rec 本身时才跳, 避免
                # "首个合并组首条 record 的 parent 不在合并组内" 时产生
                # 自环。
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

    # 校验 tool_use / tool_result 配对完整性。
    _validate_pairing(new_records)

    # 写出文件。
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
    """浅克隆 record 并把 sessionId 重写成 new_sid。"""
    nr = dict(rec)
    if "sessionId" in nr:
        nr["sessionId"] = new_sid
    return nr


def _build_synthetic_record(
    *,
    session: Session,
    template_record: dict[str, Any],
    summary_text: str,
    summary_tokens: int,
    new_uuid: str,
    parent_uuid: str | None,
    new_sid: str,
    merged_count: int,
) -> dict[str, Any]:
    """构造一条合成的 assistant text record, 用来替换整个合并组。

    外层信封字段 (isSidechain / userType / entrypoint / cwd / version /
    gitBranch / promptId) 从模板 record (合并组首条) 继承; sessionId /
    uuid / parentUuid / message / timestamp 全部用新值。message 里使用
    一个虚拟 model 名 "sculptor-synthetic" 标识来源。"""
    # 从模板 (合并组首条) 继承外层信封字段, 但 sessionId / uuid /
    # parentUuid / message 用调用方传入的值覆盖。
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
            "usage": {"output_tokens": summary_tokens},
        },
    }


def _tool_result_size(payload: Any) -> int:
    """计算 tool_result payload 的字符数 (含 image base64 体积), 用于
    在桩文本里报告原始 payload 大小。"""
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
    """合理性检查: 每个 assistant tool_use 下游应该有对应的 tool_result。

    这里不强制 raise (Claude Code 自身有时也容忍宽松的链), 只是预留位
    -- 真出现会让 resume 时 API 报错的 flagrant 孤儿才考虑提示。
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
    # 末尾的孤儿 tool_use (最后一条 assistant turn) 是正常的 -- session 可能
    # 正好在工具调用中途被暂停。只在数量多时才考虑提醒。
    if len(orphan_calls) > 1:
        # 不 raise -- Claude Code 自身在 resume 时也能容忍。先留空。
        pass
    _ = orphan_results


# ---------------------------------------------------------------------------
# 启发式 auto 模式
# ---------------------------------------------------------------------------


