# sculptor

> A practical context-editing tool for [Claude Code](https://claude.com/claude-code).
> Translate a session jsonl into editable markdown → let an agent freely edit (delete = hide, rewrite = merge, leave alone = keep) → translate back to a new jsonl that `claude --resume` picks up.

Drop this repo under `~/.claude/skills/sculptor/`. Invoke as `/sculptor`, or run `s1` / `s2` directly.

```bash
~/.claude/skills/sculptor/scripts/s1.py <session.jsonl>   # → <session>.edit.md + .sidecar.json
# agent reads & edits the markdown
~/.claude/skills/sculptor/scripts/s2.py <session>.edit.md # → new jsonl in the same directory
claude --resume <new-sid>                              # continue from there
```

Original jsonl is never modified.

---

## Relationship to the Sculptor paper

This repo is a **skill-level implementation** and follow-up of our ICLR 2026 paper:

> **Sculptor: Empowering LLMs with Cognitive Agency via Active Context Management.**
> Mo Li, L.H. Xu, Qitai Tan, Long Ma, Ting Cao, Yunxin Liu. ICLR 2026.
> [arXiv:2508.04664](https://arxiv.org/abs/2508.04664)

Same core idea — give the agent explicit control over what stays in the context window, instead of relying on opaque auto-compaction. The paper proposes three families of cognitive tools (fragmentation / summary-hide-restore / precise search). This repo implements **summary-hide-restore** for Claude Code session jsonl files, in the most direct form possible: agent reads markdown, edits it, we re-encode.

```bibtex
@article{li2025sculptor,
  title   = {Sculptor: Empowering {LLMs} with Cognitive Agency via Active Context Management},
  author  = {Li, Mo and Xu, L.H. and Tan, Qitai and Ma, Long and Cao, Ting and Liu, Yunxin},
  journal = {arXiv preprint arXiv:2508.04664},
  year    = {2025},
  url     = {https://arxiv.org/abs/2508.04664}
}
```

---

## Install

```bash
git clone https://github.com/Mor-Li/sculptor.git ~/.claude/skills/sculptor
pip install tiktoken
```

---

## What's in the intermediate markdown

Each conversation block becomes a section with a stable `b00NN` anchor:

```markdown
### turn 1 · user · b0001 · 925t
<user text>

### turn 1 · think · b0002 · 4340t
<thinking content>

### turn 3 · call+result · b0042+b0043 · 66+58t · Bash
**call** (b0042):
$ <command>

**result** (b0043):
<output>
```

Agent's three intents:
- **delete the section** → hide the corresponding record(s)
- **rewrite the body** → replace with a merged synthetic record
- **leave alone** → keep as-is

Only constraint: don't touch the `b00NN` id in the heading.

---

## ⚠️ Important: don't drop thinking signatures

The `thinking` blocks may look empty in the jsonl (`thinking: ""`) but the `signature` field is the **encrypted full thinking content** that the server decodes for round-trip reasoning. Deleting thinking sections silently degrades reasoning quality on resume.

See `SKILL.md` "❌ 反模式" section for full details and the Anthropic docs quote.

---

## Risks

- The hidden tool_result stub string is visible to Claude on resume; model usually accepts it.
- `parentUuid` chain stitching after dropping records relies on Claude Code's tolerance; observed to work but not stress-tested.
- For sessions that have been `/compact`-ed, `compact_boundary` records define a hard boundary; don't delete the `isCompactSummary: true` user record across that boundary.

---

## See also

- [Sculptor paper (arXiv)](https://arxiv.org/abs/2508.04664)
- [Claude Code Skills docs](https://docs.claude.com/en/docs/claude-code/skills)
- [SKILL.md](./SKILL.md) — full workflow & pattern guide (中文)
- [docs/jsonl-anatomy.md](./docs/jsonl-anatomy.md) — deep dive into Claude Code session jsonl format

---

## License

MIT — see [LICENSE](./LICENSE).
