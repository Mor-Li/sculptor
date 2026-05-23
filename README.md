# sculptor

> A practical context-editing tool for [Claude Code](https://claude.com/claude-code).
> Pick which past tool results, thinking blocks, or assistant turns to keep — or LLM-summarize a span of records into one synthetic message — before `claude --resume`.

This repo is a Claude Code [skill](https://docs.claude.com/en/docs/claude-code/skills). Drop it under `~/.claude/skills/sculptor/` and the agent (or you, manually) can invoke it as `/sculptor`.

---

## Relationship to the Sculptor paper

This repo is a **skill-level implementation** and follow-up of our ICLR 2026 paper:

> **Sculptor: Empowering LLMs with Cognitive Agency via Active Context Management.**
> Mo Li, L.H. Xu, Qitai Tan, Long Ma, Ting Cao, Yunxin Liu. ICLR 2026.
> [arXiv:2508.04664](https://arxiv.org/abs/2508.04664)

The two share the same core idea — give the agent (or its user) explicit control over what stays in the context window, instead of relying on opaque auto-compaction. This repo is the practical, Claude-Code-shaped version of that idea:

- The paper proposes three families of cognitive tools — *fragmentation*, *summary/hide/restore*, and *precise search*. This skill is a focused implementation of the **summary/hide/restore** family, scoped to the Claude Code session jsonl on disk.
- The TUI lets a human (or Claude itself in `--auto` mode) inspect and edit the conversation *before* the auto-compact threshold hits. You see exactly what's being removed; no black box.
- The merge feature uses an external LLM (via a [LiteLLM](https://github.com/BerriAI/litellm)-compatible endpoint) to summarize a contiguous span of records into one synthetic assistant-text record, with parentUuid stitching and tool_use/tool_result re-balancing so `claude --resume` keeps working.

If you build on this for a paper, cite Sculptor:

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
pip install tiktoken openai   # tiktoken for real token counts; openai only if you use the merge feature
```

Then in any Claude Code session, ask Claude to invoke it (`/sculptor`, "帮我精简对话", "context 快满了"), or run the CLI directly:

```bash
~/.claude/skills/sculptor/scripts/ce               # picks current cwd's latest session
~/.claude/skills/sculptor/scripts/ce path/to.jsonl # explicit
```

---

## What it does

Each Claude Code session is a `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`, one record per line. `sculptor` parses it into a tree of checkable blocks:

| block | locked? | what "hide" means |
|---|---|---|
| user input | 🔒 | always kept |
| tool_use | ✓ | `input` replaced with `{"_sculptor_hidden": true, "_original_size": N}`; type/id/name preserved so the tool_use ↔ tool_result pairing stays API-valid |
| tool_result | ✓ | content replaced with a stub `[hidden by sculptor · original size N chars]` so the tool_use/tool_result pair stays API-valid |
| assistant text | ✓ | block dropped; record dropped if empty; parentUuid chain auto-stitched |
| assistant thinking | ✓ | same as text |

Three editing modes:

1. **Manual hide (TUI)**: `↑↓` move, `space` toggle, `a` toggle all tool_results, `A` toggle all thinking, `T` toggle all assistant text, `u` un-hide everything, `p` preview, `s` save.
2. **LLM merge (TUI)**: `v` enter visual mode, `↑↓` extend selection, `m` send the span to a model (default `gemini-3.1-pro-preview` via an OpenAI-compatible endpoint) and replace the span with one synthetic assistant-text record carrying the summary. Auto-balances tool_use/tool_result across the boundary, refuses to cross user inputs.
3. **Auto (CLI)**: `ce --auto --drop-tool-results-larger-than 5000 --drop-thinking` for a non-interactive heuristic pass.

The original jsonl is **never** modified. Output is a new jsonl in the same directory plus a sidecar `<sid>.edit-manifest.json` audit trail. `claude --resume` auto-detects the new session.

---

## Configuration

The merge feature shells out to `~/ask_llm.py` (a minimal OpenAI-compatible CLI you provide — anything that takes `--model` and a prompt on stdin and prints the response works) and reads:

- `LITELLM_BASE_URL` — your endpoint (default: a LiteLLM proxy, set this to your own)
- `ANTHROPIC_AUTH_TOKEN` — the API token (any name works, follows our internal convention)

Override the model with `--merge-model gpt` or any raw model name. If you don't use merge, you don't need this.

---

## Risks

- The hidden tool_result stub string is visible to Claude on resume — the model usually accepts it and doesn't re-do the work, but YMMV.
- `parentUuid` chain stitching after dropping records relies on Claude Code's tolerance; observed to work but not stress-tested.
- Thinking blocks' `signature` is a server signature; dropping the whole record drops the signature too. Resume currently works.
- Merge is not per-record undoable. Pick the range carefully, or just `q` without saving and re-open.

---

## See also

- [Sculptor paper (arXiv)](https://arxiv.org/abs/2508.04664)
- [Claude Code Skills docs](https://docs.claude.com/en/docs/claude-code/skills)
- [SKILL.md](./SKILL.md) — the full keybinding reference & internal docs (中文)

---

## License

MIT — see [LICENSE](./LICENSE).
