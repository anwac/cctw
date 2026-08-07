#!/usr/bin/env python3
"""
cctw.py — Claude Code Transcript Watch

Live monitor for Claude Code transcript files (JSONL) in
~/.claude/projects/<project-dir>/. Shows per-session token state for the
main thread and any subagent sidechains as they run.

Usage:
  cctw.py                  # auto-pick most recently active project
  cctw.py PATH             # watch a specific project dir or .jsonl file
  cctw.py --window 200000  # context window size for % display
  cctw.py --log            # event stream instead of dashboard
  cctw.py --inspect        # dump one parsed assistant entry and exit

The transcript format is internal to Claude Code and changes between
releases. This script parses defensively: unknown fields are ignored,
unparsable lines are skipped, and --inspect exists so you can check what
your version actually writes.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

POLL_INTERVAL = 2.0  # dashboard refresh + file poll cadence (seconds)
ACTIVE_SECS = 20  # session counts as "live" if it wrote within this window
STALL_SECS = 600  # an unrecovered error older than this means "probably stuck"
NAME_TTL = 5.0  # how often the session-name sources are rescanned (seconds)
SCRIPT_PATH = os.path.abspath(__file__)

CLEAR = "\033[2J\033[H"
HOME = "\033[H"
ALT_ON = "\033[?1049h"
ALT_OFF = "\033[?1049l"
CURSOR_OFF = "\033[?25l"
CURSOR_ON = "\033[?25h"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BLUE = "\033[34m"
BROWN = "\033[38;5;130m"
GRAY = "\033[90m"
RESET = "\033[0m"
DEFAULT_FG = "\033[39m"  # back to default color without dropping bold/dim

# "Claude Code Transcript Watch" with each word's first letter in yellow.
TITLE = " ".join(f"{YELLOW}{w[0]}{DEFAULT_FG}{w[1:]}"
                 for w in "Claude Code Transcript Watch".split())


def default_project_dir():
    base = Path.home() / ".claude" / "projects"
    if not base.is_dir():
        sys.exit(f"not found: {base}")
    return base


def resolve_target(raw: str) -> Path:
    """A .jsonl file or dir under ~/.claude/projects is watched as-is;
    any other directory is mapped to its Claude Code project dir."""
    p = Path(raw).expanduser()
    base = Path.home() / ".claude" / "projects"
    if p.is_dir() and base not in p.parents and p != base:
        for name in (re.sub(r"[^A-Za-z0-9-]", "-", str(p.resolve())),
                     re.sub(r"[^A-Za-z0-9_-]", "-", str(p.resolve()))):
            cand = base / name
            if cand.is_dir():
                return cand
    return p


def jsonl_files(target: Path):
    if target.is_file():
        return [target]
    return sorted(target.glob("**/*.jsonl"))


class FileCursor:
    """Read position within one .jsonl file."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0


def entry_key(path: Path, entry: dict) -> str:
    """Group entries into logical sessions.

    Prefer the entry's own agentId: current Claude Code versions write
    subagent transcripts under <session-id>/subagents/agent-<agentId>.jsonl
    with the *parent's* sessionId on every line, so keying on sessionId
    would merge each subagent into the main thread's row. Only subagent
    lines carry agentId, so it is an unambiguous per-invocation key.
    Otherwise fall back to the entry's sessionId; failing that, split a
    file into its main-thread and sidechain streams so a subagent sharing
    the file with the main thread is tracked separately.
    """
    aid = entry.get("agentId")
    if isinstance(aid, str) and aid:
        return f"agent:{aid}"
    sid = entry.get("sessionId")
    if isinstance(sid, str) and sid:
        return sid
    side = "side" if entry.get("isSidechain") else "main"
    return f"{path}:{side}"


def is_synthetic(entry: dict) -> bool:
    """True for the placeholder assistant entries Claude Code appends when a
    request fails or an agent is terminated early: message.model is the
    literal "<synthetic>" and there is no usage."""
    msg = entry.get("message") if isinstance(entry, dict) else None
    return isinstance(msg, dict) and msg.get("model") == "<synthetic>"


def synthetic_error_text(msg: dict) -> str:
    """Error text carried by a synthetic assistant message, e.g.
    "API Error: 529 Overloaded". Any unexpected shape yields a best effort
    string rather than raising."""
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            txt = block.get("text")
            if isinstance(txt, str) and txt:
                parts.append(txt)
        return " ".join(parts).strip() or "(no error text)"
    return str(content)[:120]


class SessionNames:
    """session id -> session name, as set by `claude -n <name>` or /rename.

    Neither source on disk is sufficient alone, so both are consulted:

    ~/.claude/sessions/<pid>.json is written by every live claude process and
    carries the current name, but is keyed by pid (which a transcript never
    mentions) and is removed when the process exits; the sessionId inside it
    provides the join. ~/.claude/statusline-cache/<session-id>.json is keyed
    the way we need, is refreshed once per turn and survives the exit, so it
    covers finished sessions. Live wins where both know a session.

    Both are Claude Code internals: a missing directory, a missing field or an
    unparsable file just means no name, never an error.
    """

    def __init__(self):
        self.live: dict[str, str] = {}    # from ~/.claude/sessions
        self.cached: dict[str, str] = {}  # from ~/.claude/statusline-cache
        self.last_scan = 0.0

    @staticmethod
    def _load(path: Path) -> dict:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return d if isinstance(d, dict) else {}

    def _scan_live(self):
        live = {}
        try:
            paths = sorted((Path.home() / ".claude" / "sessions").glob("*.json"))
        except OSError:
            paths = []
        for p in paths:
            d = self._load(p)
            sid, name = d.get("sessionId"), d.get("name")
            if isinstance(sid, str) and sid and isinstance(name, str) and name:
                live[sid] = name
        self.live = live

    def get(self, sid) -> str:
        """Name for a session id, or "" if nothing on disk knows one."""
        if not isinstance(sid, str) or not sid:
            return ""
        now = time.time()
        if now - self.last_scan >= NAME_TTL:
            self.last_scan = now
            self._scan_live()
            # Forget misses, so a session that has only just written its first
            # statusline payload picks up a name on a later pass.
            self.cached = {k: v for k, v in self.cached.items() if v}
        if sid in self.live:
            return self.live[sid]
        if sid not in self.cached:
            base = Path.home() / ".claude" / "statusline-cache"
            name = self._load(base / f"{sid}.json").get("session_name")
            self.cached[sid] = name if isinstance(name, str) else ""
        return self.cached[sid]


SESSION_NAMES = SessionNames()


class Session:
    """Token state for one logical session (main thread or one sidechain)."""

    def __init__(self, path: Path):
        self.path = path
        self.is_sidechain = False
        self.agent_label = None
        self.agent_id = None
        self.session_id = None       # id the name lookup joins on
        self.model = "?"
        self.context_tokens = 0      # from the most recent assistant usage
        self.output_total = 0        # cumulative output tokens
        self.turns = 0
        self.last_ts = 0.0           # wall clock of last parsed entry
        self.slug = ""
        self.cwd = None              # working directory of the last entry seen
        self.error = None            # text of the latest unrecovered synthetic error
        self.error_ts = 0.0          # wall clock of that error

    def label(self):
        if self.agent_label:
            return f"agent:{self.agent_label}"
        if self.agent_id:
            return f"agent:{self.agent_id[:8]}"
        return "sidechain" if self.is_sidechain else "main"

    def feed(self, entry: dict, mtime: float):
        self.last_ts = mtime
        if entry.get("isSidechain"):
            self.is_sidechain = True
        aid = entry.get("agentId")
        if self.agent_id is None and isinstance(aid, str) and aid:
            self.agent_id = aid
        # Subagent lines carry the parent's sessionId, so a sidechain row shows
        # the name of the session it is running under. That is the intent: the
        # name identifies the claude process, not the individual agent.
        sid = entry.get("sessionId")
        if self.session_id is None and isinstance(sid, str) and sid:
            self.session_id = sid
        # Directory the session is working in; last one seen wins so a session
        # that changes directory shows its latest.
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.cwd = cwd
        # Try a few likely keys for the agent/subagent name; harmless if absent.
        # attributionAgent is what current versions write, and it is often
        # missing from the first line of a subagent file, so keep probing
        # every entry until a name turns up.
        #
        # Only probe on entries that actually belong to a subagent. Main-thread
        # entries carry a "slug" field holding the session's own name, so
        # probing them would mislabel every main thread as agent:<slug>.
        is_agent_entry = (isinstance(aid, str) and bool(aid)) or bool(entry.get("isSidechain"))
        if is_agent_entry and not self.agent_label:
            for key in ("attributionAgent", "agentName", "subagentType",
                        "agent", "slug"):
                v = entry.get(key)
                if isinstance(v, str) and v:
                    self.agent_label = v
                    break
        if entry.get("type") != "assistant":
            return
        msg = entry.get("message") or {}
        # A failed request / early-terminated agent: not a real turn, carries no
        # usage and no real model. Record it as the session's error state and
        # leave model, turns and token totals alone.
        if is_synthetic(entry):
            self.error = synthetic_error_text(msg)
            self.error_ts = mtime
            return
        if isinstance(msg.get("model"), str):
            self.model = msg["model"]
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return
        inp = usage.get("input_tokens", 0) or 0
        cc = usage.get("cache_creation_input_tokens", 0) or 0
        cr = usage.get("cache_read_input_tokens", 0) or 0
        out = usage.get("output_tokens", 0) or 0
        self.context_tokens = inp + cc + cr
        self.output_total += out
        self.turns += 1
        # A real turn means the session recovered.
        self.error = None
        self.error_ts = 0.0


def read_new_lines(cur: FileCursor):
    """Yield parsed JSON entries appended to the file since last read."""
    try:
        size = cur.path.stat().st_size
    except FileNotFoundError:
        return
    if size < cur.offset:           # rotated/truncated: start over
        cur.offset = 0
    if size == cur.offset:
        return
    with open(cur.path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(cur.offset)
        chunk = f.read()
        # keep a partial trailing line for next round
        if not chunk.endswith("\n"):
            cut = chunk.rfind("\n")
            if cut == -1:
                return
            chunk = chunk[: cut + 1]
        cur.offset += len(chunk.encode("utf-8", errors="replace"))
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def fmt_k(n: int) -> str:
    return f"{n/1000:.1f}K" if n >= 1000 else str(n)


def fmt_dir(p: str | None) -> str:
    """Short project name as shown in Claude Code's status line: the basename."""
    if not p:
        return ""
    if p == str(Path.home()):
        return "~"
    return Path(p).name


def shorten_error(text, limit: int = 40) -> str:
    """One-line display form of an error: drop the "API Error: " prefix,
    collapse whitespace, truncate. "API Error: 529 Overloaded" -> "529
    Overloaded"."""
    txt = text if isinstance(text, str) else str(text)
    txt = " ".join(txt.split())
    prefix = "API Error: "
    if txt.startswith(prefix):
        txt = txt[len(prefix):]
    return txt[:limit]


def fmt_model(model) -> str:
    """Display form of a model id: strip the leading "claude-", which every
    value carries and so tells the reader nothing. "claude-fable-5" ->
    "fable-5". Nothing else is removed."""
    txt = model if isinstance(model, str) else str(model)
    prefix = "claude-"
    return txt[len(prefix):] if txt.startswith(prefix) else txt


def pct_color(pct: float) -> str:
    if pct >= 45:
        return RED
    if pct >= 30:
        return YELLOW
    return GREEN


def fmt_hours(secs: float) -> str:
    """Seconds as a compact hour count: 86400 -> '24', 1800 -> '0.5'."""
    txt = f"{secs/3600:.1f}"
    return txt[:-2] if txt.endswith(".0") else txt


def caffeinate_running() -> bool:
    """True while a macOS caffeinate process is alive. Anything that stops
    pgrep from running at all (missing binary, no fork) reads as "not
    running" rather than killing the dashboard."""
    try:
        return subprocess.run(["pgrep", "-x", "caffeinate"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def render(sessions, window, target, max_age):
    now = time.time()
    rows = sorted(sessions.values(), key=lambda s: s.last_ts, reverse=True)
    age_note = f"max-age={fmt_hours(max_age)}h" if max_age > 0 else "max-age=off"
    # Fixed overhead: title, banner line, legend line, the legend's trailing
    # blank line, header (5). The cursor is hidden and the final write emits no
    # trailing newline, so rows may occupy every remaining line without
    # scrolling.
    term_lines = shutil.get_terminal_size().lines
    max_rows = max(3, term_lines - 5)
    hidden = 0
    visible = []
    for s in rows:
        # A session that died before its first real turn still has to be seen.
        if s.turns == 0 and not s.error:
            continue
        age = now - s.last_ts
        if max_age > 0 and age > max_age:
            hidden += 1
            continue
        visible.append((s, age))
    # Rows are most-recent-first, so what doesn't fit is the oldest.
    clipped = max(0, len(visible) - max_rows)
    visible = visible[:max_rows]
    notes = []
    if clipped:
        notes.append(f"{clipped} more not shown (terminal height)")
    if hidden:
        notes.append(f"{hidden} older hidden "
                     f"(>{fmt_hours(max_age)}h; --max-age 0 shows all)")
    note_txt = f"  {DIM}{'; '.join(notes)}{RESET}" if notes else ""
    out = [HOME + f"{BOLD}{TITLE}{RESET}  "
                  f"{DIM}{target}{RESET}{note_txt}"]
    # Emoji sits outside the dim escape so it renders at full intensity.
    coffee = "☕ " if caffeinate_running() else ""
    out.append(coffee
               + f"{DIM}window={fmt_k(window)}  smart-zone %% is of {fmt_k(window)}; "
                 f"red>=45%%, yellow>=30%%; {age_note}{RESET}")
    out.append(f"{DIM}! = recent error   || = probably stuck "
               f"(no recovery in 10m){RESET}")
    out.append("")
    hdr = (f"{'':2} {'ROLE':<18} {'MODEL':<8} {'CONTEXT':>9} {'% WIN':>6} "
           f"{'OUT':>8} {'TURNS':>5} {'LAST':>6}  {'NAME':<13} DIR")
    out.append(BOLD + hdr + RESET)
    # Printable width of the five middle columns (MODEL, CONTEXT, % WIN, OUT,
    # TURNS) plus their four inner separator spaces, exactly as written in the
    # row format string below. A session that errored before its first real turn
    # has nothing to put in those columns, so its error text fills this span
    # instead and LAST/DIR stay aligned with every other row. Keep this sum in
    # step with the field widths in the f-strings below.
    mid_w = 8 + 1 + 9 + 1 + 6 + 1 + 8 + 1 + 5   # == 40
    stuck_prefix = "stuck: "
    for s, age in visible:
        stuck = bool(s.error) and (now - s.error_ts) >= STALL_SECS
        if s.error:
            live, livec = ("||", RED) if stuck else ("!", RED)
        else:
            live, livec = ("\u25cf" if age < ACTIVE_SECS else " "), GREEN
        pct = 100.0 * s.context_tokens / window if window else 0.0
        col = pct_color(pct)
        if age < 120:
            when, wcol = f"{int(age)}s", BLUE
        elif age < 3600:
            when, wcol = f"{int(age/60)}m", GREEN
        elif age < 86400:
            when, wcol = f"{age/3600:.1f}h", BROWN
        else:
            when, wcol = f"{age/86400:.2f}d", GRAY
        role = s.label()[:18]
        rolec = CYAN if role != "main" else ""
        name = SESSION_NAMES.get(s.session_id)[:13]
        model = fmt_model(s.model)[:8]
        err_txt = ""
        mid = None
        if s.error:
            rolec = DIM + RED if stuck else RED
            if s.turns == 0:
                # No real turn ever completed: the token columns are all
                # placeholders, so the error text takes their place.
                if stuck:
                    body = stuck_prefix + shorten_error(
                        s.error, mid_w - len(stuck_prefix))
                    mid = f"{DIM}{body:<{mid_w}}{RESET}"
                else:
                    body = shorten_error(s.error, mid_w)
                    mid = f"{RED}{body:<{mid_w}}{RESET}"
            else:
                short = shorten_error(s.error)
                err_txt = (f"  {DIM}{stuck_prefix}{short}{RESET}" if stuck
                           else f"  {RED}{short}{RESET}")
        if mid is None:
            mid = (f"{model:<8} {fmt_k(s.context_tokens):>9} "
                   f"{col}{pct:>5.1f}%{RESET} "
                   f"{fmt_k(s.output_total):>8} {s.turns:>5}")
        out.append(
            f"{livec}{live:<2}{RESET} {rolec}{role:<18}{RESET} {mid} "
            f"{wcol}{when:>6}{RESET}"
            f"  {CYAN}{name:<13}{RESET} {DIM}{fmt_dir(s.cwd)}{RESET}{err_txt}"
        )
    sys.stdout.write("\033[K\n".join(out) + "\033[K\033[J")
    sys.stdout.flush()


def log_event(sess: Session, window: int):
    pct = 100.0 * sess.context_tokens / window if window else 0.0
    name = SESSION_NAMES.get(sess.session_id)
    print(f"{time.strftime('%H:%M:%S')} {sess.label():<16} "
          f"ctx={fmt_k(sess.context_tokens)} ({pct:.1f}%) "
          f"out={fmt_k(sess.output_total)} turns={sess.turns} "
          f"model={fmt_model(sess.model)} file={sess.path.name}"
          f"  {DIM}{name + ' ' if name else ''}{fmt_dir(sess.cwd)}{RESET}")


def log_error(sess: Session):
    """Event line for a synthetic error entry; no stall logic in log mode."""
    print(f"{RED}{time.strftime('%H:%M:%S')} {sess.label():<16} "
          f"{shorten_error(sess.error)} file={sess.path.name}{RESET}")


def inspect_one(target: Path):
    for path in reversed(jsonl_files(target)):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                entries = [json.loads(l) for l in f if l.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        for e in reversed(entries):
            if e.get("type") == "assistant" and (e.get("message") or {}).get("usage"):
                print(f"# {path}")
                print(json.dumps(e, indent=2)[:4000])
                return
    print("no assistant entry with usage found")


def maybe_respawn(start_mtime: float, respawn_argv: list[str]):
    """Re-exec this script in place if its source file changed on disk."""
    try:
        mtime = os.stat(SCRIPT_PATH).st_mtime
    except OSError:
        return
    if mtime == start_mtime:
        return
    # Don't exec a half-written file: require it to at least parse.
    try:
        with open(SCRIPT_PATH) as f:
            compile(f.read(), SCRIPT_PATH, "exec")
    except (OSError, SyntaxError):
        return  # try again next cycle once the write completes / parses
    sys.stdout.write("\ncctw.py changed on disk; respawning...\n")
    sys.stdout.flush()
    os.execv(sys.executable, respawn_argv)


def main():
    ap = argparse.ArgumentParser(description="Watch Claude Code transcripts live.")
    ap.add_argument("path", nargs="?", help="project dir, working dir, or "
                    ".jsonl file (default: all projects under "
                    "~/.claude/projects)")
    ap.add_argument("--window", type=int, default=200_000,
                    help="context window size for %% display (default 200000)")
    ap.add_argument("--max-age", type=float, default=0.0, metavar="HOURS",
                    help="hide sessions idle longer than this many hours "
                         "(default 0 = no limit; rows fill the terminal height)")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL, metavar="SECS",
                    help=f"refresh/poll interval in seconds (default {POLL_INTERVAL:g})")
    ap.add_argument("--log", action="store_true",
                    help="print an event line per assistant turn instead of a dashboard")
    ap.add_argument("--inspect", action="store_true",
                    help="dump the latest parsed assistant entry and exit")
    args = ap.parse_args()

    target = resolve_target(args.path) if args.path else default_project_dir()
    respawn_argv = [sys.executable, SCRIPT_PATH] + sys.argv[1:]
    if not args.path:
        respawn_argv.append(str(target))  # pin the resolved target across respawns
    if not target.exists():
        sys.exit(f"not found: {target}")

    if args.inspect:
        inspect_one(target)
        return

    cursors: dict[Path, FileCursor] = {}
    sessions: dict[str, Session] = {}
    start_mtime = os.stat(SCRIPT_PATH).st_mtime
    cutoff = args.max_age * 3600
    if not args.log:
        n = len(jsonl_files(target))
        sys.stdout.write(ALT_ON + CURSOR_OFF + CLEAR
                         + f"scanning {n} transcript files under {target} ...\n")
        sys.stdout.flush()
    try:
        while True:
            for path in jsonl_files(target):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                cur = cursors.get(path)
                if cur is None:
                    if cutoff > 0 and time.time() - mtime > cutoff:
                        continue      # too old to ever display; don't read it
                    cur = cursors[path] = FileCursor(path)
                for entry in read_new_lines(cur):
                    key = entry_key(path, entry)
                    sess = sessions.get(key)
                    if sess is None:
                        sess = sessions[key] = Session(path)
                    sess.feed(entry, mtime)
                    if args.log and entry.get("type") == "assistant":
                        if is_synthetic(entry):
                            log_error(sess)
                        else:
                            log_event(sess, args.window)
            if not args.log:
                render(sessions, args.window, target, args.max_age * 3600)
            maybe_respawn(start_mtime, respawn_argv)
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        pass
    finally:
        if not args.log:
            sys.stdout.write(CURSOR_ON + ALT_OFF)
            sys.stdout.flush()


if __name__ == "__main__":
    main()
