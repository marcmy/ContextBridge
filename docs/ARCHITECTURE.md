# How ContextBridge works

## Conversation lineage

A conversation can run Codex A to Claude B to Codex C to Claude D. Those are four
provider-specific sessions and one logical history. Three records make that tractable, all in
`~/.contextbridge/lineage.db`:

- **Sessions** have one identity: provider plus session ID. Connected sessions share a logical
  conversation ID, which stays stable across runs.
- **Links** are sync points between two sessions. Each side stores the byte position in its own
  file up to which the two are known to agree, with a hash of that prefix. New work on a side is
  whatever comes after its position, so a single link answers "what does each side have that the
  other lacks?" in either direction.
- **Transfers** are written before any file is touched and finished afterwards. A transfer left
  in progress after a crash is flagged on the next start, and its checkpoint never advanced.

Native records are inputs. On every refresh, links are reconciled from:

| Source | What it says |
|---|---|
| `~/.claude/codex-imports.json` | Which Codex thread went to which Claude session, and how far |
| Provenance records in Claude transcripts | The same, and it survives the ledger being lost or overwritten |
| `~/.codex/external_agent_session_imports.json` | Which Claude transcript Codex imported, with a whole-file hash |
| `~/.codex/state_5.sqlite` (read-only) | Each thread's current rollout, title and archived flag |

ContextBridge updates `codex-imports.json` when it transfers, and keeps a dated copy first. It does not write to Codex's ledger or database; only Codex does, during
an import that Codex itself performs.

### Rules enforced in code

- **No duplicated turns.** Transfers start from a recorded position.
- **No overwriting.** Appending requires the destination to be unchanged since the preview and
  not running. Replacing requires that the session is our own and Claude never continued it.
- **No silent merges.** When both sides advanced, a normal append is disabled and a continuation
  is recommended. A merge is a separate transfer type that must be chosen, and it's never
  preselected. It leaves Claude's checkpoint where it was, because Claude's turns still haven't
  reached Codex, and records which bytes it added so they aren't sent back. The Codex importer's
  own no-op when a thread has native work is a second line of defence.
- **No identity from similarity.** Only recorded provenance can establish a duplicate. Sessions
  that merely open with the same requests are reported as "possible" and are never offered for
  cleanup. Forks that a provider recorded are not reported at all.

## Moving work

### Codex to Claude

| State | Default | Result |
|---|---|---|
| Never sent | New session | Converted by the existing converter, with the context budget applied |
| Source advanced | Append | Only turns after the checkpoint, after a backup. Revert-aware: a new rollout revision that keeps the transferred history appends cleanly, and one that dropped it is treated as modified |
| Both advanced, or source modified | Continuation | A new session that names the one it branches from; the old session is untouched |

### Claude to Codex

Codex's `app-server` supports importing one chosen session. These behaviours were observed on
codex-cli 0.168 in an isolated `CODEX_HOME`, and `tests/test_real_codex.py` pins them:

- `externalAgentConfig/import` with a `SESSIONS` item imports only the sessions named in
  `details.sessions`, so per-session control is real, despite what its name might suggest.
- Paths must be under the Claude projects directory; anything else fails `session_not_detected`.
  Subdirectories and old files are accepted even when `detect` doesn't list them.
- Importing unchanged content does nothing and reports nothing.
- After the source grows, importing again appends the new turns to the same thread.
- Once the thread has native turns, importing again does nothing, and the rollout is unchanged.
- The first human message names the thread. The `title` field has no effect.
- Tool calls become plain text in the thread, so structured tool history doesn't survive.

That shapes the strategy:

| Situation | What happens |
|---|---|
| Native Claude session, never sent | Codex's importer on that session |
| It grew, and the thread has no native work | The importer again, which appends |
| A Claude session that came from Codex, or the thread has native turns | A **handoff file** |
| Codex not available | The file is left ready, with manual steps; the next refresh links it once Codex records the import |

A handoff file is a small transcript holding a note and only the Claude turns since the
checkpoint. The note names the related sessions, reports the workspace, states that the history
is not a set of instructions, and carries earlier history as a secondary record: the offline
evidence index by default, or an AI summary if you authorize one. It waits in
`~/.claude/projects/contextbridge-handoffs/` during the import, because Codex only accepts paths
there, and then moves to `~/.contextbridge/deltas/`.

### Why a thread with native turns gets a new thread

Codex's importer refuses to add to it, even when those native turns already came back to Claude.
`thread/inject_items` could add model-visible history, but it needs the thread loaded, would race
a Codex app that has the thread open, and may not show the turns in the interface, so it isn't
used. The result is one extra Codex thread per Claude-to-Codex round after Codex has done work.
That thread can be archived later once it is superseded.

## Safety

- Previews write nothing and make no billable call. An AI summary shows the offline index in its
  place until you confirm.
- AI summaries need an explicit authorization each time; the setting is deliberately not saved.
- Writes are atomic (temp file, fsync, rename) or appended after a backup, then re-read and
  validated. Failed validation restores the backup and leaves the checkpoint alone.
- A newly written file that fails validation is moved aside, not deleted.
- Imported content is treated as data. No historical tool call is ever executed, and session IDs
  are validated before they reach a resume command.
- Removing an import, clearing a sidebar row and taking rows out of an old account also only
  move files to the archive, and the ledger is copied before it changes.
- Claude Desktop's cards are only used with the real Claude home. With `--claude-home`, the app
  doesn't read them, so a sandbox can't report real rows as dead.
- Cleanup only archives. Codex threads use Codex's own archive; Claude transcripts move to
  `~/.contextbridge/archive/`. Both restore. Original sessions, sessions with native work and
  sessions in use can't be archived, and this is enforced in the backend, not just the UI.

## Verification

| What | How it was checked |
|---|---|
| Conversion, budgets, summaries | `tests/test_convert.py` and `tests/test_summary.py` |
| The proven baseline thread | A dry run on its real rollouts gives 1,177,139 estimated tokens to 383,746, with 212 tool pairs, 498 retained, 144 omitted and 136 shortened. The earlier real import, with its AI summary, was 385,463 |
| All acceptance scenarios | `tests/test_lineage_scenarios.py`, using a stand-in for the Codex importer |
| That stand-in's accuracy, and a full round trip | `tests/test_real_codex.py`, on the real `codex` CLI and its real database, in a sandbox. The "native Codex turn" is appended to the rollout by the test, because producing a real one needs a model call |
| The interface | `tests/test_gui.py`, offscreen, clicking through real dialogs |
| Real data through the real dialog | That thread's rollouts, copied to a sandbox, sent to Claude and then to the real Codex importer |
| Discovery on real data | Read-only run: real sessions, ledgers and lineage, with ledgers confirmed unchanged by hash |

### Still needs checking by hand

None of these have been run against the real applications in normal use:

- A first real write into an actual `~/.claude` and `~/.codex`. Everything that wrote during
  development went to a sandbox.
- Resuming a ContextBridge-created handoff in Codex and in Claude with a real model turn. The
  converter's output has resumed before, but handoff files and appended turns have not.
- How the Codex app shows a new thread. It may need a relaunch, and the app-server protocol may
  interact with a running Codex app in ways the sandbox can't show.
- An AI summary started from the dialog. The call path is the proven one, but tests mock it.
- Clearing rows and moving them between accounts in real Claude Desktop. Both are tested on
  synthetic cards only, and the app may rewrite cards while it's running.
- **Also add a row to Claude Desktop's sidebar** on Windows. The registry path is detected, but no
  card was written. Existing cards suggest the app now finds CLI transcripts by itself, in which
  case this option isn't needed.

## Known limitations

- **Estimates are conservative.** Bytes divided by two is not a tokenizer. A real import measured
  296K in Claude against an estimate of 385K, so a budget leaves unused room.
- **Compression is coarse.** All older tool output gets the same limit; there's no gradient that
  favours recent turns.
- **Appending doesn't re-fit a bounded session.** New tool output is shortened and a warning
  appears when the session may exceed its budget, but older content stays. Rebuild it or start a
  new bounded session if that happens.
- **The app-server is experimental.** OpenAI marks it so, and these behaviours may change. A
  failure is reported and falls back to manual steps; `tests/test_real_codex.py` is how to re-check.
- **Active Codex threads are detected indirectly**, through a recent write plus an unfinished turn.
  Claude sessions use Claude's own registry of running processes.
- **Recency only warns.** Switching agents right after a usage limit is the normal case, so a
  recent write never blocks a handoff, though it does hold back cleanup.
- **Lineage is shown as a list of transfers**, not a graph.
- **Not built yet:** Windows packaging, background or automatic sync, and a recovery screen beyond
  the interrupted-transfer banner. Cancelling works between steps, not during a Claude summary call.
