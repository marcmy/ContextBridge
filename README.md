# codeximporterforclaude

Import OpenAI Codex conversation history into Claude Code, so old Codex threads
show up in `/resume` and can be continued.

Codex can import Claude sessions. Claude Code's own `/import` only moves *config*
(AGENTS.md, subagents, prompts, MCP servers) — it has no path for transcripts.
This closes that gap.

## Fork fixes: Codex rollout revisions and internal sessions

Codex `thread/revert` creates a new immutable rollout file named
`rollout-<timestamp>-<thread-id>_<rollout-id>.jsonl`. These are *revisions of
one logical thread*, not independent conversations. By default, `list` and
`import --all` now select the newest filesystem revision per thread ID.
For revisions that expose a `history_base` pointer, the importer reconstructs
the retained ancestor JSONL prefixes in order, respecting byte offsets. If an
ancestor is unavailable, the import is **skipped**, rather than silently
importing an incomplete conversation.

The filesystem selection is a fallback and does not yet consult the current
rollout path in Codex's SQLite index. This version only reads uncompressed
`.jsonl` rollouts; compressed `.jsonl.zst` ancestors require a future
reader. If a thread was imported before a revert, rerunning the importer will
**not** append a different rollout revision to the existing Claude transcript:
use `import --force` to import the newest revision as a separate session.

Codex subagents and internal reviewer/Guardian sessions are excluded by
default. To inspect or import those explicitly:

```sh
python3 src/codex_import.py list --include-internal
python3 src/codex_import.py import --all --include-internal --dry-run
```

Internal app-control and approval-review prompts that are logged with the
`user` role are treated as background metadata rather than human messages.
If a rollout contains no actual conversation, it is skipped.

## Use

```sh
python3 src/codex_import.py list                    # what's importable
python3 src/codex_import.py import --all --dry-run  # see what would happen
python3 src/codex_import.py import --all            # do it
python3 src/codex_import.py import --session 01a0aa55   # just one (id prefix)
```

That covers the **CLI**: `claude` in the session's cwd, then `/resume`.

For the **desktop app**, add `--desktop`:

```sh
python3 src/codex_import.py import --all --desktop
```

The CLI finds transcripts by scanning `~/.claude/projects/`. The desktop app doesn't —
it lists sessions from its own registry of per-session cards, joined to the transcript
by a `cliSessionId` field. Without a card there is no sidebar row, however valid the
transcript is. `--desktop` writes that card. **Quit and reopen Claude Desktop
afterwards** — it caches the session list in memory.

Sessions are titled after the first thing you actually typed. Codex's
`<recommended_plugins>` catalogue, `AGENTS.md` block and `<environment_context>` are
stripped from the title and, in the transcript, are marked `isMeta: true` so they
render as background rather than as chat bubbles attributed to you.

Codex Desktop's per-thread scratch dirs (`~/Documents/Codex/<date>/<slug>`) collapse
into one `Codex` sidebar folder instead of one folder each. Threads whose cwd is a
real project land in that project's folder beside your native Claude sessions.

Re-running is safe: imports are recorded in the ledger, and a thread already in Claude
is never imported twice. If it has moved on in Codex since, only the new turns are
appended (see below). To undo everything it created:

```sh
python3 src/codex_import.py uninstall --dry-run   # see what would go
python3 src/codex_import.py uninstall
```

Nothing is read from Codex except `~/.codex/sessions/` and the import ledger. Nothing
existing is overwritten: a new thread gets a fresh session UUID, and an existing
transcript is only ever appended to, after a backup copy is saved.

## Keep a large import within Claude's context window

Full Codex transcripts may be much larger than Claude's context window.
The optional `--context-budget` creates a **lossy working-session view**:
it preserves recent complete human turns, shortens large historical tool
traffic while keeping tool-use/result pairs valid, and inserts an explicit
background note containing selected earlier user-request excerpts. When
even the last turn is too large, it retains its user request and most recent
assistant text instead of invalid partial tool calls.

The original `~/.codex/sessions/` files are never changed. This mode is
**not an AI-generated summary** and may omit earlier project decisions. Claude
can re-read the local repository and the original source rollouts as needed.

To safely remake ONE existing imported session (rather than uninstalling all
imports), close Claude Code/Desktop first and preview:

```powershell
python3 src/codex_import.py import --session 01a1ab29 --mode faithful --keep-developer --context-budget 100000 --replace --dry-run
```

Then remove `--dry-run` to write. `--replace` automatically backs up the
previous imported Claude transcript in `~/.claude/codex-import-backups/`,
preserves its Claude session UUID, and updates its import ledger. It **refuses**
to replace a transcript that Claude has continued with native assistant turns,
or any transcript lacking the importer provenance marker. It also requires a
single session ID; `--all --replace` is forbidden.

The token estimate is deliberately conservative (UTF-8 bytes / 2 plus
overhead), but **not Claude's real token counter**. A budget of 100,000 estimated
message tokens leaves room in a standard 200K context for tool definitions,
instructions, and continuing the conversation. Check Claude's context meter
after re-opening; lower the budget if necessary.

Incremental synchronization is disabled for budgeted imports: rerun the same
`--replace --context-budget` command to refresh the bounded view from Codex.
If you already continued the imported conversation in Claude, preserve that
Claude work and create a *separate* bounded import using `--force` instead.

### Preserve the decisions in omitted older turns

A context budget does not need to mean discarding every earlier decision. A
bounded import can now include an evidence-backed digest of the omitted history,
before the newer full conversation turns.

The default `--summary-mode index` is **entirely offline**: it copies labeled
excerpts of earlier user requests, assistant outcomes and diagnostic tool output
without invoking an LLM. It is explicitly an evidence index, **not a claim that
the project was semantically summarized**.

For a richer summary, explicitly opt in to `--summary-mode claude`:

```powershell
# Preview: zero Claude calls, zero file writes.
python3 src/codex_import.py import --session 01a1ab29 --mode faithful --context-budget 400000 --summary-mode claude --summary-tokens 8000 --summary-model sonnet --force --dry-run

# Real run: Claude Code CLI synthesizes evidence-cited historical summaries.
python3 src/codex_import.py import --session 01a1ab29 --mode faithful --context-budget 400000 --summary-mode claude --summary-tokens 8000 --summary-model sonnet --force
```

This uses `claude -p` without tools or persisted sessions, and sends **omitted
Codex transcript excerpts** to your configured Claude account. It therefore
consumes Claude Code usage and may incur charges depending on your authentication
and billing. Calls have a configurable `--summary-max-usd` **per-call** limit
(default $2; not a guaranteed total cost cap). It also refuses jobs
requiring more than `--summary-max-calls 16` model calls by default; raising
that limit is an explicit opt-in. Responses must contain valid [Tnnnn] citations
from their source segment; otherwise the importer stops rather than inserting
unsupported history. No Anthropic access is used in default `index` mode.

To avoid very large model prompts, the history is split chronologically into
bounded chunks, each summarized separately. If necessary, the resulting summaries
are merged hierarchically. The model is instructed to distinguish requests,
implementation, attempted approaches, failures and next steps, with [T0001] style
source-turn references. Its output is still **AI-generated and fallible**;
review before trusting technical decisions. Claude failures fail the import
rather than silently losing source context. Chunk and consolidation summaries
are cached locally under `~/.claude/codex-import-summaries/` by exact content,
model and prompt version.

A dry-run of `--summary-mode claude` uses the offline digest as a **preview
placeholder**; it makes no billable calls and the final size may differ.
`--summary-tokens` reserves an *estimated* portion of the working context for
the summary, while retaining the most recent complete conversation turns.
The original rollouts remain untouched.

You can also supply a manually reviewed summary, avoiding additional model calls:

```powershell
python3 src/codex_import.py import --session 01a1ab29 --mode faithful --context-budget 400000 --summary-file .\history-notes.txt --force --dry-run
```

If you've already continued the Claude import, use `--force` to create a new
session rather than `--replace`; replacement deliberately refuses to overwrite
native Claude work.

## When a thread moves on in Codex

A thread can be in both apps and keep going in Codex:

- **An import of ours you continued in Codex.** Codex appends to the rollout; the
  ledger records how far into it the import got.
- **A Claude session Codex imported, continued in Codex.** Codex replays the Claude
  history as `external-import-turn-*` turns, then carries on. Everything after those
  is work Claude has never seen.

Either way, `import --all` appends the Codex turns Claude doesn't have yet to the Claude
transcript that already holds the thread, chained onto its latest message, so it
stays one conversation you can resume. A background banner marks where the Codex part
starts, and the desktop card's activity time moves up to the last Codex turn. If the
Claude side moved on too, the Codex turns follow its latest message. Nothing already
in the transcript is touched; a copy is saved to `~/.claude/codex-import-backups/`
first.

Quit Claude desktop before syncing. A session it has open keeps its cached copy, and a
message sent there would branch off before the Codex turns.

`--no-sync` skips these threads instead; `--force` imports an import of ours again as
a fresh session; `--include-round-trips` imports a Codex copy of a Claude session as a
new session instead of appending to the original. `uninstall` never deletes a native
Claude session Codex turns were appended to — only the record of the sync.

## Modes

**`--mode archive`** (default) — tool calls and outputs are flattened into readable
text blocks. The transcript reads like a log. No foreign tool names ever reach the
API. Use this if you mainly want the history searchable and readable.

**`--mode faithful`** — real `tool_use`/`tool_result` pairs, so the session renders
in Claude Code the way a native one does, with collapsible tool calls. Codex `exec`
/ `shell` become `Bash`; MCP tools keep their `mcp__*` names; anything else is
prefixed `codex__` so it is visibly foreign rather than impersonating a real tool.
Tested and working on resume, but it does replay tool names that don't exist in your
current session.

Both modes were verified by actually resuming an imported session and asking it to
recall the Codex-side history.

## Flags

| | |
|---|---|
| `--all` / `--session <prefix>` | what to import |
| `--mode archive\|faithful` | see above |
| `--desktop` | also register with the Claude desktop app so it shows in the sidebar |
| `--force` | re-import sessions already in the ledger as a fresh copy |
| `--no-sync` | don't append newer Codex turns to threads already in Claude |
| `--title-prefix '[codex] '` | mark imported titles (default: unmarked) |
| `--no-group-scratch` | keep Codex scratch dirs as separate sidebar folders |
| `--no-banner` | skip the visible provenance banner (metadata still recorded) |
| `--dry-run` | print the plan, write nothing |
| `--keep-developer` | keep Codex developer/system injections as meta messages |
| `--include-round-trips` | import threads Codex imported *from* Claude as new sessions (default: append their Codex-side turns to the original) |
| `--codex-dir` / `--claude-dir` | override source and destination roots |

## Tracking what came from Codex

Every import is recorded in three places, so provenance survives losing any one of them:

- **In the transcript.** The first record is a background (`isMeta`) banner naming the
  Codex thread id, its start time, the source rollout path and the import mode — visible
  when you open the session, without looking like something you typed. The trailing
  record carries the machine-readable version: thread id, rollout path, a sha256 of the
  source, import time, mode, tool version. Both travel with the file.
- **In `~/.claude/codex-imports.json`.** The ledger, deliberately stored beside the
  Claude data rather than in this checkout — deleting or moving the tool must not orphan
  the imports. Keyed by Codex thread id, with how many bytes of the rollout have been
  brought across, so a re-run only picks up what is new.
- **On the desktop card**, as a `codexImport` object. Best effort: the app owns that file
  and may drop unknown keys when it rewrites the card, which is why it is not the only copy.

`--no-banner` suppresses the visible banner; the metadata is still written.

```sh
python3 src/codex_import.py status
```

lists each import, both ids, and checks that all three sides still line up — flagging a
missing transcript, a missing card, a deleted source rollout, or a rollout that has
changed since it was imported (Codex appends to a thread when you continue it, so a
changed rollout means there is newer history; `import --all` brings it across).

## When a row says "session not found on disk"

The desktop app writes its in-memory session list back to disk, so a card removed while
the app is running can reappear without its transcript. The result is a sidebar row that
fails to open.

```sh
python3 src/codex_import.py repair
```

clears rows whose transcript is gone and reports any import that lost its transcript
(re-import those with `import --all --force`). Quit the app first, or the same thing can
happen again.

## Undoing an import

`uninstall` removes the transcripts and desktop cards this tool created, and nothing
else, using `imported.json`. If that ledger is lost, `--scan` finds them anyway —
every transcript is stamped with an import marker. `--orphan-cards` additionally
clears dead sidebar rows whose transcript no longer exists.

## Tests

```sh
python3 tests/test_convert.py
```

28 fixture tests pinning the parts that were expensive to work out: which Codex
records carry content, how machine preambles are split from what the user typed, the
tool_use/tool_result invariant, the two path rules, and appending newer Codex turns to
a thread already in Claude.

## What doesn't survive

Reasoning, mostly. Codex keeps it in an encrypted blob with a usually-empty
plaintext summary — about 85% of reasoning records have nothing recoverable, and the
rest become plain text. It can't become a Claude `thinking` block: those carry an
Anthropic-issued signature that's verified on replay and can't be minted.

Also dropped: token accounting, rate-limit snapshots, sandbox/permission profiles,
web-search result cards, compaction history.

Everything else — user turns, assistant turns, tool calls, tool output, timestamps,
cwd — comes across.

## Layout

```
src/codex_import.py    the converter
src/desktop_register.py  writes the desktop app's session cards
schema/skel.py         JSONL structure dumper used to do the diff
schema/*.skel.txt      the dumps both formats were read from
```
