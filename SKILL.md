---
name: sculptor
description: 'Active context management for Claude Code sessions. Translate the session jsonl into editable markdown, let an agent read/edit (delete = hide, rewrite = merge, leave alone = keep), then translate back to a new jsonl that `claude --resume` picks up. Use when the user says "context is filling up / compact the conversation / trim history / let the agent organize the context" or when you proactively detect context pressure. **Recommended**: spawn a dedicated subagent (strong model, e.g. Opus) to do the editing — that way the main conversation only pays for the eventual `cd ... && claude --resume <sid>` one-liner, not the scanning / pattern-matching / dry-running work. Companion to the ICLR 2026 paper "Sculptor: Empowering LLMs with Cognitive Agency via Active Context Management" (arXiv:2508.04664).'
---

# sculptor

Two scripts is all you need.

```bash
~/.claude/skills/sculptor/scripts/s1.py <session.jsonl>   # → edit.md
# agent reads & edits edit.md directly
~/.claude/skills/sculptor/scripts/s2.py <edit.md>         # → new jsonl
claude --resume <new-sid>                                  # continue from there
```

The original jsonl is never modified. The new jsonl lands in the same directory as the original and `claude --resume` picks it up automatically.

## Where are these `<session.jsonl>` files?

Claude Code stores each project's conversation history as `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`. The directory name is the project's working directory with `/` replaced by `-`:

| working directory (`cwd`) | session file path |
|---|---|
| `~/Documents/myproject` | `~/.claude/projects/-Users-<you>-Documents-myproject/<sid>.jsonl` |
| `~` (home) | `~/.claude/projects/-Users-<you>/<sid>.jsonl` |

`<sid>` is the session UUID. To find the most recent session for your current project (note: Claude Code encodes both `/` **and** `.` as `-` in the directory name, so `/.claude/` becomes `--claude-`):

```bash
ls -lat ~/.claude/projects/$(pwd | sed 's|[/.]|-|g')/*.jsonl | head -3
```

Each line of the jsonl is one **record** (user input, assistant text, thinking block, tool_use, tool_result, etc.). `s1.py` parses these into the editable markdown shown below.

## What s2.py prints when it's done

s2 ends by printing a **ready-to-paste resume command**, so you don't have to assemble the path yourself. Two variants depending on where the new jsonl landed:

If s2 wrote directly into `~/.claude/projects/<encoded-cwd>/` (the default — same directory as the source jsonl):

```
✓ wrote ~/.claude/projects/-Users-<you>-Documents-myproject/<new-sid>.jsonl
  ...
  tokens kept: 51,230  (hidden -88,470)

恢复用 (直接复制即可):
  cd ~/Documents/myproject && claude --dangerously-skip-permissions -r <new-sid>
```

If s2 wrote to some other directory (you passed `-o <other-dir>`):

```
恢复用 (先 cp 到 project 目录再 resume):
  cp /tmp/out/<new-sid>.jsonl ~/.claude/projects/-Users-<you>-Documents-myproject/ && \
  cd ~/Documents/myproject && claude --dangerously-skip-permissions -r <new-sid>
```

Paste, run, you're back in the same conversation — minus what you chose to cut.

## Typical scenario (this is how sculptor is meant to be used)

The user has been chatting with Claude in some project for a long time and context is getting full. They tell Claude: "context is filling up, please trim this session" / "compact the conversation" / "整理一下" / etc.

Claude's correct response is **not** to run s1 / redit / s2 itself — that would burn the same context the user is trying to relieve. Instead Claude should:

1. **Spawn a dedicated subagent** via the `Agent` tool (strong model, e.g. Opus).
2. The subagent finds the current session jsonl by itself: `cwd` is known (the main conversation is running in some project), the path is `~/.claude/projects/$(echo "$CWD" | tr '/' '-')/<latest-mtime-jsonl>`.
3. The subagent runs the whole `s1.py → explore → redit.py → s2.py` pipeline on its own context budget.
4. The subagent returns to the main conversation the **single `cd ... && claude --dangerously-skip-permissions -r <new-sid>` one-liner** that s2 printed at the end.
5. The user pastes that one-liner into a terminal (likely in another window) and resumes with the trimmed history.

The main conversation pays only the cost of (1) and (4) — typically a few hundred tokens total — instead of the tens of K of tokens it would cost to scan and edit the markdown directly.

## Recommended invocation

```
Agent({
  subagent_type: "general-purpose",
  description: "整理当前 session 的 jsonl 上下文",
  prompt: """
  使用 sculptor 流程整理当前正在进行的 conversation:

  1. 找到当前 session 的 jsonl. cwd 应当跟主 conversation 一致:
       ENCODED=$(pwd | sed 's|[/.]|-|g')   # 注意 . 也要 encode 成 - (/.claude → --claude)
       LATEST=$(ls -t ~/.claude/projects/$ENCODED/*.jsonl 2>/dev/null | head -1)
     如果 ls 不到, 用 mtime 在 ~/.claude/projects/$ENCODED/ 里找最新的 jsonl.
     注意: 主 conversation 当前正在往这个 jsonl 写; 我们读取的是当前的快照.

  2. 跑 s1 把 jsonl 转 markdown, 同时备份:
       ~/.claude/skills/sculptor/scripts/s1.py "$LATEST"
       cp "${LATEST%.jsonl}.edit.md" "${LATEST%.jsonl}.edit.before.md"

  3. 探索全局 (不要 Read 整个 md 文件, 它可能几 MB):
       grep -nE "^### turn " "${LATEST%.jsonl}.edit.md" | sort -t '·' -k 4 -n -r | head -30
     等等, 按 SKILL.md "Getting your bearings" 那段提示的方式.

  4. 用 redit.py 按 SKILL.md 列出的 pattern (大 result 转述、boilerplate、
     重复 Read、失败实验整段、image attachment、env-dump 等) 批量裁.
     目标 30~50% jsonl token 降幅, 不追极限.
     **不要单独删 thinking 块** (跟它的 turn 共生死, 见 SKILL.md 反模式).

  5. 跑 s2 产出新 jsonl:
       ~/.claude/skills/sculptor/scripts/s2.py "${LATEST%.jsonl}.edit.md"

  6. 把 s2 最后打印的 "cd ... && claude --dangerously-skip-permissions -r <new-sid>"
     那一行**原文返回**给我. 这就是我要交给用户的全部内容.
  """,
})
```

The subagent does the whole job; the main conversation only relays the resulting one-liner to the user.

**Choose a strong model**: this work needs judgment (which pattern applies, what's safe to delete, what to rewrite vs hide). Weak models tend to over-delete or miss obvious wins.

## What the intermediate markdown looks like

Each record becomes a section, headed like `### turn N · kind · b00NN · Ntokens · meta`:

```markdown
### turn 1 · user · b0001 · 925t
<user text>

### turn 1 · think · b0002 · 4340t
<thinking content, may be empty + signature note>

### turn 1 · asst · b0003 · 1022t
<assistant text>

### turn 3 · call+result · b0042+b0043 · 66+58t · Bash
**call** (b0042):
description: ...
$ <command>

**result** (b0043):
<output>
```

`b00NN` is the anchor; the postprocessor uses it to locate the original jsonl record. A `tool_use` and its corresponding `tool_result` are packed into the same section — delete them together to avoid pairing orphans.

## Getting your bearings

Before editing, it helps to have a sense of what's in `edit.md` overall — which turns are huge, which patterns repeat, which sections are obvious deletes. The most direct approach is to `Read` the whole file, but a freshly-cut session can range from tens of KB up to several MB; for the bigger ones it might not fit in your context or budget. A few cheap ways to get a global view without reading every byte:

- `wc -l edit.md` / `grep -c "^### turn" edit.md` — quick size and section-count sanity
- `grep -nE "^### turn " edit.md` — list every section heading; you see the whole structure (turn, kind, anchor, tokens, meta) line by line
- pipe the heading list through `sort -t '·' -k 4 -n -r | head -30` (or eyeball it) to surface the biggest sections first
- targeted `grep -A 20 "b0123"` to inspect one section without pulling its neighbours into context
- Read with `offset` / `limit` for a specific region rather than the whole file

How much context you want to spend on exploration vs. editing is your call. The point is to know enough about the global shape to make confident decisions; how you get there is up to you.

## Editing efficiently: `redit.py` for bulk deletes

The built-in `Edit` tool requires `old_string` to be the **exact full text** of the region being replaced. For a 50KB section that costs ~12,500 output tokens to type out, plus the same amount of input tokens forever in the session (the tool_use record stays in context). For a large `edit.md` (often several MB) this is prohibitive.

Use `redit.py` instead — it takes only the **short boundary markers** and computes the region locally:

```bash
~/.claude/skills/sculptor/scripts/redit.py edit.md \
  --start "### turn 5 · think · b0123" \
  --end   "### turn 5 · asst · b0124"
```

Defaults: `--start` is included in the deletion (it's the head of the region you want to delete), `--end` is excluded (it's the sentinel marking the next section that should be preserved). Pass `--dry-run` to preview first. Pass `--new "<text>"` to replace instead of delete (useful for the "rewrite body as merged summary" pattern). The tool fails fast if `--start` isn't unique in the file, and warns if `--end` matches multiple times after `--start`.

Cost: deleting 100 sections via `redit.py` ≈ a few hundred tokens of CLI args, vs. tens of MB via `Edit`.

## Three intents

| What the agent does | Postprocess result |
|---|---|
| Delete the whole section | hide (the record is stubbed/dropped) |
| Edit the body content | merge replacement (replaced with a synthetic record) |
| Leave alone | keep as-is |

Only one constraint: **don't change the `b00NN` id in the heading**.

## Which sections to delete / rewrite

Patterns that have proven effective on real sessions (sorted by frequency):

- **Large tool_result already paraphrased**: the agent has already summarized the key takeaway in the next think/asst block — the verbatim content can be deleted.
- **Repeated Read / Edit on the same file**: keep only the last Read (reflects current disk state) and delete the stale intermediate snapshots.
- **Boilerplate receipts**: fixed templates like `Task #N created successfully` / `The file X has been updated successfully` / `Updated task #N status` carry zero information.
- **Failure → retry chain**: rewrite "fail → diagnose → retry → success" as a single line: "agent realized X failed because of Y, used Z instead and it worked."
- **Failed-experiment turns**: turns that already got recapped by later turns and are no longer referenced — delete the whole turn, or keep a one-line summary that future agents might still need.
- **SendMessage echo**: in multi-agent sessions, the SendMessage tool_result is usually a complete echo of `call.content` — delete the result, keep the call.
- **Oversized single result**: the agent fetched an entire Feishu doc / git log / dataset dump but only used the head/tail — delete the whole result; the original is on disk or a remote source.
- **Large image attachments (originals on disk)**: PNG screenshots and generated images get base64-embedded into the session (megabytes each). If the corresponding `<file_path>` still exists on disk (`output/*.png`, `screenshots/*.png`, etc.), delete the image content and keep only the file_path reference. The agent can `Read` it back on resume. **Important**: this also unblocks the Anthropic API's "image dimension > 2000px" and "many-image request" limits — long-running sessions accumulate dozens of large PNGs and eventually can't resume; pruning redundant images is the only way out.
- **Obviously irrelevant chunks in long-context tests (use with caution)** ⚠️: for needle-in-haystack benchmarks where the user input is a large context plus a specific question, most of the context is irrelevant. You *can* delete the obviously irrelevant parts (disclaimers, repeated section headers, boilerplate). But be **conservative**: the needle may hide in "looks irrelevant" places. **Delete one small chunk at a time and verify the resumed answer is still correct** — never batch-hide large sections, or you might delete the needle along with the hay.

Rule of thumb: **after deleting this, can the server-side resume still finish the same task, and was the deleted info actually contributing to that outcome? If yes to both → delete it.**

## ⚠️ Special handling: thinking blocks

The default for `thinking` blocks is **leave them alone**, but it's worth understanding *why*, because there's a narrow case where deleting them is fine.

Fact (from the Anthropic official docs):

> "The signature field is **not just a verification hash**—it contains the encrypted full thinking content that the server can decode."
>
> "The server **decrypts the signature** to reconstruct the original thinking for prompt construction."

So an empty `thinking` field in the jsonl is just the client's `display: "omitted"` mode hiding the prose — **the `signature` itself carries the full encrypted thinking content** that the server decodes back for reasoning. Unlike a `tool_result` (where you've read the text and can judge whether it's redundant), with a `thinking` block you genuinely don't know what's in it. Deleting it is a **blind delete**.

**Default**: leave thinking alone. The agent should not touch it.

**When deleting IS fine**: if the entire surrounding turn is already being deleted — user input, assistant text, all the tool calls and results in that turn — then the thinking block goes with them. There's no remaining context for the server to condition that thinking on anyway, so dropping it is consistent. This applies to the "整段失败实验" / "failed-experiment turns" pattern above: if you delete a whole turn, the thinking in it goes too.

**When deleting is NOT fine**: selectively keeping the user / assistant text of a turn but stripping out just the thinking. That breaks the "entire sequence of consecutive thinking blocks must match the outputs generated by the model during the original request" guarantee, and may degrade reasoning quality on resume, or, if the deleted thinking sits inside a tool-use turn, get the request rejected outright.

Practical rule: thinking goes with its turn. Delete the turn or keep it whole; don't pick a turn apart.

## Boundaries & guarantees

- **Original jsonl is never modified**: s2 writes a new file to the same directory with a `<new-sid>.edit-manifest.json` audit trail.
- **tool_use ↔ tool_result pairing**: s2 automatically stubs / re-balances to keep the API valid — the agent doesn't have to worry about it.
- **parentUuid chain**: after dropping records, the chain auto-stitches to the nearest surviving ancestor.
- **Merged synthetic records**: sections the agent rewrote land as records tagged with `model: "sculptor-synthetic"`, body prefixed `agent rewrote ...`, for post-hoc audit.
- **compact_boundary warning** ⚠️: sessions that have been through `/compact` contain a user record with `isCompactSummary: true` — this is the compact boundary, do not delete it.

## Dependencies

- Python 3.10+
- `tiktoken` (for cl100k_base token counts)
