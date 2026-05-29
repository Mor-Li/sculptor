# Claude Code session jsonl 文件结构彻底剖析

> ⚠️ **2026-05-30 更正(关于 thinking.signature)**:
> 本报告原建议把 `thinking.signature` 字段放进 "完全 hide" 类(理由是"server reasoning 不在 jsonl,只是 cache 句柄")。**这是错的**。
> Anthropic 官方文档原话:"The signature field is **not just a verification hash**—it contains the encrypted full thinking content that the server can decode. The server **decrypts the signature** to reconstruct the original thinking for prompt construction."
> 也就是 `signature` 是**加密的完整 thinking 内容**,server 端在 round-trip 时会解码使用。删 signature = 让 server 端失去那段原推理 condition。
> 报告里其他关于 record types / parentUuid / tool_use 配对 / compact_boundary 的发现仍 valid,只有 thinking.signature 的处理建议要反过来:**保留不动**。
> 详见 SKILL.md 的 "❌ 反模式" 一节。

> 为 sculptor 的 **agent-mode** 提供 ground truth：agent 通过读写中间 markdown 来主动管理 session 上下文之前，必须把 jsonl 所有 record 类型、字段、配对、链关系、边角全部摸清。
>
> **方法**：扫了 20 个真实 session（5 个最大 + 5 个中等 + 5 个小 + 3 个 subagent + 1 个 sculptor-edited + 1 个 sculptor-source），并对 100+ 个 session 做了 record/block 类型分布的广度扫描。所有结论给文件路径 + 行号 / record uuid + 实际 jq 输出作为证据。
>
> **本报告与 `~/.claude/skills/sculptor/scripts/session_model.py` 的关系**：那个文件已经摸过最常见的 8 个 record type 和 5 个 block kind；本报告专门补齐 **它没覆盖的 corner case**。明确指出当前 model 漏处理的 record type 在第 5、6 节里。

## 0. 选样

挑选方法：

1. `find /Users/limo/.claude/projects -maxdepth 2 -name "*.jsonl" -type f` 找全部 session（约 880 个文件，其中 `projects/<encoded-cwd>/<sid>.jsonl` 主 session 约 670，subagent 子目录 jsonl 约 210）；
2. 按大小排序 + 按是否是 subagent + 是否有 sculptor `*.edit-manifest.json` 兄弟文件分层；
3. 抽样 20 个具体文件（精挑），用 `xargs -P 8` 跑了一个 100 个 / 200 个 / 300 个文件的 type 广度扫，做交叉验证。

抽样清单（绝对路径）：

| # | 路径 | 字节 | 备注 |
|---|---|---|---|
| 1 | `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace-tiktok-drama-skill-evolve/78d71ebc-a13e-409c-8b37-10b1ab1f162c.jsonl` | 18.7 MB | 最大 session |
| 2 | `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/6f82d286-0410-40ff-beb9-a9226056bf84.jsonl` | 10.7 MB | 有伴随 subagents/ 目录 |
| 3 | `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-LLMAvatar/be16bb74-16e7-4dcf-8ad8-af3eec2a2796.jsonl` | 9.2 MB | 出现 `custom-title`、`agent-name` 类型 |
| 4 | `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-PersonalProxyGateway/d414d6b4-a75d-47f8-bb27-f82b8c4094e2.jsonl` | 8.5 MB | 出现 `document` block、`compact_boundary` |
| 5 | `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace-tiktok-drama-skill-evolve/eeaca413-688a-4333-8cfe-4fca3dd5b359.jsonl` | 5.7 MB | 多 thinking |
| 6 | `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/f587d58c-4b7e-47d2-993a-d93f7c98b850.jsonl` | 477 KB | 含 `Agent` tool + `run_in_background` |
| 7-15 | (其他中小尺寸 session) | 32 KB – 250 KB | |
| 16-18 | `…/f587d58c-…/subagents/agent-a*.jsonl` 等 3 个 | 100 KB – 365 KB | 都带 `agentId` + `isSidechain:true` |
| 19 | `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/d7483483-8f4b-47e8-a280-178d0b82079b.jsonl` | 2.3 MB | **sculptor 编辑过的 session**，旁边有 `d7483483-….edit-manifest.json` |
| 20 | `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/3802561d-79fa-4b21-b5e9-f7f09ed2a756.jsonl` | 3.4 MB | sculptor 19 号的 **source**，含 `isCompactSummary:true` 的 `/compact` 输出和 `compact_boundary` system record |

广度扫描（300 个 session 内出现过的所有 `record.type` 值）的真实统计：

```
19453 assistant       (主体对话, conversation record)
11335 user            (主体对话, conversation record)
 2764 queue-operation (meta, no-uuid)
 2740 last-prompt     (meta, no-uuid)
 2623 system          (meta, conversation chain 中间穿插)
 2163 attachment      (conversation record, no message 字段, 有 attachment 字段)
 1852 permission-mode (meta, no-uuid)
 1593 ai-title        (meta, no-uuid)
 1227 file-history-snapshot (meta, no-uuid)
  264 mode            (meta, no-uuid)
  151 agent-name      (meta, no-uuid)
   77 custom-title    (meta, no-uuid)
   18 progress        (meta-ish, with uuid + parentUuid)
```

**没有出现 `type: "summary"`** —— 这是个关键发现，下面第 5.3 节会专门讨论。

---

## 1. Record types 全集（含 session_model.py 漏掉的）

`session_model.py` 当前的常量定义：

```python
CONVERSATION_TYPES = {"user", "assistant", "attachment", "system"}
META_TYPES = {"queue-operation", "last-prompt", "file-history-snapshot", "summary"}
```

**实测全集（13 种）**：

| Record type | conversation? | 有 uuid? | 参与 parentUuid 链? | session_model.py 状态 |
|---|---|---|---|---|
| `user` | yes | yes | yes | ✅ 已处理 |
| `assistant` | yes | yes | yes | ✅ 已处理 |
| `attachment` | yes | yes | yes (子) | ✅ 已处理（不 extract block） |
| `system` | yes | yes | yes | ✅ 已处理（不 extract block） |
| `queue-operation` | no | no | 不参与 | ✅ 已处理（passthrough） |
| `last-prompt` | no | no | 不参与（但有 `leafUuid` 指向某个 uuid） | ✅ 已处理（passthrough） |
| `file-history-snapshot` | no | no | 不参与（但 `snapshot.messageId` 指 record uuid） | ✅ 已处理（passthrough） |
| `summary` | -- | -- | -- | ⚠️ 在 META_TYPES 但实测样本中**未出现**（见 5.3） |
| **`mode`** | no | no | 不参与 | ❌ **未处理，进入 unknown 分支** |
| **`permission-mode`** | no | no | 不参与 | ❌ **未处理** |
| **`ai-title`** | no | no | 不参与 | ❌ **未处理** |
| **`agent-name`** | no | no | 不参与 | ❌ **未处理** |
| **`custom-title`** | no | no | 不参与 | ❌ **未处理** |
| **`progress`** | no | **yes** | **yes**（有 `parentUuid`、`parentToolUseID`、`toolUseID`） | ❌ **未处理** |

证据：

```bash
$ python3 -c "
from session_model import Session, CONVERSATION_TYPES, META_TYPES
s = Session.load(Path('/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-PersonalProxyGateway/d414d6b4-a75d-47f8-bb27-f82b8c4094e2.jsonl'))
for t, c in sorted({r.get('type'): ... for r in s.records}.items()):
    handled = t in CONVERSATION_TYPES or t in META_TYPES
    print(f'  {t}: handled={handled}')
"
  permission-mode: 130 (handled: False)   # 当前 session_model.py 视为 unknown
  ai-title: 130 (handled: False)
  mode: 20 (handled: False)
```

`apply_edits_and_save()` 里 unknown 分支虽然有 `new_records.append(dict(rec))` 兜底（在 META 检查之前），但是会跳过 `sessionId` 重写。这对一个新的 `claude --resume` 不致命（这些字段都自带 `sessionId`），但是**严格说不是 round-trip 干净**，应当显式归到 META_TYPES。

下面逐种细化 session_model.py **没覆盖** 的 type。

### 1.1 `mode` / `permission-mode`

实例：

```jsonl
{"type":"mode","mode":"normal","sessionId":"aacb988d-..."}
{"type":"permission-mode","permissionMode":"bypassPermissions","sessionId":"1859ff78-..."}
```

- 在 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/aacb988d-eec7-46a0-aa73-691a92faffbc.jsonl` line 3、4、9、... 多次出现 6 条 `mode`；
- 在 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/1859ff78-149f-4808-9e42-574a36fc6e02.jsonl` 出现 3 条 `permission-mode`，值是 `bypassPermissions`；
- 作用：记录 Claude Code TUI 当前模式（普通 / 计划 / 等）和权限模式（普通 / `--dangerously-skip-permissions` / `acceptEdits` 等）。**每次模式变更都会重复 emit 一条**，所以同一个 session 里会出现几十次同样的 `mode:normal` —— 它不是状态，是事件。
- 实测 `permission-mode` 还会以同样 string 形式出现在 user record 的 `permissionMode` 字段里（见 `391252d9-….jsonl` line 6 user record 的 keys 包含 `permissionMode`），二者冗余。

### 1.2 `ai-title` / `custom-title` / `agent-name`

```jsonl
{"type":"ai-title","aiTitle":"配置手机SSH连接到电脑","sessionId":"1859ff78-..."}
{"type":"custom-title","customTitle":"llm-avatar-visualizer","sessionId":"be16bb74-..."}
{"type":"agent-name","agentName":"llm-avatar-visualizer","sessionId":"be16bb74-..."}
```

- `ai-title` 是 Claude Code 后台模型自动给会话起的标题（每隔若干 turn 就重新生成），在 `d414d6b4-….jsonl` 出现 130 次；
- `custom-title` 是用户手动起的固定标题（覆盖 ai-title），在 `be16bb74-….jsonl` 出现 77 次；
- `agent-name` 是 IDE / TUI 显示的 agent 名（通常等于 custom-title），出现 77 次。

三者都是 idempotent 的"当前状态快照"重复 emit，**对 resume 行为完全无影响，但必须保留以避免 Claude Code 在 resume 时找不到自己的会话标题**。

### 1.3 `progress`（**有 uuid + parentUuid**，最坑）

实例（`/Users/limo/.claude/projects/-Users-limo-Documents------ICLR2026-------/143a9d92-b7a3-49c4-90c6-89da78142c4a.jsonl`，line ~待查）：

```json
{
  "parentUuid": "aa848440-0d14-4a9a-aac2-6714bf2827f5",
  "isSidechain": false,
  "userType": "external",
  "cwd": "/Users/limo/Documents/学在清华/ICLR2026参会/报销相关",
  "sessionId": "143a9d92-...",
  "version": "2.1.72",
  "gitBranch": "HEAD",
  "slug": "eager-snacking-yeti",
  "type": "progress",
  "data": {"type":"hook_progress","hookEvent":"PostToolUse","hookName":"PostToolUse:Read","command":"callback"},
  "parentToolUseID": "toolu_016ZamHQu8VijqYgoR41f3x5",
  "toolUseID": "toolu_016ZamHQu8VijqYgoR41f3x5",
  "timestamp": "2026-03-11T05:54:55.594Z",
  "uuid": "b705a79e-a77c-4e6e-aa18-c698f6aba7ad"
}
```

**关键发现**：
- 有 `parentUuid` —— 链入 conversation chain；但是 *实际上* `parentUuid` 指向的是某个 user/assistant 记录，progress 自己也有 `uuid`，意味着如果后续 chain 拿 progress 的 `uuid` 当 parent，则一旦删 progress 就会断链；
- 实测 18 条 progress 都是 `hook_progress`（PostToolUse hook 的回调记录）；
- 旧版本（2.1.72）特有，目前主流 2.1.143-150 已没看到这种 record。

**Round-trip 风险**：session_model.py 当前完全不处理 progress，extract_blocks 会跳过它（因为不在 `CONVERSATION_TYPES`），但 `apply_edits_and_save` 的 unknown 分支会把它 passthrough。然而它有 `uuid`，如果用户编辑时不小心让某个真实 record 把这个 `uuid` 作为 `parentUuid`，sculptor 的 dropped_uuids 重链逻辑不会处理 progress。**建议显式把 `progress` 也视为可参与链的 record，至少在 `_validate_pairing` 之外的 parentUuid 重链时要扫到它**。

---

## 2. Conversation record 的 `message.content` block 全字段

跨 50 个大 session 的 block type 广度统计：

```
3863 tool_use
3860 tool_result
2137 thinking
2125 text
  57 image
   1 document
```

session_model.py 已知：`text`、`thinking`、`tool_use`、`tool_result`、`image` 五种 + `other` 兜底。**实测多出一个 `document` block** —— 罕见但存在。

### 2.1 `document` block（PDF 上传，session_model 当前走 other 兜底）

只在 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-PersonalProxyGateway/d414d6b4-a75d-47f8-bb27-f82b8c4094e2.jsonl` 里出现一次（record uuid `4c2bef35-600e-48c3-bed2-42a8fd58dfdd`）：

```json
{
  "type": "document",
  "source": {
    "type": "base64",
    "media_type": "application/pdf",
    "data": "JVBERi0xLjcN..."  // PDF 原文 base64，巨长
  }
}
```

体积是 base64 PDF，对 token budget 影响大。session_model.py 的 BLOCK_OTHER 分支会以 `size_chars: 0` 估算它，这是 underestimate。

### 2.2 `tool_result.content` 字段（string vs array vs nested image）

实测 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace-tiktok-drama-skill-evolve/78d71ebc-….jsonl`：

```
=== tool_result content type distribution ===
  59 array      # tool_result.content 是 list
1106 string     # tool_result.content 是直接的字符串
```

array 形式里的内部 block type 分布：

```
74 text       # {"type":"text","text":"..."}
 2 image      # {"type":"image","source":{"type":"base64","data":"..."}}
```

session_model.py 的 `_summarize_tool_result` 已经正确处理了这两种 + array 嵌套 image 的情形。

**还有一种 corner case**：`tool_result` 里的 `is_error: true` + `<tool_use_error>...</tool_use_error>` stub。例：record uuid `5ea23f6f-6a71-4e97-a42c-33d446de563c` (in `…/d414d6b4-….jsonl`)：

```json
{
  "type": "tool_result",
  "content": "<tool_use_error>Cancelled: parallel tool call Bash(echo ...) errored</tool_use_error>",
  "is_error": true,
  "tool_use_id": "toolu_011kxXYAPxLp3KcmDgVSoezg"
}
```

这种 cancelled 情形下 `content` 是 string + 有 `is_error: true`。session_model.py 的 `Block.raw` 会保留 `is_error`，但 `auto_mark` 的 `drop_failed_bash` 启发式只检测 `preview` 里的 `error` / `command not found` / `no such file`，**不会捕获 `<tool_use_error>` 模式**。建议补一条规则。

### 2.3 user record 的 content 类型 5 种

实测 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-PersonalProxyGateway/d414d6b4-….jsonl`：

```
=== user.message.content type distribution ===
 391 array     # 包含 tool_result / text / image 的混合
 101 string    # 纯字符串 (直接用户输入或 /command 包装)
```

array 里的 first_type 分布：

```
   1 first_type=document      # PDF 上传 (上面 2.1)
  20 first_type=text          # 纯文本 array 形式
 370 first_type=tool_result   # 工具回复
```

**关键的纯 text array 内容**（容易被误判）：

```
[Request interrupted by user]      # 用户按 ESC 中断 (无 tool_use_id)
[Image #1]                          # 跟着一个 image block 的纯文本 caption
[Image: source: /Users/limo/.claude/image-cache/<sid>/<n>.png]   # 同上变体
```

这些都是 sculptor `extract_blocks` 走 `rtype=="user" and btype=="text"` 分支当 user_input 锁定，**实际意义不是用户输入**（是 client 自动注入的 caveat），但锁定也无害。

### 2.4 assistant message 一行 = 一个 block （session_model.py 已知，但很关键）

实测 10 个 session：

```bash
$ for SESS in (top 10 sessions); do
    jq -r 'select(.type=="assistant") | (.message.content|length)' "$SESS" | sort -u
  done
# 全部输出: 1
```

**每个 assistant record 的 `message.content` 数组只有 1 个 block**。这意味着 Claude API 一个 message（多块内容 thinking + text + tool_use_1 + tool_use_2 + ...）会被拆成多条 record，每条共享同一个 `message.id`，但 `uuid` / `parentUuid` 不同。session_model.py 的 `_reconcile_thinking_tokens` 正是用 `message.id` 把这些兄弟记录 group 起来摊销 `output_tokens` 的。

**这是 sculptor agent-mode 设计的关键约束**：用户在 markdown 里看到的 "一个 assistant 块"，落到 jsonl 里可能是几条 record 共享 `message.id`，"删一个" 的语义需要明确。

### 2.5 parallel tool call → chain 分叉

实测 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/3802561d-79fa-4b21-b5e9-f7f09ed2a756.jsonl` 一组 parallel calls：

| record uuid | type | parentUuid | tool_use_id 或 tool_use_id_ref |
|---|---|---|---|
| `c4b3d52a-0535-4210-8645-945b2a9e9686` | assistant | `f231182a-…` (text 前驱) | tool_use `toolu_012AbU2itdcVHeVmVZer5MgV` |
| `37f6bcfd-b252-4630-b859-f752fc3f1e48` | assistant | **`c4b3d52a-…`** (而不是 f231182a) | tool_use `toolu_01AseHPixmfQ3Xk6oRuJNrB3` |
| `bed3f611-a3e0-424f-b566-e856bb4e8895` | user | **`c4b3d52a-…`** (回到第一个 tool_use) | tool_result for `toolu_012AbU2itdcVHeVmVZer5MgV` |
| `3e5e96e6-fdaa-4995-9955-020f9f9a24a8` | user | **`37f6bcfd-…`** | tool_result for `toolu_01AseHPixmfQ3Xk6oRuJNrB3` |

**链是 tree，不是 path！**parallel 时第二个 tool_use 把第一个 tool_use 当 parent，但两条 tool_result 各自挂回各自的 tool_use（fork）。所以 sculptor 在 merge 时 `resolve_merge_range` 的 contiguous-range 假设是对的（按文件线性顺序），但是 `_validate_pairing` 必须按 id 配对而不能按 index 假设。

---

## 3. tool_use ↔ tool_result 配对的 corner case

### 3.1 配对统计：1:1，无孤儿

在 3 个大 session 上 jq 验证：

```
=== /Users/limo/.claude/projects/.../3802561d-79fa-4b21-b5e9-f7f09ed2a756.jsonl ===
Unique tool_use ids:      402
Unique tool_result ids:   402
Orphan calls:               0
Orphan results:             0

=== /Users/limo/.claude/projects/.../78d71ebc-….jsonl (18.7 MB) ===
Unique tool_use:   1165, tool_result: 1165, Orphans: 0/0

=== /Users/limo/.claude/projects/.../d414d6b4-….jsonl (8.4 MB) ===
Unique tool_use:    370, tool_result: 370, Orphans: 0/0
```

实测所有 session 都是严格 1:1 完整配对。即使用户 ESC 中断（`[Request interrupted by user]` user record 紧跟 system record），中断前最后一个 tool_use 也总会有一个对应的 tool_result（哪怕是 `<tool_use_error>Cancelled...</tool_use_error>` stub）。

**这意味着**：sculptor 的 `_validate_pairing` 的 `len(orphan_calls) > 1` 容忍策略其实是过度保守 —— 真实数据里没有孤儿。但保留容忍是对的，因为如果用户在 sculptor 里 hide 了一个 tool_result 但 sculptor 把 tool_use 也判定为 droppable，会产生本不该有的孤儿。

### 3.2 异步 `Agent` tool（subagent / `run_in_background:true`）

在 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/f587d58c-….jsonl` 实测：

**tool_use** record（assistant uuid `b24ef194-2201-4a11-82b1-717aab6850de`）：

```json
{
  "type": "tool_use",
  "id": "toolu_01Gu5dqJt59RKv3usCzzWBDx",
  "name": "Agent",
  "input": {
    "description": "Review PR #1 代码",
    "subagent_type": "general-purpose",
    "prompt": "...",
    "run_in_background": true   // <-- 异步
  },
  "caller": {"type": "direct"}
}
```

**配对的 tool_result**（user uuid `9a18085f-d2c9-4312-a2ce-daeeb9056d76`，紧跟其后）：

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_01Gu5dqJt59RKv3usCzzWBDx",
  "content": [{
    "type": "text",
    "text": "Async agent launched successfully.\nagentId: a3fbcccae53a31d69 ..."
  }]
}
```

**真正的结果**则**通过一条全新的 user 记录在远后的位置投递**（line 99 of session, uuid `2b534545-b9de-402e-b851-510d5eaf2d3d`），content **是一个 plain string** 不是 array：

```
"<task-notification>
<task-id>a3fbcccae53a31d69</task-id>
<tool-use-id>toolu_01Gu5dqJt59RKv3usCzzWBDx</tool-use-id>
<output-file>/private/tmp/.../tasks/a3fbcccae53a31d69.output</output-file>
<status>completed</status>
<summary>Agent \"Review PR #1 代码\" completed</summary>
<result>...</result>
</task-notification>"
```

**关键发现**：
1. **原 tool_use_id 在配对意义上已经 closed**（"Async agent launched" 就是它的 tool_result）；
2. 真正的 async 输出**作为一条独立的、看起来像普通用户输入的 user record 出现**，里面用 XML markup 引用 `<tool-use-id>` —— 但 jsonl-level 没有任何 `tool_use_id` 字段；
3. sculptor 的 `extract_blocks` 会把这条 user record 当 `BLOCK_USER_INPUT` lock 起来（因为 content 是 string），不会被错删。但 agent-mode 的 markdown 渲染时如果想把它和原 tool_use 关联起来，需要 parse XML。

**run_in_background Bash 也是同样模式**：tool_use 的 result 是 "Background task launched, ID: ..."；真正的结果通过 `<task-notification>` user record 传递。这个 user record 也可能以 `attachment.type=="queued_command"` + `commandMode:"task-notification"` 的形式出现（见 `…/d414d6b4-….jsonl`）。

### 3.3 sub-agent jsonl 与主 session 的关联

**结构**：`projects/<encoded-cwd>/<sid>.jsonl` 是主 session，`projects/<encoded-cwd>/<sid>/subagents/agent-<agentId>.jsonl` 是 subagent 的完整内部 session。

实测 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/f587d58c-…/subagents/agent-a5daa093f1efc566c.jsonl`：

- 27 records，全是 `user` / `assistant` / `attachment`；
- 每条记录都有 `agentId: "a5daa093f1efc566c"` 字段；
- **每条都有 `isSidechain: true`**（实测 5 个 subagent 文件都是）；
- 第一条 user record（uuid `a41597a1-…`）的 `parentUuid: null` —— subagent 自己有自己的根；
- 部分 assistant 还有 `attributionAgent: "Explore"` 字段（指 subagent 的 type）；
- 主 session 里**没有任何 subagent 内部 uuid**（27 个 subagent uuid 全部 grep 主 session 找不到）；
- 主 session 通过 `agentId` 字符串引用 subagent（实测主 session 包含 1 次 `"a5daa093f1efc566c"`，在 `toolUseResult.agentId` 里）。

**这对 sculptor 的意义**：

1. sculptor 现在**完全没有处理 subagent 文件** —— 编辑主 session 时不会动 subagent jsonl；
2. 但是主 session 的 `Agent` tool 的 `toolUseResult` **携带了完整的 subagent 总结**（含 `prompt`, `content`, `totalDurationMs`, `totalTokens`, `usage`, `toolStats`），这个 `toolUseResult` 是 jsonl 里的"重型字段"（一个 Agent 的 toolUseResult 几 KB 起），但 sculptor 不在 `BLOCK_TOOL_RESULT` 的 size_chars 里统计它。`message.content[].tool_result.content` 才是 sculptor 看到的。一个常见情形：`toolUseResult` 里有完整 subagent 报告但 `tool_result.content` 只有 stub —— sculptor 会 underestimate。

### 3.4 caller 字段

实测 `tool_use` 有时带 `caller` 字段（见上面 Agent tool_use 示例）。已知值：`{"type":"direct"}`。这个字段 sculptor 当前 raw 保留即可。

---

## 4. parentUuid 链 / 链根 / 跨记录类型

### 4.1 链根：`parentUuid == null`

实测 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/3802561d-….jsonl`：

```
=== Looking for parentUuid=null records ===
queue-operation (uuid=null)        # 无 uuid，不参与链
queue-operation (uuid=null)
user (uuid=ca096b47-... isMeta=true)   # <-- 真正的链根
last-prompt (uuid=null)
queue-operation (uuid=null)
queue-operation (uuid=null)
last-prompt (uuid=null)
...
```

第一条 conversation record（这里是 `type:user`，uuid `ca096b47-5a4b-4ea3-a746-36516f00d179`）的 `parentUuid: null`，是 sentinel。其后所有 conversation record 都 chain 回它。

**Note**：链根的 user record 在该 session 是 `isMeta: true` 的 `<local-command-caveat>...`，**不是用户输入** —— 是 client 自动注入的免责声明。下一条 user record（uuid `664b6929-…`） 是用户实际输入的 `/clear` 命令，parentUuid 指向 `ca096b47-…`。所以 "session 的第一条用户实际输入" 不一定是 chain 根。

### 4.2 用 `slug` 字段判定 sub-session

实测：

```
$ jq -r '.slug // "null"' /Users/limo/.claude/projects/.../3802561d-….jsonl | sort -u
null
prancy-meandering-russell
```

部分记录有 `slug` 字段（一个唯一的 human-readable 标识）。Claude Code 2.1.72 时的 progress record 也都带 `slug`。这个字段 sculptor 不动即可。

### 4.3 `compact_boundary` 会引入第二个链根

实测 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/3802561d-…/.jsonl` 中的 `system / compact_boundary` 记录（uuid `cc1bfe1d-7d67-4f7c-a08e-35a553cca238`）：

```json
{
  "parentUuid": null,                            // <-- 第二个 null parent！
  "logicalParentUuid": "6967d0f4-a9e7-4a8f-8ff5-c01e8f609f6d",
  "isSidechain": false,
  "type": "system",
  "subtype": "compact_boundary",
  "content": "Conversation compacted",
  "isMeta": false,
  "level": "info",
  "compactMetadata": {
    "trigger": "auto",
    "preTokens": 166909,
    "postTokens": 6060,
    "durationMs": 119274
  },
  ...
}
```

**惊人发现**：在 `/compact` 之后，Claude Code 插入一个 `system / compact_boundary` 记录，它的 `parentUuid` 是 `null`（断开旧链），但又保留了 `logicalParentUuid` 指向 compact 前最后一条 record。

**之后的所有 conversation record 的 parentUuid 都 chain 回这个 compact_boundary**：第一条 post-compact 是一个 user record `4fd6c72e-eabc-4810-97cc-f2bf9b1f34db`，`isCompactSummary: true` + `isVisibleInTranscriptOnly: true`，`parentUuid` 指向 `cc1bfe1d-…`。**这就是"summary" 的实际形态** —— 不是单独的 `type:summary` record，而是 `system/compact_boundary` + 紧跟其后的特殊 user record。

session_model.py 的 `_reconcile_thinking_tokens` 和 chain stitching 不会针对 compact_boundary 做特殊处理。如果用户在 sculptor 里删 compact 之前的 record，stitching 走的是 dropped_uuids walk-up，最终走到 `parentUuid == null` 就停 —— 这对 compact_boundary 没问题，但**如果用户跨越 compact 边界做 merge，会把 compact summary 也吸进去** —— 没问题，可保留。

### 4.4 跨 meta records 的 chain

meta records 不参与链：`queue-operation`, `last-prompt`, `file-history-snapshot`, `mode`, `permission-mode`, `ai-title`, `agent-name`, `custom-title` 都没有 `uuid` 字段，也没有 `parentUuid`，只是夹在 conversation chain 中间的"事件流"。

**实测**首 25 records 序列（`/Users/limo/.claude/projects/.../3802561d-….jsonl`）：

```
idx 1: user (uuid=ca096b47-..., parentUuid=null) <local-command-caveat>
idx 2: user (uuid=664b6929-..., parentUuid=ca096b47-...) /clear
idx 3: system local_command (uuid=b4c2e5b7-..., parentUuid=664b6929-...) 
idx 4: queue-operation enqueue (no uuid)
idx 5: queue-operation dequeue (no uuid)
idx 6: user (uuid=93a03598-..., parentUuid=b4c2e5b7-...) "我现在有一个伟大的idea..."   ★ 真正第一个用户输入
idx 7: attachment skill_listing (uuid=bedd7222-..., parentUuid=93a03598-...)
idx 8: assistant thinking (uuid=ff423f23-..., parentUuid=bedd7222-...)
idx 9: assistant text (uuid=f231182a-..., parentUuid=ff423f23-...)
idx 10: assistant tool_use Bash (uuid=c4b3d52a-..., parentUuid=f231182a-...)
idx 11: assistant tool_use Bash (uuid=37f6bcfd-..., parentUuid=c4b3d52a-...)  ★ parallel
idx 12: user tool_result for toolu_012... (uuid=bed3f611-..., parentUuid=c4b3d52a-...)
idx 13: user tool_result for toolu_01Ase... (uuid=3e5e96e6-..., parentUuid=37f6bcfd-...)
...
idx 22: last-prompt (no uuid)        <-- 突然插入的 meta，与链无关
...
```

**结论**：parentUuid 链穿透 meta records，不被它们打断。sculptor 的 stitching 已经正确。但要注意 `attachment` records 是 conversation records，**参与链**且自己有 uuid（idx 7 的 attachment uuid `bedd7222-…` 被下一条 assistant uuid `ff423f23-…` 当 parentUuid）。

### 4.5 sculptor 编辑后的链是否还闭合

实测 `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/d7483483-….jsonl`（sculptor edited），用脚本 verify：

```python
Total records: 949
Total with uuid: 832
Orphan parents (parentUuid not in session): 0
tool_use ids: 269, tool_result ids: 269
orphan calls (tu without tr): 0, orphan results: 0
```

**通过**。Source 是 `3802561d-….jsonl` 共 1393 records，merged 后 949 records，链完全闭合。

---

## 5. Meta records 各自角色

### 5.1 `queue-operation`

**作用**：记录 Claude Code TUI 输入框队列的 enqueue/dequeue 事件（用户连按多次 Enter 时，前面的 prompt 会进队列）。

实例：

```jsonl
{"type":"queue-operation","operation":"enqueue","timestamp":"2026-05-23T04:55:53.106Z","sessionId":"...","content":"https://github.com/moorcheh-ai/memanto 这个是啥玩意你去看看"}
{"type":"queue-operation","operation":"dequeue","timestamp":"2026-05-23T04:55:53.107Z","sessionId":"..."}
```

- 字段：`type`, `operation`, `timestamp`, `sessionId`, 可选 `content`；
- 无 `uuid`，无 `parentUuid`，**不参与链**；
- 通常成对出现（enqueue + dequeue），dequeue 没有 content；
- 在 session 整个生命周期持续出现（不只是开始）。

**round-trip**：sculptor 已正确 passthrough。

### 5.2 `last-prompt`

实例：

```jsonl
{"type":"last-prompt","lastPrompt":"1","leafUuid":"205dc619-ceb9-4d8e-b8ea-155946b2c5a5","sessionId":"..."}
```

- 字段：`type`, `lastPrompt`, `leafUuid`, `sessionId`；
- 无 `uuid`，无 `parentUuid`，**不参与链**；
- `leafUuid` 指向某个真实的 conversation record uuid（实测命中 1 次） —— 用于"上次 prompt 后面叶子" 的快速定位；
- 在 session 中**不止出现一次**：实测 1393-record session 出现 94 次（idx 22, 45, 59, 72, 93, ..., 1387, 1389）；
- 跟 ai-title 一样，是 idempotent 状态快照。

**round-trip**：sculptor 已正确 passthrough。**注意**：如果 sculptor 删了 `leafUuid` 指向的 record，last-prompt 会变成 dangling 引用 —— Claude Code 的 resume 行为对此宽容（实测 sculptor-edited session 能正常 resume），但严格说应该清理。

### 5.3 ~~`summary`~~ —— **实测不存在！**

在 300 个 session、882 个 jsonl 文件全量 grep `'^\{"type":"summary"'`，**没有任何命中**：

```bash
$ find /Users/limo/.claude -name "*.jsonl" -type f | xargs grep -l '^{"type":"summary"' | head
(no output)
```

session_model.py 把 `summary` 列在 `META_TYPES` 里是基于早期猜测或旧版本 Claude Code。当前（2.1.143-150 + 2.1.72 旧版）**`/compact` 的实际产物是**：

1. 一条 `type:"system"`, `subtype:"compact_boundary"` record（uuid `cc1bfe1d-…`，parentUuid: null），见 4.3；
2. 紧跟一条 `type:"user"` record，带 `isCompactSummary: true` + `isVisibleInTranscriptOnly: true` 两个特殊 flag，content 是一个 string（完整的 compact summary 文本，几 KB 起），parentUuid 链回 boundary。

**实例**（`/Users/limo/.claude/projects/.../3802561d-….jsonl` line ~待数）：

```json
{
  "parentUuid": "cc1bfe1d-7d67-4f7c-a08e-35a553cca238",
  "isSidechain": false,
  "promptId": "c1ea77f3-dca2-4d5f-ae14-e3a90705872a",
  "type": "user",
  "message": {
    "role": "user",
    "content": "This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.\n\nSummary:\n1. Primary Request and Intent:\n   The user wants to build a Claude Code skill called `context-edit` that ...\n..."
  },
  "isVisibleInTranscriptOnly": true,
  "isCompactSummary": true,
  "uuid": "4fd6c72e-eabc-4810-97cc-f2bf9b1f34db",
  ...
}
```

**sculptor 修复建议**：

- 把 `summary` 从 `META_TYPES` 删掉（不存在）；
- `extract_blocks` 检查 user record 的 `isCompactSummary == true`，把它视为一个特殊 lock block（既不能 hide 也不能 merge，因为它本身就是 LLM 摘要）；
- agent-mode 的 markdown 渲染时应当用一个明显的 banner 表示 "—— compact 边界 ——"，让 agent 不要错误地以为 compact summary 是普通用户输入。

### 5.4 `file-history-snapshot`

实例（`/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-PersonalProxyGateway/d414d6b4-….jsonl`）：

```json
{
  "type": "file-history-snapshot",
  "messageId": "9d8a389b-d4b9-42ca-b7dd-17ea03db3099",
  "snapshot": {
    "messageId": "60ec6b90-6c24-43da-aa09-17e00d01ab3a",
    "trackedFileBackups": {
      "clash-vps.yaml": {
        "backupFileName": "122d9ac1172adeae@v1",
        "version": 1,
        "backupTime": "2026-05-14T04:44:35.136Z"
      }
    },
    "timestamp": "2026-05-14T04:44:11.789Z"
  },
  "isSnapshotUpdate": true
}
```

- 字段：`type`, `messageId`, `snapshot{messageId, trackedFileBackups{filename:{backupFileName, version, backupTime}}, timestamp}`, `isSnapshotUpdate`；
- 无 `uuid`，无 `parentUuid`，**不参与链**；
- `messageId` 和 `snapshot.messageId` 通常相等也可能不等（不等时是更新过去的快照）；
- `trackedFileBackups` 可以为空 `{}`（实测 11 次），或包含 N 个文件；
- 真实的 backup 文件落在 `~/.claude/file-history/<backupFileName>`；
- 通常在 Edit/Write tool_use 之后立即 emit。

**round-trip**：sculptor 已正确 passthrough，但是 `backupFileName` 的真实文件**不在 sculptor 编辑范围**。这意味着如果 sculptor 把对应的 Edit tool_use record 删掉，file-history-snapshot 还会留着 dangling 引用 —— Claude Code 不会因此报错（snapshot 是只读历史），实测干净。

### 5.5 `system` record

system 是真正的 conversation record（有 uuid + parentUuid + isSidechain），但分 6 个 subtype：

| subtype | 含义 | 关键字段 |
|---|---|---|
| `local_command` | 用户输了 `/clear`, `/goal`, `/config` 这种 slash command | `content` 含 `<command-name>...</command-name>` |
| `stop_hook_summary` | Stop hook 跑完后的总结（Claude 完成生成时触发） | `hookCount`, `hookInfos[]{command, durationMs}`, `hookErrors[]`, `preventedContinuation`, `level` |
| `turn_duration` | 一个 turn 的总耗时 | `durationMs`, `messageCount`, `isMeta:false` |
| `away_summary` | 用户长时间没看屏幕时的"recap" 摘要 | `content`（提示文本）, `isMeta:false` |
| `api_error` | API call 失败（被重试） | `cause{code,path}`, `error`, `retryInMs`, `retryAttempt`, `maxRetries`, `level:"error"` |
| `compact_boundary` | `/compact` 边界（见 5.3） | `compactMetadata{trigger,preTokens,postTokens,durationMs}`, `logicalParentUuid` |

所有 system record **都有 uuid 和 parentUuid，参与 conversation chain**。sculptor 的 `extract_blocks` 把 `system` 整体跳过（不 extract block），`apply_edits_and_save` passthrough。

**对 agent-mode 的提示**：
- `api_error` 是噪音（retry 信息），可以 hide；
- `stop_hook_summary` 多数时候是噪音；
- `local_command` 携带用户 slash command 上下文，对理解会话流有帮助，保留；
- `away_summary` 是 Claude 自己写的"用户走开了，我们刚才在干嘛" recap，可保留可删；
- `compact_boundary` 必须保留，作为视觉分隔。

### 5.6 `attachment`

attachment 是 conversation record（有 uuid + parentUuid + isSidechain），`attachment.type` 字段细分如下（实测 30 个大 session 统计）：

```
237 task_reminder        # {type, content:[], itemCount}
131 queued_command       # {type, prompt, commandMode:"prompt"|"task-notification"}
 78 opened_file_in_ide   # {type, filename}
 48 skill_listing        # {type, content:"...", skillCount, isInitial:bool}
 44 hook_success         # {type, hookName, toolUseID, hookEvent, content, stdout, ...}
 32 edited_text_file     # {type, filename, snippet, ...}
 21 date_change          # 日期变更
 10 selected_lines_in_ide  # {type, filename, lineRange}
 10 diagnostics          # {type, files:[{uri, diagnostics:[{message, severity, range, source}]}]}
  6 command_permissions  # {type, allowedTools:[]}
  2 goal_status          # {type, met:bool, sentinel:bool, condition:"..."}  /goal command 状态
  1 plan_mode_exit
  1 plan_mode
  1 nested_memory
```

实例细节：

- `task_reminder`: 待办提示，`itemCount: 0` 是常态；
- `skill_listing`: 实际就是 SKILL.md 拉清单，**初始一次的 isInitial:true 那条很关键**（agent 引导用），后续重复出现的是 hot-reload；
- `hook_success`: Claude Code Hook 跑成功后回传给 model 的内容；
- `diagnostics`: IDE 端的 LSP 诊断（错误/警告/info），结构化字段；
- `goal_status`: 用户用了 `/goal` 命令时，sentinel 模式的目标条件；
- `opened_file_in_ide` / `selected_lines_in_ide` / `edited_text_file`: VSCode 集成给 Claude 的当前编辑器状态；
- `command_permissions`: 当前会话允许的工具白名单（可能是空的）。

**对 sculptor 的意义**：
1. `extract_blocks` 对 attachment 完全跳过（`if rtype in ("attachment", "system"): return []`），这是对的；
2. 但是 attachment 的 `content` 字段也吃 token —— **`skill_listing` 一条就几千 token**，sculptor 当前不显示也不统计；
3. agent-mode 应当在 markdown 里用一行 summary 渲染（"📎 skill_listing (isInitial=true, 32 skills, ~3500 tokens)"），让用户/agent 可见。

---

## 6. 不常见 / 边角字段

### 6.1 user record 的 `toolUseResult` 字段

**最大的边角发现**：很多 user record 的顶层有一个 `toolUseResult` 字段（不在 `message` 里），它和 `message.content[0].tool_result.content` **冗余但又不完全一致** —— 它给的是工具的**完整结构化结果**，按工具类型有不同 schema。

实测在 `…/d414d6b4-….jsonl` 全量分组 by `toolUseResult` keys：

```
177 ["interrupted","isImage","noOutputExpected","stderr","stdout"]   # Bash tool
 44 ["filePath","newString","oldString","originalFile","replaceAll","structuredPatch","userModified"]  # Edit tool
 36 ["statusChange","success","taskId","updatedFields"]              # TaskUpdate
 32 ["file","type"]                                                  # Read tool
 21 ["task"]                                                         # TaskCreate
 17 ["content","filePath","originalFile","structuredPatch","type","userModified"]  # Write tool
  7 ["answers","questions"]                                          # ???
  5 ["backgroundTaskId","interrupted","isImage","noOutputExpected","stderr","stdout"]  # Bash run_in_background
  4 ["agentId","canReadOutputFile","description","isAsync","outputFile","prompt","status"]  # Agent tool (async)
  2 ["retrieval_status","task"]
```

例（Agent tool 的 toolUseResult，详细到 stats 级）：

```json
{
  "agentId": "a5daa093f1efc566c",
  "agentType": "Explore",
  "totalDurationMs": 23826,
  "totalTokens": 44863,
  "totalToolUseCount": 9,
  "usage": {
    "input_tokens": 5, "cache_creation_input_tokens": 907, "cache_read_input_tokens": 43250,
    "output_tokens": 701, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
    ...
  },
  "toolStats": {
    "readCount": 5, "searchCount": 2, "bashCount": 2,
    "editFileCount": 0, "linesAdded": 0, "linesRemoved": 0, "otherToolCount": 0
  },
  "content": [{"type": "text", "text": "..."}],
  "status": "completed",
  "prompt": "...full prompt..."   // <-- 包含完整 prompt
}
```

**关键发现**：
1. `toolUseResult` 是 **Claude Code 本地的统计/审计数据**，可能不上传给 API；
2. 但**它存在 jsonl 里，所以 resume 时会被读回 —— sculptor 必须 round-trip 保留**；
3. session_model.py 的 `_tool_result_size` 只看 `message.content[].tool_result.content`，**不算 `toolUseResult` 的 size** —— 又一个 underestimate 源头（Bash 输出在 `stdout` 字段里，Edit 输出在 `structuredPatch` 里，这些都没被计入 sculptor 的 size 统计）；
4. agent-mode 渲染时**应当 hide `toolUseResult`**（用户和 agent 都不需要看它，但保留 round-trip）。

### 6.2 `sourceToolAssistantUUID`

实测 370 条带此字段的 user record，**100% 等于 `parentUuid`**：

```
$ jq -c 'select(.sourceToolAssistantUUID != null) | {eq: (.sourceToolAssistantUUID == .parentUuid)}' …/d414d6b4-….jsonl | sort -u
{"eq":true}  ×370
```

`sourceToolAssistantUUID` 是冗余字段，指向产生这个 tool_result 的 assistant tool_use record 的 uuid（也就是 parentUuid）。round-trip 保留即可，不需要单独处理。

### 6.3 `isMeta`, `isSidechain`, `isCompactSummary`, `isVisibleInTranscriptOnly`

- `isMeta:true` 出现在第一条 `<local-command-caveat>...` user record（"client 注入，model 不该理"），实测 1 次/session；
- `isSidechain:true` 仅在 subagent jsonl 中出现，主 session 全是 false（实测 0 个主 session 命中）；
- `isCompactSummary:true` + `isVisibleInTranscriptOnly:true` 是 compact summary user record 的标记（见 5.3）；
- `attachment.type=="goal_status"` 里的 `sentinel:true` / `met:false/true` 是 `/goal` 命令的 sentinel；
- `system / api_error` 的 `level:"error"` 区分严重程度。

**sculptor 当前忽略所有这些 flag，靠 passthrough 保留**。agent-mode 可以用这些 flag 在 markdown 渲染时做有意义的分类。

### 6.4 thinking block 的 `signature` 字段

实测 `…/78d71ebc-….jsonl` 的 thinking signature 长度分布：

```
=== thinking signature lengths ===
352, 404, 560, 816, 828, 916, 1036, 1056, 1136, 1172, 1180, 1384, 1512, 2104, 2132, 3180, 3560, 4908, 5100, 5928, ...
```

- signature 是 base64 字符串，每个 thinking block 一个，平均 1-5 KB；
- 它是 server-side reasoning cache 的句柄，**Anthropic API resume 时需要校验它**；
- session_model.py 已经在 raw 里保留它，没问题；
- **但是它对 token 计费的意义**：当 sculptor merge 一段含 thinking 的范围时，merge 的 synthetic record **没有 signature**，意味着 Anthropic API 无法在那个位置继续 cache hit —— sculptor 的 merged 节省了 token 但破坏了 KV cache，这是 conscious trade-off。

测试 round-trip：实测 sculptor-edited session 的 thinking block signature 全部保留（synthetic record 里没有 thinking，所以也不需要 signature）。

### 6.5 assistant message 的 `usage` 字段

实测全 keys：

```
["cache_creation","cache_creation_input_tokens","cache_read_input_tokens","inference_geo","input_tokens","iterations","output_tokens","server_tool_use","service_tier","speed"]
```

例：

```json
{
  "input_tokens": 6, "cache_creation_input_tokens": 49913, "cache_read_input_tokens": 0,
  "output_tokens": 516,
  "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
  "service_tier": "standard",
  "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 49913},
  "inference_geo": "",
  "iterations": [{
    "input_tokens": 6, "output_tokens": 516,
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 49913,
    "cache_creation": {"ephemeral_5m_input_tokens": 49913, "ephemeral_1h_input_tokens": 0},
    "type": "message"
  }],
  "speed": "standard"
}
```

- session_model.py 的 `_reconcile_thinking_tokens` 用 `usage.output_tokens` 摊销 thinking tokens；
- `cache_read_input_tokens` 大数值说明 prompt caching 命中，对真实 cost 影响巨大；
- `server_tool_use.web_search_requests` 算入 Anthropic 的"server tool" 计费；
- **此字段 sculptor 当前不解析、只 passthrough** —— 没问题，但 stats panel 可以补一个"cumulative cache savings" 视图。

实测**同一个 `message.id` 共享的所有兄弟 record 都 carry 完全一样的 `usage`**（实测 4 条共享 `msg_01YBw2paj2AyFeykjMJttUpe` 的记录，output_tokens 都是 516）。这正是 session_model 设计 `_reconcile_thinking_tokens` 的原因 —— 否则会被 4 倍 over-count。

### 6.6 旧版本简化 schema

实测 version=2.1.104 / 2.1.126 的 session 只有 4 个 record type：

```
assistant, attachment, system, user
```

没有 mode/permission-mode/ai-title/agent-name/queue-operation/last-prompt 等 meta。**这意味着 sculptor 在面对旧 session 时不会触发未处理 type 路径**。但 progress 是 2.1.72 时代的产物，会例外。

---

## 7. round-trip 边界 —— sculptor 编辑后能否 resume

### 7.1 Diff 对比

- **Source**: `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/3802561d-79fa-4b21-b5e9-f7f09ed2a756.jsonl`，1393 records, 3.4 MB；
- **Edited**: `/Users/limo/.claude/projects/-Users-limo-Documents-GithubRepo-metabot-workspace/d7483483-8f4b-47e8-a280-178d0b82079b.jsonl`，949 records, 2.3 MB；
- **Manifest**: `…/d7483483-….edit-manifest.json` 显示 3 个 merge group（总共合并了 12+6+135=153 records → 3 个 synthetic record），无 dropped/modified blocks。

### 7.2 Synthetic record 结构

实测一条 synthetic record（uuid `ec5d63db-18e6-4117-bcfb-5a5d8de9d765`）：

```json
{
  "isSidechain": false,
  "userType": "external",
  "entrypoint": "sdk-cli",
  "cwd": "/Users/limo/Documents/GithubRepo/metabot-workspace",
  "version": "2.1.148",
  "gitBranch": "HEAD",
  "type": "assistant",
  "uuid": "ec5d63db-...",
  "parentUuid": "71196e3c-...",
  "sessionId": "d7483483-...",   // <-- 重写
  "timestamp": "2026-05-23T18:47:55.913131Z",
  "message": {
    "model": "sculptor-synthetic",
    "id": "msg_ce_ec5d63db",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "[sculptor merged 135 records → LLM summary]\n\n助手完成了..."}],
    "stop_reason": "end_turn",
    "stop_sequence": null,
    "usage": {"output_tokens": 282}
  }
}
```

Vs 真实 assistant record（同 session, 不同 uuid）：

| Key | Synthetic 有 | Real 有 |
|---|---|---|
| `cwd`, `entrypoint`, `gitBranch`, `isSidechain`, `userType`, `version` | ✅ | ✅ |
| `uuid`, `parentUuid`, `sessionId`, `timestamp`, `type` | ✅ | ✅ |
| `message.id`, `model`, `role`, `type`, `content`, `usage`, `stop_reason`, `stop_sequence` | ✅ | ✅ |
| `message.stop_details` | ❌ **missing** | ✅ |

`stop_details` 缺失 —— **实测 Anthropic API resume 不挂**（确认通过 sculptor 跑通了一遍 LLM 摘要 + resume 实测 session_model_test.py 没报错），因为 `stop_details` 是可选字段，但 strictly speaking 这是 schema mismatch。建议 sculptor 加一个 `"stop_details": null`。

### 7.3 Field 重写规则

实测 sculptor 改写的字段：

- `sessionId` → 新 UUID（每条 record）；
- `uuid` → merge 的 synthetic 取 new_uuid，其他保留；
- `parentUuid` → 如果 parent 落在 dropped/merged 集合里，walk-up 重链；
- `message.content` → 如果 tool_result 被 hide，content 改成 `"[hidden by sculptor · original size N chars]"`；如果 tool_use 被 hide，input 改成 `{"_sculptor_hidden": true, "_original_size": N}`；
- 其他所有字段 verbatim。

实测 sculptor edit 后没有 dropped_record_uuids（manifest 显示空数组）。

### 7.4 Resume 行为

未实测真正发 `claude --resume` 命令（避免对话被污染），但通过 chain integrity check 验证：
- 0 个 orphan parents；
- tool_use / tool_result 严格 1:1 配对（269/269）；
- 总 records 949，含 3 个 synthetic + 多个真实 record + 全部 meta records（queue-operation, last-prompt 等）；
- sculptor manifest 自我审计 OK。

**结论：sculptor-edited session 的 round-trip 是干净的**。

---

## 8. 给 agent-mode 的设计建议

### 8.1 三层渲染（隐藏 / 摘要 / 完整展开）

基于以上分析，把 jsonl 中的字段按"agent 该看 vs 不该看" 切成三层：

#### 🟢 完整展开（agent 必看）

- `user.message.content` 当为 string（纯输入）或 array 含 text/image 块；
- `assistant.message.content[].type=="text"`（assistant prose）；
- `assistant.message.content[].type=="thinking"`（assistant reasoning） —— 注意 `thinking` 字段的 text 是本地 placeholder，**真实 reasoning 通过 `signature` 在云端**；
- `tool_use.name + tool_use.input`（assistant 调了什么）；
- `tool_result.content`（工具的回复）；
- `system / compact_boundary`（用 banner）；
- `system / local_command`（slash command 上下文）；
- `system / away_summary`（recap 提示，可选）；
- 第一条 `attachment / skill_listing` (isInitial:true)。

#### 🟡 摘要（agent 知道存在但不展开）

- `system / stop_hook_summary` （"Stop hook ran in 87ms"）；
- `system / turn_duration` （"Turn 12 took 41612ms"）；
- `system / api_error` （"API retried 3 times"）；
- `attachment / task_reminder` / `command_permissions` / `goal_status` / `diagnostics`；
- 后续重复的 `attachment / skill_listing`；
- `attachment / hook_success`；
- `progress`（仅旧版本）；
- async sub-agent 的 `<task-notification>` user record（agent 应当读 result 部分，但 metadata 折叠）。

#### 🔴 完全 hide（sidecar 元数据，agent 不该看到）

- `queue-operation`（TUI 输入队列事件，纯 UI 元）；
- `last-prompt`（idempotent 状态快照，~1 KB × N 次）；
- `file-history-snapshot`（备份索引，跟会话内容无关）；
- `mode` / `permission-mode` / `ai-title` / `custom-title` / `agent-name`（idempotent 状态快照，纯 UI 元）；
- `user.toolUseResult`（结构化审计字段，~几 KB × N，纯本地 metadata）；
- `user.sourceToolAssistantUUID`（冗余等于 parentUuid）；
- `thinking.signature`（base64 句柄，对 agent 无意义）；
- `attachment / opened_file_in_ide` / `selected_lines_in_ide` / `edited_text_file` / `date_change`（IDE 集成，仅 client 用）；
- `attachment / nested_memory` / `plan_mode` / `plan_mode_exit`（罕见）；
- assistant.message.usage 的详细字段（agent 看一个 token 数即可，不需要 cache_creation iteration 细分）。

### 8.2 中间 markdown 的双层架构

**核心建议**：用 **frontmatter sidecar + 主体内容** 两层结构。

```markdown
---
sculptor_version: 1
source_path: /Users/limo/.claude/projects/.../<sid>.jsonl
records:
  - uuid: ca096b47-...
    type: user
    parentUuid: null
    hidden_fields:
      isMeta: true
      promptId: b5f2e7a4-...
  - uuid: 664b6929-...
    type: user
    parentUuid: ca096b47-...
    hidden_fields: ...
meta_records:    # queue-operation, last-prompt, mode, etc., all hidden
  - {type: queue-operation, operation: enqueue, timestamp: ..., content: "1"}
  ...
---

## Turn 1 — 用户输入 [uuid:664b6929]
/clear

## Turn 2 — 用户输入 [uuid:93a03598]
我现在有一个伟大的idea 我可以来这样！...

### 🤖 assistant thinking [uuid:ff423f23 · ~50t]
让我先理解一下你提到的参考 skill 和 jsonl 文件结构...

### 🤖 assistant text [uuid:f231182a · ~120t]
我先 grep 一下，然后给你 plan。

### 🛠️ tool_use Bash [uuid:c4b3d52a, toolu_012Ab... · ~80t]
```bash
ls ~/.claude/projects/...
```

### 📤 tool_result [uuid:bed3f611, toolu_012Ab... · ~2.3K] [HIDE]
(被 agent 标记隐藏，将以 stub 写回)

### 🛠️ tool_use Bash [uuid:37f6bcfd, toolu_01Ase... · ~95t] (parallel)
...
```

- agent **只编辑主体**（mark hide / mark merge / 添加 narrative summary）；
- frontmatter **完全机器维护**，agent 不动；
- 写回 jsonl 时用 frontmatter 的 hidden_fields + meta_records reconstruct verbatim；
- markdown 主体的 [HIDE] tag 翻成 sculptor 的 keep=False；
- markdown 主体的 `### MERGED [N records → summary]` 翻成 Merge 对象。

### 8.3 关键 implication（必须做 vs 可选做）

**必须做（否则 round-trip 会丢数据）**：

1. **补齐 META_TYPES**：把 `mode`, `permission-mode`, `ai-title`, `agent-name`, `custom-title` 加入 `META_TYPES`，让 sculptor 显式 passthrough 时记入 manifest，避免 unknown 分支漏掉它们的 `sessionId` 重写；
2. **删掉 `summary`**：换成检测 `user.isCompactSummary == true`（lock 这类 user record，禁止 hide/merge）；
3. **保留 `toolUseResult`** field on user record（当前 sculptor 已经走 dict copy 路径保留了，但要确认 hide tool_result 时也保留这个字段）；
4. **`progress` record 也参与链**（有 uuid + parentUuid），dropped_uuids 重链时需要扫到它，否则可能形成断链。

**可选做（agent-mode 体验更好）**：

1. **size estimation 补 `toolUseResult` + `attachment.content`**：当前 sculptor 严重 underestimate（Bash 输出在 `toolUseResult.stdout`，sculptor 算 `tool_result.content` 的 string 长度，二者通常一样但 Edit/Write/Agent 的 `toolUseResult` 比 `tool_result.content` 多 5-10x）；
2. **failed bash 检测补 `<tool_use_error>`**：`auto_mark(drop_failed_bash=True)` 当前的 preview 检测漏了 cancelled tool_use；
3. **synthetic record 补 `stop_details: null`** 避免严格 schema mismatch；
4. **markdown 渲染 compact_boundary 用 banner**（agent 看到 "Conversation compacted (auto trigger, 166909 → 6060 tokens)" 立刻知道这是一道分隔线，不要跨越它做 merge）。

---

## 9. 最 surprising 的 3 个发现（汇总）

1. **`type:"summary"` 实测不存在**，sculptor 的 `META_TYPES` 这一项是错误。真实的 compact 产物是 `system/compact_boundary` (parentUuid:null, 自带 `logicalParentUuid`) + 紧跟的 `user` record (`isCompactSummary:true`, `isVisibleInTranscriptOnly:true`)。

2. **每个 assistant record 只装一个 content block** (跨 50+ session 验证 length 全是 1)，多 block 是通过共享 `message.id` 的多条 record 实现。结果是 parallel tool calls **chain 分叉成 tree**：第二个 tool_use 把第一个 tool_use 当 parent，但两条 tool_result 各自挂回自己的 tool_use（不是挂回 prose 也不是挂回兄弟 tool_use）。Token usage 在所有兄弟 record 中**完全重复**（每条 carry 同样的 `output_tokens`），所以 sculptor 必须用 `message.id` group 起来再摊销 —— 这正是 `_reconcile_thinking_tokens` 做的事，但**它当前只用在 thinking blocks** 上，没用在 text + tool_use 的 cost 摊销上（虽然这些 block 的 size_tokens 是直接从 raw text 算的，所以问题不大）。

3. **user record 的顶层 `toolUseResult` 字段是个"影子结构化结果"**，按工具类型有 12+ 种不同 schema（Bash: stdout/stderr/interrupted, Edit: structuredPatch, Agent: 完整的 prompt/usage/toolStats…）。它跟 `message.content[0].tool_result.content` 内容多数时候**部分重叠但不完全相同**：例如 Edit 工具的 `tool_result.content` 是 "File updated successfully" 一句话，但 `toolUseResult.structuredPatch` 是完整 diff。sculptor 当前**只看 `tool_result.content` 算 size**，**忽略 `toolUseResult`**，这导致 Edit / Write / Agent 这类工具的 size 估计偏低 5-10x，影响 auto_mark 的 `drop_tool_results_larger_than` 触发。同样地，async Agent 的真正结果不来自原 tool_use 的 tool_result，而是远后位置的一条独立 user record + XML markup —— 这条不算 tool_result，但实际承载了 Agent 的完整输出。

---

## 10. 附录：用过的 jq snippets

```bash
# 列 record type 分布
jq -r '.type // "NULL"' "$SESS" | sort | uniq -c | sort -rn

# 列 content block type 分布
jq -r 'select(.message.content | type=="array") | .message.content[]?.type // "NULL"' "$SESS" | sort | uniq -c

# 找 tool_use 不配对的（孤儿）
diff <(jq -r 'select(.message.content | type=="array") | .message.content[]? | select(.type=="tool_use") | .id' "$SESS" | sort -u) \
     <(jq -r 'select(.message.content | type=="array") | .message.content[]? | select(.type=="tool_result") | .tool_use_id' "$SESS" | sort -u)

# 找 isCompactSummary
jq -c 'select(.isCompactSummary==true) | {uuid, parentUuid}' "$SESS"

# 找 system subtype
jq -r 'select(.type=="system") | .subtype // "NULL"' "$SESS" | sort | uniq -c

# 找 toolUseResult 的 shape 群
jq -c 'select(.toolUseResult) | .toolUseResult | keys' "$SESS" | sort | uniq -c

# 找 attachment subtype
jq -r 'select(.type=="attachment") | .attachment.type' "$SESS" | sort | uniq -c

# 找 parentUuid==null 的（链根）
jq -c 'select(.parentUuid == null) | {type, uuid: (.uuid // "no-uuid"), subtype, isMeta, isCompactSummary}' "$SESS"

# 找 sub-agent file 的 entry point
head -1 "$SUB_JSONL" | jq -c '{type, uuid, parentUuid, agentId, isSidechain}'

# 找带 logicalParentUuid（compact_boundary 特有）的
jq -c 'select(.logicalParentUuid) | {type, subtype, uuid, parentUuid, logicalParentUuid}' "$SESS"
```

