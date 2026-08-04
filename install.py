#!/usr/bin/env python3
"""
Knewrall installer — wires a Knewrall knowledge base into an agent workspace so
that any harness (Claude Code, Codex, OpenCode, Cline, Antigravity, Gemini, ...)
loads its instructions automatically.

Knewrall is designed to live as a ``knewrall/`` subfolder of your workspace. Copy
or clone it there, then run:

    python knewrall/install.py

What it does (all idempotent — safe to re-run):

* Writes/updates thin pointer files at the workspace root, each in the filename a
  given harness auto-loads, so the always-on behavior is recognized everywhere:
  ``AGENTS.md`` (Codex/OpenCode/Cursor/Antigravity), ``CLAUDE.md`` (Claude Code),
  ``GEMINI.md`` (Gemini CLI), ``.clinerules`` (Cline). Existing content is
  preserved — only the block between the Knewrall markers is managed.
* Installs Claude Code hardening (``.claude/`` SessionStart hook + skill) so the
  behavior is enforced, not just suggested, where supported.
* Builds the searchable index.

Run ``python knewrall/install.py --uninstall`` to remove the managed blocks and
the Claude Code hardening (your own content is left intact).
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

KNEWRALL_DIR = Path(__file__).resolve().parent
BEGIN = "<!-- KNEWRALL:BEGIN (managed by knewrall/install.py — edits here are overwritten) -->"
END = "<!-- KNEWRALL:END -->"
HOOK_COMMAND = "python .claude/hooks/knewrall_session_start.py"
FOLD_TURN_HOOK_COMMAND = "python .claude/hooks/knewrall_fold_turn.py"
PRECOMPACT_HOOK_COMMAND = "python .claude/hooks/knewrall_precompact.py"
FOLD_ENFORCE_HOOK_COMMAND = "python .claude/hooks/knewrall_fold_enforce.py"


def _block_body(instructions_rel: str, launcher_rel: str, *, claude_import: bool) -> str:
    """The shared Knewrall instruction block written into each root pointer file."""
    lines = [
        "## Knewrall knowledge base",
        "",
        f"This workspace has a Knewrall personal knowledge graph at `{KNEWRALL_DIR.name}/`. "
        "As an agent here, two behaviors are **always in effect**, with no user request needed:",
        "",
        "1. **Ground first.** Before acting — and whenever you meet a name, person, project, "
        "place, decision, or preference you might already know — recall the knowledge base and "
        "use what you find (one call returns full content, not just pointers):",
        "",
        f"   `python {launcher_rel} recall \"<term>\"`",
        "",
        "2. **Save durable facts.** When the user reveals something worth remembering, capture it "
        "as a Neuron via the Knewrall CLI (check for duplicates first).",
        "",
        f"Full operating instructions: read [`{instructions_rel}`]({instructions_rel}) and follow it.",
    ]
    if claude_import:
        lines += ["", f"@{instructions_rel}"]
    return "\n".join(lines)


def _wrap(body: str) -> str:
    return f"{BEGIN}\n{body}\n{END}\n"


def upsert_block(path: Path, body: str) -> str:
    """Create the file or replace its Knewrall block, preserving other content."""
    block = _wrap(body)
    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return "created"

    content = path.read_text(encoding="utf-8")
    if BEGIN in content and END in content:
        pre = content[: content.index(BEGIN)]
        post = content[content.index(END) + len(END):].lstrip("\n")
        new = pre + block + (("\n" + post) if post else "")
        if new != content:
            path.write_text(new, encoding="utf-8")
            return "updated"
        return "unchanged"

    sep = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
    path.write_text(content + sep + block, encoding="utf-8")
    return "appended"


def remove_block(path: Path) -> str:
    """Strip the Knewrall block; delete the file if nothing else remains."""
    if not path.exists():
        return "absent"
    content = path.read_text(encoding="utf-8")
    if BEGIN not in content or END not in content:
        return "no-block"
    pre = content[: content.index(BEGIN)]
    post = content[content.index(END) + len(END):].lstrip("\n")
    remainder = (pre.rstrip("\n") + ("\n" + post if post else "")).strip()
    if remainder:
        path.write_text(pre.rstrip("\n") + ("\n\n" + post if post else "\n"), encoding="utf-8")
        return "block removed"
    path.unlink()
    return "file removed"


def install_claude_hardening(workspace: Path, *, with_fold_enforcement: bool = False) -> list[str]:
    """Copy the SessionStart + UserPromptSubmit + PreCompact hooks and skill,
    and register all three hooks in settings.json. The PreToolUse fold
    enforcer (Phase 5) is opt-in — only installed/registered/enabled with
    `--with-fold-enforcement`, since it's the only Engram Layer piece that
    can actively obstruct the agent (a `deny` decision on a Bash call)."""
    notes = []
    src = KNEWRALL_DIR / "templates" / "claude"
    dst = workspace / ".claude"

    hook_dst = dst / "hooks" / "knewrall_session_start.py"
    hook_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "hooks" / "knewrall_session_start.py", hook_dst)
    notes.append(".claude/hooks/knewrall_session_start.py")

    fold_turn_dst = dst / "hooks" / "knewrall_fold_turn.py"
    shutil.copy2(src / "hooks" / "knewrall_fold_turn.py", fold_turn_dst)
    notes.append(".claude/hooks/knewrall_fold_turn.py")

    precompact_dst = dst / "hooks" / "knewrall_precompact.py"
    shutil.copy2(src / "hooks" / "knewrall_precompact.py", precompact_dst)
    notes.append(".claude/hooks/knewrall_precompact.py")

    if with_fold_enforcement:
        fold_enforce_dst = dst / "hooks" / "knewrall_fold_enforce.py"
        shutil.copy2(src / "hooks" / "knewrall_fold_enforce.py", fold_enforce_dst)
        notes.append(".claude/hooks/knewrall_fold_enforce.py")

    skill_dst = dst / "skills" / "knewrall"
    skill_dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "skills" / "knewrall" / "SKILL.md", skill_dst / "SKILL.md")
    notes.append(".claude/skills/knewrall/SKILL.md")

    settings = dst / "settings.json"
    data = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  ! {settings} is not valid JSON — leaving it untouched; register the hooks manually.")
            return notes

    session_start_hooks = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    already = any(
        h.get("command") == HOOK_COMMAND
        for group in session_start_hooks for h in group.get("hooks", [])
    )
    changed = False
    if not already:
        session_start_hooks.append({"hooks": [{"type": "command", "command": HOOK_COMMAND}]})
        notes.append(".claude/settings.json (SessionStart hook registered)")
        changed = True

    # APPEND to the existing UserPromptSubmit group — never replace it. Other
    # subsystems (e.g. teamwork's teamwork_route.py) may already own a hook
    # there, and this one is meant to run AFTER it (fold-scan re-surfacing
    # folded context is lower priority than routing the turn itself).
    prompt_hooks = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    already_prompt = any(
        h.get("command") == FOLD_TURN_HOOK_COMMAND
        for group in prompt_hooks for h in group.get("hooks", [])
    )
    if not already_prompt:
        prompt_hooks.append({"hooks": [{"type": "command", "command": FOLD_TURN_HOOK_COMMAND}]})
        notes.append(".claude/settings.json (UserPromptSubmit fold-scan hook registered, appended after any existing hooks)")
        changed = True

    precompact_hooks = data.setdefault("hooks", {}).setdefault("PreCompact", [])
    already_precompact = any(
        h.get("command") == PRECOMPACT_HOOK_COMMAND
        for group in precompact_hooks for h in group.get("hooks", [])
    )
    if not already_precompact:
        precompact_hooks.append({"hooks": [{"type": "command", "command": PRECOMPACT_HOOK_COMMAND}]})
        notes.append(".claude/settings.json (PreCompact transcript-ingestion hook registered)")
        changed = True

    if with_fold_enforcement:
        pretooluse_hooks = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
        already_enforce = any(
            h.get("command") == FOLD_ENFORCE_HOOK_COMMAND
            for group in pretooluse_hooks for h in group.get("hooks", [])
        )
        if not already_enforce:
            pretooluse_hooks.append({
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": FOLD_ENFORCE_HOOK_COMMAND}],
            })
            notes.append(".claude/settings.json (PreToolUse fold enforcer registered, Bash only)")
            changed = True

        engrams_config_note = _enable_fold_enforcement_in_config()
        if engrams_config_note:
            notes.append(engrams_config_note)

    if changed:
        settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return notes


def _enable_fold_enforcement_in_config() -> str | None:
    """Flip `engrams.enforce` to true in knewrall/.knewrall/config.json —
    installing the hook without this would be a silent no-op, since the hook
    itself checks this flag and allows everything when it's false."""
    config_path = KNEWRALL_DIR / ".knewrall" / "config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("engrams", {}).get("enforce") is True:
        return None
    data.setdefault("engrams", {})["enforce"] = True
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return ".knewrall/config.json (engrams.enforce set to true)"


def uninstall_claude_hardening(workspace: Path) -> None:
    dst = workspace / ".claude"
    for p in [dst / "hooks" / "knewrall_session_start.py", dst / "hooks" / "knewrall_fold_turn.py",
              dst / "hooks" / "knewrall_precompact.py", dst / "hooks" / "knewrall_fold_enforce.py"]:
        if p.exists():
            p.unlink()
            print(f"  removed {p.relative_to(workspace)}")
    skill = dst / "skills" / "knewrall"
    if skill.exists():
        shutil.rmtree(skill)
        print(f"  removed {skill.relative_to(workspace)}")
    settings = dst / "settings.json"
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            for event, command in (
                ("SessionStart", HOOK_COMMAND),
                ("UserPromptSubmit", FOLD_TURN_HOOK_COMMAND),
                ("PreCompact", PRECOMPACT_HOOK_COMMAND),
                ("PreToolUse", FOLD_ENFORCE_HOOK_COMMAND),
            ):
                groups = data.get("hooks", {}).get(event, [])
                for g in groups:
                    g["hooks"] = [h for h in g.get("hooks", []) if h.get("command") != command]
                remaining = [g for g in groups if g.get("hooks")]
                if event in data.get("hooks", {}):
                    if remaining:
                        data["hooks"][event] = remaining
                    else:
                        data["hooks"].pop(event)
            if not data.get("hooks"):
                data.pop("hooks", None)
            settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print("  deregistered SessionStart + UserPromptSubmit (fold-scan) + PreCompact + PreToolUse (fold enforcer) hooks from .claude/settings.json")
        except json.JSONDecodeError:
            pass


def build_index() -> None:
    os.environ["KNEWRALL_ROOT"] = str(KNEWRALL_DIR)
    sys.path.insert(0, str(KNEWRALL_DIR))
    from src.knewrall_indexer import rebuild_index_command
    rebuild_index_command(True)


def relpath(target: Path, start: Path) -> str:
    return Path(os.path.relpath(target, start)).as_posix()


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Knewrall into an agent workspace.")
    parser.add_argument("--workspace", type=Path, default=KNEWRALL_DIR.parent,
                        help="Workspace root to wire up (default: the parent of knewrall/).")
    parser.add_argument("--uninstall", action="store_true", help="Remove managed blocks and hardening.")
    parser.add_argument("--no-claude", action="store_true", help="Skip Claude Code hook/skill hardening.")
    parser.add_argument("--no-index", action="store_true", help="Skip building the search index.")
    parser.add_argument("--with-fold-enforcement", action="store_true",
                        help="Opt-in: install the PreToolUse Engram Layer enforcer (denies verbose bare "
                             "Bash commands, e.g. pytest, telling the agent to re-run via fold-run) and "
                             "set engrams.enforce=true. Off by default — the only Engram Layer piece "
                             "that can actively obstruct the agent.")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    pointer_files = {
        "AGENTS.md": False,
        "CLAUDE.md": True,   # Claude Code supports @import — pull the full doc in
        "GEMINI.md": False,
        ".clinerules": False,
    }
    instructions_rel = relpath(KNEWRALL_DIR / "INSTRUCTIONS.md", workspace)
    launcher_rel = relpath(KNEWRALL_DIR / "bin" / "knewrall.py", workspace)

    if args.uninstall:
        print(f"Uninstalling Knewrall pointers from {workspace}")
        for name in pointer_files:
            print(f"  {name}: {remove_block(workspace / name)}")
        uninstall_claude_hardening(workspace)
        print("Done. The knewrall/ folder and its data were left untouched.")
        return

    print(f"Installing Knewrall ({KNEWRALL_DIR.name}/) into workspace: {workspace}")
    for name, claude_import in pointer_files.items():
        body = _block_body(instructions_rel, launcher_rel, claude_import=claude_import)
        print(f"  {name}: {upsert_block(workspace / name, body)}")

    if not args.no_claude:
        print("Claude Code hardening:")
        for note in install_claude_hardening(workspace, with_fold_enforcement=args.with_fold_enforcement):
            print(f"  + {note}")

    if not args.no_index:
        print("Building search index...")
        build_index()

    print("\nDone. Agents in this workspace will now ground from and write to the "
          "Knewrall knowledge base automatically.")
    print(f"Verify with:  python {launcher_rel} stats")


if __name__ == "__main__":
    main()
