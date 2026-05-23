---
name: sculptor
description: 主动管理 Claude Code 对话上下文（context-edit / 裁剪 history / 精简对话）。打开当前项目最新的 session jsonl，用 curses TUI 勾选哪些 tool result / assistant text / thinking 要保留或隐藏，或者用 visual 模式选一段记录调 LLM 摘要合并成一条 synthetic record，输出一个新的 jsonl 让 `claude --resume` 接着干。当用户说"上下文快满了/精简一下/帮我裁剪 history/我自己挑哪些工具结果不要了/把这几步用 LLM 总结成一条/打开 sculptor"或者主动感知到 context pressure 需要在 compact 之前先人工筛选时使用。也支持 `--auto` 模式让 agent 自己按启发式规则做主动 context 管理。配套 ICLR 2026 论文 "Sculptor: Empowering LLMs with Cognitive Agency via Active Context Management" (arXiv:2508.04664)。
---

# sculptor

主入口：

```bash
~/.claude/skills/sculptor/scripts/ce
```

底层 Python：`~/.claude/skills/sculptor/scripts/context_edit.py`

## 何时使用

- 用户说 "上下文/context 快满了"、"帮我裁剪 history"、"精简对话"
- 用户说 "我自己挑哪些 tool result 不要了"、"hide 一些过去的消息"
- 用户准备 `claude --resume` 接着干老 session，但想先扔掉一些没用的中间步骤
- 用户嫌 `/compact` 太黑盒，想自己控制要保留什么
- agent 自己感知到 context pressure（比如被 hook 提醒、context 用量过半），主动跑 `--auto` 模式做粗筛

## 工作模型

每个 Claude Code session 是 `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`，每行一条 record。该 skill 把会话拆成"可勾选的 block"：

| block 类型 | 锁定？ | 隐藏时的行为 |
|---|---|---|
| `user_input` 真用户输入 | 🔒 永远保留 | — |
| `tool_use` Claude 发起的工具调用 | ✓ 可隐藏 | `input` 替换成 `{"_sculptor_hidden": true, "_original_size": N}` 桩；保留 type/id/name，确保跟 tool_result 的配对仍合法 |
| `tool_result` 工具返回 | ✓ 可隐藏 | 内容替换成 `[hidden by sculptor · original size N chars]` 桩，保持 tool_use/tool_result 配对合法 |
| `text` assistant 文本 | ✓ 可隐藏 | 从 record 的 content array 删除；空了则整条 record 也删，自动缝合 `parentUuid` 链 |
| `thinking` assistant 思考 | ✓ 可隐藏 | 同 text |
| 元数据（system / queue-operation / file-history-snapshot / summary 等） | 不显示 | 原样保留 |

除"勾选 / 隐藏"之外还支持 **merge**：visual 模式选一段连续记录，按 `m` 把它们整段送给 LLM 自动摘要，回写时这段会被替换成一条 synthetic assistant text record（带 `[sculptor merged N records → LLM summary]` 前缀，`model: "sculptor-synthetic"`）。merge 范围会自动配平 tool_use/tool_result，跨 user input 会被拒。

**永远不动原文件**。保存时写新 jsonl 到同目录（即 `~/.claude/projects/<encoded-cwd>/<新sid>.jsonl`），同时落一份 `<新sid>.edit-manifest.json` 留痕（含 merge_groups，记录哪几条原 uuid 被替换成了哪个 synthetic uuid）。`claude --resume` 自动能扫到。

## 默认工作流

### 交互模式（用户亲自挑）

```bash
~/.claude/skills/sculptor/scripts/ce                  # 自动选当前 cwd 最新 session
~/.claude/skills/sculptor/scripts/ce path/to/X.jsonl  # 指定 session
```

TUI 快捷键：

| key | 作用 |
|---|---|
| key | 作用 |
|---|---|
| `↑/↓` 或 `j/k` | 移动光标 |
| `PgUp/PgDn` `Home/End` | 翻页 / 跳首尾 |
| `space` | 切换当前 block 的勾选（visual 模式下禁用） |
| `enter` | turn header 上：折叠/展开；非 header 上：弹浮层看完整内容（与 `p` 等价） |
| `p` | 在浮层里看当前 block 的完整内容 |
| `a` | 一键切换所有 tool_results |
| `A` | 一键切换所有 thinking |
| `T` | 一键切换所有 assistant text |
| `u` | 全部恢复成保留 |
| `v` | **进入/退出 visual 模式**（vim 风格，选一段连续 block） |
| `m` | **visual 模式里把所选区间送 LLM 做 merge 摘要** |
| `M` | **一键对每个 user turn 单独调 LLM 总结**（N turns = N 次并发调用，默认 8 并发；默认跳过 <1500 tok 的小 turn 和最近 3 个 turn） |
| `esc` | 取消 visual 选择；非 visual 模式下相当于 `q` |
| `s` | 保存到新 jsonl |
| `q` | 退出（会问是否保存） |
| `?` | 帮助浮层 |

保存后终端会打印新文件路径和带 sid 的 `claude --resume <new-sid>` 命令，直接复制即可恢复。

行尾标签显示 `Nt`（token 数，via tiktoken cl100k_base；assistant blocks 直接取 `message.usage.output_tokens`），不是字符数。

### Merge 工作流（visual 模式 + LLM 摘要）

1. 把光标移到想合并的第一条记录
2. 按 `v` 进入 visual 模式，状态栏会显示 `VISUAL · N rows / M records · press m to merge`
3. `↑/↓` 扩展选区
4. 按 `m`：工具会
   - 自动把选区扩到最近的 user input 边界以内（跨 user input 直接拒绝）
   - 自动配平 tool_use ↔ tool_result（缺哪头就往那个方向扩到把配对补全）
   - 弹确认窗：`Merge N records (~X prompt tokens) via gemini? [y/N]`
5. 按 `y` 后底部状态栏显示 `Calling LLM (gemini) on N records (X chars)…`（**会卡住主线程**，一般 10–40s）
6. LLM 返回后状态栏报 `✓ merged N records into K-char summary`，原区间整段折叠成一行 `[Σ] asst Kc [merged N records] ...`
7. 想反悔可以在 visual 模式外按 `u` 把所有 hide 还原，但 **merge 不可单独撤销**——目前唯一办法是 `q` 不保存然后重开
8. 按 `s` 保存：merge 在新 jsonl 里以一条 synthetic assistant text record 落地，把原 N 条 drop 掉，parentUuid 链自动续上

可在命令行用 `--merge-model gpt` 切换模型（默认 `gemini`，背后调 `~/ask_llm.py`，需要 `ANTHROPIC_AUTH_TOKEN` 环境变量）。也接受任意 `ask_llm.py` 认识的 raw model name（如 `claude-opus-4-7`）。

### 自动模式（agent 自己跑 / 批量预处理）

```bash
~/.claude/skills/sculptor/scripts/ce --auto \
    --drop-tool-results-larger-than 3000 \
    --drop-thinking \
    --drop-failed-bash
```

不进 TUI，按启发式规则直接挑出"明显该删的"，写新 jsonl 并打印路径。可用规则：

- `--drop-tool-results-larger-than N`：把 ≥ N 字符的 tool_result 全 hide（默认 0 = 不触发）
- `--drop-thinking`：所有 thinking 块全删
- `--drop-failed-bash`：tool_result 里看起来像报错的（"error" / "command not found" / "no such file"）

加 `--dry-run` 只算账不写文件；加 `--print-path` 只在 stdout 输出新文件路径，方便脚本拼装。

### 自动 per-turn merge（CLI，给 agent 调用最方便）

```bash
~/.claude/skills/sculptor/scripts/ce --merge-turns \
    --merge-turns-min-tokens 1500 \
    --merge-model gemini
```

对**每个**有 ≥ `--merge-turns-min-tokens` (默认 1500) 个 assistant token 的 user turn，独立调一次 LLM 把整 turn 总结成一条 synthetic assistant text record。每个 turn 只送自己那段内容给 LLM —— **turn 之间互相独立**，所以可以并发：`--merge-turns-concurrency N` 控制最大并发数（默认 8）。20 个 eligible turn @ 8 并发 ≈ 30s 走完，比串行的 5-10 分钟快一个数量级。

不进 TUI，不需交互确认；写新 jsonl 并打印路径。可与 `--drop-*` 规则组合：先 drop 明显垃圾再 per-turn merge。已经在之前手动 merge 过的 record 会被跳过，不会重复 merge。

`--merge-turns-skip-last N`（**默认 3**）保留最近 N 个 user turn 不 merge —— 这几轮通常是 agent 正在用的活上下文，total summary 反而丢必要细节。要 compact 全部就 `--merge-turns-skip-last 0`。

输出的 synthetic record 是一段**完整叙述 agent 干了啥的纯 prose**（不是要点列表）：保留具体文件路径、命令、错误、决策；省略 verbose 工具输出和死路细节。详见 `MERGE_PROMPT_TEMPLATE` in `session_model.py`。

TUI 里同样的功能绑在 `M` 大写键上，有一次性确认弹窗，跑相同的并发实现。

## 给 agent 自己调用的提示

如果 agent 自己感知到上下文压力（比如 hook 提示，或者 user 让 agent "自己看看能不能省点 context"）：

1. 先 `ce --dry-run` 看一眼当前 session 多少 token、多少可压缩
2. 跑 `ce --auto --drop-tool-results-larger-than 5000`（保守起点）做一次粗筛
3. 把新 sid 报告给 user，并提示 user：`cd <project> && claude --resume` 切到新 session

merge 目前只在 TUI 里走（需要 user 自己挑范围 + 确认 LLM 调用）；agent 想批量 merge 时也最好别绕开 TUI——LLM 摘要质量高度依赖人对"这几步该总结成什么"的判断。

不要在用户没明确同意时直接覆盖、也不要往原文件里写——本 skill 设计上永远只产出**新 jsonl**，原 session 始终保留可回溯。

## 风险 & 边界

- 编辑过的 session resume 后偶尔行为差异：被 hide 的 tool_result 桩字符串 Claude 看得到，知道"这里有东西被人工删了"，模型一般会接受不复读
- `parentUuid` 链断裂的兼容性靠 Claude Code 端的容错——已观测能正常 resume，但极端长链下未做过压测
- thinking 块的 `signature` 字段是 server 签名，被删后该 record 不再带 signature；目前看 resume OK（因为整 record 也被删）
- 不要把生成的 `<sid>.edit-manifest.json` 错当 jsonl 读
- **Merge 相关**：
  - synthetic record 用 `model: "sculptor-synthetic"` 标记，正文带 `[sculptor merged N records → LLM summary]` 前缀，方便事后审计
  - LLM 调用是同步阻塞的，TUI 期间会卡几十秒；prompt 超过几千 chars 时建议先 `--dry-run` 估算
  - merge 不能逐条 undo（hide 可以），决定要 merge 前先确认范围
  - `ANTHROPIC_AUTH_TOKEN` 缺失时 LLM 调用会失败并把错误信息写到状态栏；用户没 source 过 `~/.zshrc` 的 raw shell 里别忘 `export`

## 依赖

- Python 3.10+（用了 `X | None` 语法）
- `tiktoken`（真 token 计数，cl100k_base 编码，与全局 CLAUDE.md 约定一致）
- merge 功能依赖 `~/ask_llm.py` 和 `ANTHROPIC_AUTH_TOKEN`

## 参考实现

- `scripts/session_model.py`：jsonl 解析、block 抽取、apply_edits、parentUuid 缝合、tool_use/tool_result 配对校验、tiktoken 包装
- `scripts/context_edit.py`：curses TUI + auto 模式 + CLI 包装
- `scripts/ce`：一行 shell wrapper
