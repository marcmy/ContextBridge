"""`python -m contextbridge`: the desktop app, or a command for scripts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):      # run as a plain script path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "contextbridge"

import contextbridge  # noqa: E402,F401

from contextbridge.handoff import HandoffError, HandoffOptions  # noqa: E402
from contextbridge.models import Provider, Session  # noqa: E402
from contextbridge.paths import Roots  # noqa: E402
from contextbridge.service import ContextBridge, setup_logging  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="contextbridge", description=contextbridge.TAGLINE)
    ap.add_argument("--codex-home", help="use this directory instead of ~/.codex")
    ap.add_argument("--claude-home", help="use this directory instead of ~/.claude")
    ap.add_argument("--app-home", help="use this directory instead of ~/.contextbridge")
    sub = ap.add_subparsers(dest="cmd")

    gui = sub.add_parser("gui", help="open the desktop app (the default)")
    gui.add_argument("--smoke-test", action="store_true",
                     help="load sessions, then exit; 0 means the window worked")
    gui.add_argument("--screenshot", metavar="PNG", help="with --smoke-test, save the window")

    sessions = sub.add_parser("sessions", help="list conversations with their sync state")
    sessions.add_argument("--project", help="only those whose project path contains this text")
    sessions.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="one conversation's state and lineage")
    show.add_argument("session", help="session ID or prefix")

    for name, text in (("plan", "preview a handoff: writes nothing, makes no billable call"),
                       ("send", "carry out a handoff")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("session", help="session ID or prefix")
        cmd.add_argument("--to", choices=["codex", "claude"], help="default: the other agent")
        cmd.add_argument("--strategy", help="a transfer type listed by `plan`")
        cmd.add_argument("--budget", type=int, default=400_000,
                         help="context budget in estimated tokens (default 400000); 0 for none")
        cmd.add_argument("--summary", choices=["off", "index", "claude"], default="index")
        cmd.add_argument("--summary-tokens", type=int, default=8000)
        cmd.add_argument("--summary-model", default="sonnet")
        cmd.add_argument("--mode", choices=["faithful", "archive"], default="faithful")
        if name == "send":
            cmd.add_argument("--yes", action="store_true", help="required to write anything")
            cmd.add_argument("--authorize-ai-summary", action="store_true",
                             help="allow Claude summary calls, which use your Claude account")

    sub.add_parser("transfers", help="transfer history")
    sub.add_parser("duplicates", help="duplicate findings")
    return ap


def _roots(args) -> Roots:
    base = Roots.default()
    return Roots(Path(args.codex_home) if args.codex_home else base.codex_home,
                 Path(args.claude_home) if args.claude_home else base.claude_home,
                 Path(args.app_home) if args.app_home else base.app_home)


def _find(bridge: ContextBridge, prefix: str) -> Session:
    hits = bridge.find_sessions(prefix)
    if not hits:
        sys.exit(f"No conversation matches {prefix!r}.")
    if len(hits) > 1:
        names = ", ".join(f"{s.provider.label} {s.short_id}" for s in hits[:6])
        sys.exit(f"{prefix!r} matches {len(hits)} conversations: {names}. Use a longer ID.")
    return hits[0]


def _options(args) -> HandoffOptions:
    return HandoffOptions(mode=args.mode, context_budget=args.budget or None,
                          summary_mode=args.summary, summary_tokens=args.summary_tokens,
                          summary_model=args.summary_model,
                          summary_authorized=getattr(args, "authorize_ai_summary", False))


def _print_plan(plan) -> None:
    print(f"State: {plan.assessment.state.label}. {plan.assessment.detail}")
    for option in plan.strategies:
        mark = ">" if option.key == plan.strategy else " "
        state = "" if option.enabled else " (unavailable)"
        print(f" {mark} {option.key}: {option.label}{state}")
    if plan.blocked:
        print(f"Blocked: {plan.blocked}")
    if plan.estimated_after is not None:
        print(f"Estimated tokens: {plan.estimated_before or plan.estimated_after:,} -> {plan.estimated_after:,}")
    if plan.budget:
        b = plan.budget
        print(f"Records: {b.get('retained', 0)} kept, {b.get('omitted', 0)} omitted, {b.get('trimmed', 0)} shortened")
    print(f"Human turns: {plan.new_human}; assistant messages: {plan.new_assistant}")
    for note in plan.warnings + plan.workspace_notes:
        print(f"- {note}")


def run_cli(args) -> int:
    bridge = ContextBridge(_roots(args))
    snapshot = bridge.refresh()
    if args.cmd == "sessions":
        rows = [s for s in snapshot.sessions
                if not args.project or args.project.casefold() in (s.project or s.cwd).casefold()]
        if args.json:
            print(json.dumps([{"provider": s.provider.value, "id": s.session_id, "title": s.title,
                               "project": s.project, "state": s.state.value, "updated_ms": s.updated_ms}
                              for s in rows], indent=2, ensure_ascii=False))
            return 0
        for s in rows:
            print(f"{s.provider.label:<7}{s.short_id:<15}{s.state.label:<24}{s.title[:60]}")
    elif args.cmd == "show":
        s = _find(bridge, args.session)
        print(f"{s.title}\n{s.provider.label} {s.session_id}\nProject: {s.project}\n"
              f"State: {s.state.label}. {s.state_detail}")
        for entry in bridge.lineage_of(s):
            link = entry["link"]
            print(f"  {link.src_provider.label} {link.src_id[:13]} -> {link.dst_provider.label} "
                  f"{link.dst_id[:13]}  {link.relation} {link.transferred_at[:16]}")
    elif args.cmd in ("plan", "send"):
        s = _find(bridge, args.session)
        target = Provider(args.to) if args.to else s.provider.other
        try:
            plan = bridge.plan(s, target, _options(args), args.strategy)
            _print_plan(plan)
            if args.cmd == "plan":
                return 0
            if not args.yes:
                print("\nNothing written. Add --yes to carry this out.")
                return 0
            report = bridge.execute(plan, progress=lambda m: print(f"... {m}"))
        except HandoffError as err:
            sys.exit(str(err))
        print(f"\n{report.status}: {report.dest_id or '(no session created)'}\nReport: {report.report_path}")
    elif args.cmd == "transfers":
        for t in snapshot.transfers:
            print(f"{t['started_at'][:16]}  {t['status']:<12}{t['strategy']:<15}{t['src_id'][:13]} -> {t['dst_id'] or ''}")
    elif args.cmd == "duplicates":
        for d in snapshot.duplicates:
            print(f"{d.kind:<18}{d.session[0]:<7}{d.session[1][:13]:<15}"
                  f"{'can archive' if d.cleanup_allowed else 'keep':<13}{d.reason}")
    return 0


def run_gui(args) -> int:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        sys.exit("The desktop app needs PySide6. Run: pip install PySide6-Essentials")

    from contextbridge.ui.main_window import MainWindow, app_icon
    from contextbridge.ui.theme import apply_theme

    roots = _roots(args)
    setup_logging(roots)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(contextbridge.APP_NAME)
    app.setOrganizationName(contextbridge.APP_NAME)
    app.setWindowIcon(app_icon())
    apply_theme(app)
    try:
        bridge = ContextBridge(roots)
    except Exception as err:
        QMessageBox.critical(None, contextbridge.APP_NAME,
                             f"ContextBridge could not open its database in {roots.app_home}:\n\n{err}")
        return 1
    window = MainWindow(bridge)
    window.show()
    if getattr(args, "smoke_test", False):
        code = {"value": 1}

        def check():
            if window._refreshing:
                QTimer.singleShot(100, check)
                return
            if args.screenshot:
                window.grab().save(args.screenshot)
            code["value"] = 0 if bridge.snapshot is not None else 1
            app.quit()
        QTimer.singleShot(200, check)
        app.exec()
        return code["value"]
    return app.exec()


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd in (None, "gui"):
        return run_gui(args)
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
