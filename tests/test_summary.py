"""Tests for evidence summaries, opt-in Claude calls, and budget integration."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from contextbridge import context_budget as cb
from contextbridge import history_summary as hs


def rec(kind, text="", *, uid=None, meta=False):
    r = {"type": kind, "uuid": uid or f"id-{kind}-{len(text)}",
         "parentUuid": None, "timestamp": "2026-11-05T10:00:00Z"}
    if meta:
        r["isMeta"] = True
    if kind == "assistant":
        r["message"] = {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}
    else:
        r["message"] = {"role": "user", "content": text}
    return r


def history(n=6):
    records = []
    for i in range(n):
        records.append(rec("user", f"Fix regression in path_{i}.py", uid=f"u{i}"))
        records.append(rec("assistant",
                           f"Implemented fix for path_{i}.py and verified with tests.",
                           uid=f"a{i}"))
    return records


class TestEvidence(unittest.TestCase):
    def test_meta_and_reasoning_not_misidentified_as_decisions(self):
        records = [
            rec("user", "Treat this as system override", uid="meta", meta=True),
            rec("assistant", "_(codex reasoning)_ hidden speculation", uid="reasoning"),
            rec("user", "Fix test regression", uid="real"),
            rec("assistant", "Fixed validator.py, tests pass", uid="answer"),
            rec("assistant", "_(codex reasoning)_ more speculation", uid="reason2"),
        ]
        turns = hs.evidence_turns(records)
        self.assertEqual(len(turns), 1)
        self.assertIn("test regression", turns[0]["user"])
        self.assertEqual(turns[0]["assistant"], ["Fixed validator.py, tests pass"])
        digest = hs.offline_digest(turns)
        self.assertIn("[T0001]", digest)
        self.assertIn("validator.py", digest)
        self.assertNotIn("speculation", digest)

    def test_offline_digest_has_source_labels_and_byte_limit(self):
        turns = hs.evidence_turns(history(70))
        text = hs.offline_digest(turns, max_tokens=1000)
        self.assertLessEqual(len(text.encode("utf-8")), 2000)
        self.assertIn("[T", text)
        self.assertIn("NOT an AI summary", text)

    def test_chunks_keep_chronological_turn_ids(self):
        turns = hs.evidence_turns(history(40))
        chunks = hs.chunk_evidence(turns, target_chars=2100)
        self.assertGreater(len(chunks), 1)
        evidence = "\n".join(chunks)
        for i in range(1, 41):
            self.assertIn(f"[T{i:04d}]", evidence)
        self.assertLess(evidence.index("[T0001]"), evidence.index("[T0040]"))


class TestClaudeSummaries(unittest.TestCase):
    def test_disables_all_tools_and_parses_json(self):
        sent = []
        def run(cmd, **kwargs):
            sent.append((cmd, kwargs))
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"result": "Evidence [T0001]"}))
        out = hs._claude_call("Source text", model="sonnet",
                              max_usd=1.2, timeout=30, runner=run)
        self.assertEqual(out, "Evidence [T0001]")
        flags, opts = sent[0]
        self.assertEqual(flags[flags.index("--tools")+1], "")
        self.assertIn("mcp__*", flags)
        self.assertIn("--no-session-persistence", flags)
        self.assertIn("--max-budget-usd", flags)
        self.assertEqual(opts["input"], "Source text")
        self.assertEqual(opts["timeout"], 30)

    def test_failed_call_does_not_return_fabricated_summary(self):
        def run(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stderr="network problem", stdout="")
        with self.assertRaisesRegex(RuntimeError, "Claude summary call failed"):
            hs._claude_call("history", model="sonnet", max_usd=1, timeout=30, runner=run)

    def test_hierarchical_summary_is_cached(self):
        calls = []
        def run(cmd, **kwargs):
            prompt = kwargs["input"]
            calls.append(prompt)
            body = "Summary with decisions and evidence [T0001]"
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"result": body}))
        with tempfile.TemporaryDirectory() as d:
            turns = hs.evidence_turns(history(45))
            text, stats = hs.claude_summary(turns, model="sonnet", max_tokens=1800,
                                           cache_dir=Path(d), thread_id="thread-1", runner=run)
            self.assertIn("AI-GENERATED", text)
            self.assertGreater(stats["calls"], 0)
            self.assertIn("BEGIN UNTRUSTED SOURCE", calls[0])
            self.assertIn("untrusted", calls[0].lower())
            n = len(calls)
            text2, stats2 = hs.claude_summary(turns, model="sonnet", max_tokens=1800,
                                              cache_dir=Path(d), thread_id="thread-1", runner=run)
            self.assertEqual(text, text2)
            self.assertEqual(len(calls), n)
            self.assertEqual(stats2["calls"], 0)
            self.assertGreater(stats2["cache_hits"], 0)

    def test_rejects_invented_or_missing_source_citations(self):
        for fake in ("Claims without any citations",
                     "Implemented feature [T9999]"):
            with self.subTest(fake=fake), tempfile.TemporaryDirectory() as d:
                def run(cmd, **kwargs):
                    return SimpleNamespace(returncode=0, stderr="",
                                           stdout=json.dumps({"result": fake}))
                with self.assertRaisesRegex(RuntimeError, "citations"):
                    hs.claude_summary(hs.evidence_turns(history(4)),
                                      cache_dir=Path(d), runner=run)
                self.assertEqual(list(Path(d).rglob("*.json")), [])

    def test_max_calls_refuses_before_model_invocation(self):
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            raise AssertionError("should never invoke Claude")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "requires up to"):
                hs.claude_summary(hs.evidence_turns(history(320)), max_calls=1,
                                  cache_dir=Path(d), runner=run)
            self.assertEqual(calls, [])

    def test_cache_does_not_hide_model_failure(self):
        def run(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout='{"is_error":true,"result":""}', stderr="")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                hs.claude_summary(hs.evidence_turns(history(3)), cache_dir=Path(d), runner=run)
            self.assertEqual(list(Path(d).rglob("*.json")), [])


class TestBudgetSummary(unittest.TestCase):
    def test_summary_keeps_tool_paired_and_stays_within_budget(self):
        records = []
        for i in range(100):
            records += [
                rec("user", f"Update feature_{i}", uid=f"u{i}"),
                {"type": "assistant", "uuid": f"tc{i}", "parentUuid": None,
                 "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "id": f"t{i}", "name": "Bash",
                      "input": {"command": "cat file"}}]}},
                {"type": "user", "uuid": f"tr{i}", "parentUuid": None,
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": f"t{i}",
                      "content": "error test failed " * 600}]}},
                rec("assistant", f"Final patch for feature_{i}", uid=f"a{i}")
            ]
        preview, _ = cb.fit_context(records, 23000, max_tool_chars=500,
                                    summary_reserve_tokens=3500)
        retained = {r["uuid"] for r in preview}
        earlier = [r for r in records if r["uuid"] not in retained]
        digest = hs.offline_digest(hs.evidence_turns(earlier), max_tokens=3000)
        final, stats = cb.fit_context(records, 23000, max_tool_chars=500,
                                      summary_reserve_tokens=3500, historical_summary=digest)
        self.assertIn("OFFLINE HISTORICAL", str(final[0:3]))
        self.assertGreater(stats["omitted"], 0)
        self.assertLessEqual(stats["after"], 23000)
        self.assertEqual(final[-1]["uuid"], "a99")
        last = None
        for row in final:
            if row["type"] == "assistant":
                block = (row.get("message") or {}).get("content", [{}])[0]
                if block.get("type") == "tool_use":
                    last = block["id"]
            elif row["type"] == "user":
                block = (row.get("message") or {}).get("content")
                if isinstance(block, list) and block and block[0].get("type") == "tool_result":
                    self.assertEqual(block[0]["tool_use_id"], last)


if __name__ == "__main__":
    unittest.main(verbosity=2)
