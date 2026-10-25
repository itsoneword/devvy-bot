"""Destructive-operation classifier for the confirmation gate.

Used only for **full-access** (unsandboxed) profiles like ``devops``. Sandboxed
profiles can't do cross-FS / network damage from bash, so they are never gated.

The threat model is an *unattended* full-access agent reading a prompt-injection
and silently destroying data. Defence is confirmation, not prohibition: when a
tool call matches one of these patterns the run pauses for a Telegram
approve/deny. The set is a *balanced curated* list — catch the irreversible /
system-level ops, let everyday commands (ls, git status/commit, builds, tests,
edits inside cwd) run freely.

``classify(tool_name, tool_input, working_directory)`` returns a short reason
string when the op should be confirmed, else ``None``.

This file is the one place for the *ask* rules, for every backend: the Claude
PreToolUse hook and the Codex PreToolUse hook both reach it through
``src/security/policy.py`` (dv-1601). Three kinds of rule:

* regexes over the command string: ``_BASH_RULES`` match commands, so they
  skip text inside quotes and heredocs unless that text is itself a command
  line (``sh -c 'kill 1'``, ``watch 'pkill x'``, ``"$(pkill x)"``) -- see
  ``_command_rule_reason`` (dv-af70); ``_LITERAL_RULES`` (SQL, fork bomb)
  match the raw text, because SQL lives in literals;
* the exchange-tool execution gate (``_money_script_reason``, dv-8664):
  confirm-by-default for any node/npm execution inside an
  ``EXCHANGE_TOOL_DIRS`` tree, except ``READ_ONLY_SCRIPT_NAMES``;
* deletion and outbound-transfer rules over parsed commands
  (``_deletion_reason`` / ``_egress_reason``), which also look inside
  ``sh -c '...'``;
* system-path rules for file-writing tools;
* merging a pump branch the pump flagged ``pump_privileged`` (dv-535b,
  ``src/storage/board_policy.py``).

All of them match the command *text*. ``python3 x.py`` shows a script name, not
what the script does -- these rules catch mistakes and plain injected
instructions, not a determined attacker.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..storage.board_policy import privileged_merge_reason

# Dirs under ~/tools that hold the exchange scripts (fetchcrypto, fetchmexc),
# including their lib/ subdirs -- every JS file under either tree is a money
# script by default (dv-8664). A fixed denylist of script names (the old
# MONEY_SCRIPT_NAMES approach below) silently let a brand-new script run
# without a button until someone remembered to add its name -- move_stop.js
# did exactly that on 2026-10-04.
EXCHANGE_TOOL_DIRS: Tuple[str, ...] = ("fetchcrypto", "fetchmexc")

# The short allowlist: read-only reports that never move money, so they run
# without a button. Everything else under EXCHANGE_TOOL_DIRS -- named today
# or not -- asks.
READ_ONLY_SCRIPT_NAMES: Tuple[str, ...] = (
    "index",
    "orders",
    "query_trades",
    "analytics",
)

# Known money-moving script names -- kept only as the Codex execpolicy
# backstop (src/agent_backends/codex_execpolicy.py, dv-4fc2). Codex's
# execpolicy is exact-pattern "forbidden" matching: it has no way to express
# "ask unless on the allowlist", so it still hard-forbids this fixed list by
# name rather than inverting to the allowlist. That means a brand-new script
# is NOT forbidden on Codex until added here -- same gap this ticket closes
# for Claude's ask-gate below. Codex devops already runs with no owner
# Approve/Deny path (dv-bf0c tracks the real fix); this list is a speed bump,
# not the primary defence.
MONEY_SCRIPT_NAMES: Tuple[str, ...] = (
    "buy",
    "sell",
    "perp",
    "close",
    "modify_order",
    "cancel_spot",
    "transfer",
    "move_stop",
)

# System roots a write/redirect must never touch without confirmation.
_SYSTEM_PREFIXES = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    "/var",
    "/root",
)

# (compiled pattern, human reason). Order doesn't matter; first match wins.
# Matched against commands, not literal text (``_command_rule_reason``).
_BASH_RULES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bdd\b[^|]*\bof="), "raw disk write (dd)"),
    (re.compile(r"\bmkfs\b"), "filesystem format (mkfs)"),
    (re.compile(r"\bshred\b"), "secure file wipe (shred)"),
    (re.compile(r"\btruncate\b"), "file truncation (truncate)"),
    (re.compile(r"\bwipefs\b"), "filesystem signature wipe (wipefs)"),
    (re.compile(r"\bgit\s+push\b[^\n]*\s(--force|-f)\b"), "force git push"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (
        re.compile(r"\bgit\s+clean\b[^\n]*-\w*f"),
        "git clean -f (deletes untracked files)",
    ),
    (re.compile(r"\bchmod\s+-\w*R"), "recursive permission change (chmod -R)"),
    (re.compile(r"\bchown\s+-\w*R"), "recursive ownership change (chown -R)"),
    (
        re.compile(r"\bsystemctl\s+(stop|restart|disable|mask|kill)\b"),
        "service control (systemctl)",
    ),
    (re.compile(r"\b(kill|pkill|killall)\b"), "process termination (kill/pkill)"),
    (
        re.compile(r"\bdocker\b[^\n]*\b(rm|rmi|prune)\b"),
        "docker resource removal (docker rm/prune)",
    ),
    (re.compile(r"\bmv\s+[^\n]+\s+/dev/null\b"), "move to /dev/null (data loss)"),
]

# Matched against the raw text, quotes and all: the SQL is *in* a literal
# (`sqlite3 db "DROP TABLE x"`), and the fork bomb is punctuation the segment
# split would break apart.
_LITERAL_RULES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (
        re.compile(r"\b(drop\s+(table|database)|truncate\s+table)\b", re.IGNORECASE),
        "destructive SQL (DROP/TRUNCATE)",
    ),
]

# A redirect ( > or >> ) whose target lands under a system prefix. The path
# stops at `)` / backtick / quote: `$(ls x 2>/dev/null)` targets /dev/null,
# not "/dev/null)" (dv-af70).
_REDIRECT_RE = re.compile(r">>?\s*(?P<path>/[^\s;&|()`'\"]+)")

# Redirecting a stream *into* these is never destructive: there's no real data
# there to damage (unlike e.g. /dev/sda, an actual device backing store).
_SAFE_REDIRECT_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"}

_BASH_TOOLS = {"Bash", "bash", "shell"}
_FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "create_file", "edit_file"}


# -- parsed-command rules (deletion, outbound transfer) ----------------------

# Words that only wrap the real command.
_WRAPPERS = {"sudo", "nohup", "time", "command", "exec", "env", "nice", "ionice"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_MAX_NESTING = 3

# `rm` embedded where the parser can't see it as a command (a string handed to
# an interpreter, a heredoc script): the pre-dv-1601 regexes, kept as a net.
_EMBEDDED_RM = re.compile(r"\brm\s+(-\w*\s+)*-\w*[rf]")

_DELETE_COMMANDS = {"rm", "unlink", "rmdir"}
_TRANSFER_COMMANDS = {
    "scp": "file copy to another host (scp)",
    "sftp": "file transfer to another host (sftp)",
    "ftp": "file transfer to another host (ftp)",
    "lftp": "file transfer to another host (lftp)",
    "rclone": "cloud file transfer (rclone)",
    "ssh": "remote shell on another host (ssh)",
    "nc": "raw network connection (nc)",
    "ncat": "raw network connection (ncat)",
    "netcat": "raw network connection (netcat)",
    "socat": "raw network connection (socat)",
    "telnet": "raw network connection (telnet)",
    "mail": "outbound email (mail)",
    "mailx": "outbound email (mailx)",
    "mutt": "outbound email (mutt)",
    "sendmail": "outbound email (sendmail)",
}
_CURL_LONG_UPLOAD = re.compile(r"^--(data\b|data-|form\b|form-|json\b|upload-file\b)")
_CURL_SHORT_UPLOAD = re.compile(r"^-[A-Za-z]*[dFT]")
_SENDING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_WGET_UPLOAD = re.compile(r"^--(post-data|post-file|body-data|body-file|method)\b")
_URL_HOST = re.compile(r"https?://(\[[^\]]+\]|[^/\s:'\"]+)")
_BARE_LOCAL = re.compile(
    r"(?:^|[\s'\"=])(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:[/\s'\"]|$)"
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "0.0.0.0"}
_RSYNC_REMOTE = re.compile(r"^(rsync://|[^/\s:]+@?[^/\s:]*:)")


def _segments(command: str) -> List[str]:
    """Split a shell line into simple commands, respecting quotes.

    Splits on ``; | & newline ( ) `` and ``$(`` outside quotes, so the command
    inside a pipe, a subshell or a substitution is a segment of its own.
    """
    out: List[str] = []
    buf: List[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(command[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
        elif ch == "\\" and i + 1 < len(command):
            buf.append(ch)
            buf.append(command[i + 1])
            i += 1
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in ";|&\n()`":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [seg.strip() for seg in out if seg.strip()]


def _argv(segment: str) -> List[str]:
    """Words of one simple command, minus wrappers and env assignments."""
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    while words:
        head = words[0]
        if head in ("{", "$", "!", "then", "do", "else"):
            words = words[1:]
        elif _ENV_ASSIGNMENT.match(head):
            words = words[1:]
        elif os.path.basename(head) in _WRAPPERS:
            words = words[1:]
            # the wrapper's own flags (`sudo -n`, `nice -n 5`)
            while words and words[0].startswith("-"):
                words = words[1:]
        elif os.path.basename(head) in ("timeout", "xargs"):
            words = words[1:]
            while words and (words[0].startswith("-") or words[0][:1].isdigit()):
                words = words[1:]
        else:
            break
    return words


def _commands(command: str, depth: int = 0) -> List[List[str]]:
    """Every simple command in *command*, including inside ``sh -c '...'``."""
    found: List[List[str]] = []
    for segment in _segments(command):
        argv = _argv(segment)
        if not argv:
            continue
        found.append(argv)
        name = os.path.basename(argv[0])
        if name in _SHELLS and depth < _MAX_NESTING:
            for idx, word in enumerate(argv[1:], start=1):
                if word.startswith("-") and "c" in word and idx + 1 < len(argv):
                    found.extend(_commands(argv[idx + 1], depth + 1))
                    break
    return found


# -- command rules vs. literal text (dv-af70) --------------------------------
#
# `kill` inside `python3 -c "... LIKE '%kill %'"` is SQL, not a command, yet
# the old whole-text regex asked on it. Command rules now see the line with
# every quoted string and heredoc body blanked; each blanked body is then
# checked as a command line of its own -- a rule must match at the *start* of
# one of its simple commands (after wrappers, `xargs`, a path prefix). That
# keeps `bash -c 'kill 1'`, `su -c 'kill 1'`, `watch 'pkill x'`,
# `find -exec sh -c 'kill ...'`, `"$(pkill x)"`, `bash <<EOF` and even
# `os.system('kill 1')` asking, while `LIKE '%kill %'` and
# `git commit -m "fix: kill stale lock"` do not. A literal that *starts* with
# a command word still asks (`LIKE 'kill%'`): fail-safe direction.

_HEREDOC = re.compile(r"<<(-?)[ \t]*(\\?)(['\"]?)([A-Za-z_][A-Za-z0-9_-]*)\3")


def _mask_literals(command: str) -> Tuple[str, List[str]]:
    """``(command with literal contents blanked, the literal bodies)``.

    Blanks the inside of '...', "..." and heredoc bodies (newlines kept, so
    line-bound rules still see lines). Double-quoted bodies come back with
    their backslash escapes undone. An unterminated quote masks nothing:
    the rules then see the raw text, as before (fail-safe).
    """
    out: List[str] = []
    bodies: List[str] = []
    heredocs: List[Tuple[str, bool]] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            out.append(command[i : i + 2])
            i += 2
        elif ch == "'":
            ansi = i > 0 and command[i - 1] == "$"
            j = i + 1
            while j < n and command[j] != "'":
                j += 2 if ansi and command[j] == "\\" else 1
            if j >= n:
                return command, []
            bodies.append(command[i + 1 : j])
            out.append("'" + _blank(command[i + 1 : j]) + "'")
            i = j + 1
        elif ch == '"':
            j = i + 1
            body: List[str] = []
            while j < n and command[j] != '"':
                if command[j] == "\\" and j + 1 < n:
                    nxt = command[j + 1]
                    if nxt in '$`"\\\n':
                        body.append(nxt)
                    else:
                        body.append(command[j : j + 2])
                    j += 2
                else:
                    body.append(command[j])
                    j += 1
            if j >= n:
                return command, []
            bodies.append("".join(body))
            out.append('"' + _blank(command[i + 1 : j]) + '"')
            i = j + 1
        elif (
            ch == "<"
            and command.startswith("<<", i)
            and not command.startswith("<<<", i)
        ):
            m = _HEREDOC.match(command, i)
            if not m:
                out.append(ch)
                i += 1
                continue
            heredocs.append((m.group(4), m.group(1) == "-"))
            out.append(m.group(0))
            i = m.end()
        elif ch == "\n" and heredocs:
            out.append(ch)
            i += 1
            for delimiter, strip_tabs in heredocs:
                lines: List[str] = []
                while i < n:
                    end = command.find("\n", i)
                    end = n if end < 0 else end
                    line = command[i:end]
                    i = min(end + 1, n)
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        out.append(line + ("\n" if end < n else ""))
                        break
                    lines.append(line)
                    out.append(_blank(line) + ("\n" if end < n else ""))
                bodies.append("\n".join(lines))
            heredocs = []
        else:
            out.append(ch)
            i += 1
    return "".join(out), bodies


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _command_rule_reason(command: str, depth: int = 0) -> Optional[str]:
    """First ``_BASH_RULES`` hit on a command (not on literal text) in *command*.

    At the top level a rule may match anywhere in the masked line (as before
    dv-af70, minus the literals); inside a literal it must match at the start
    of a simple command, since a literal is usually data.
    """
    masked, bodies = _mask_literals(command)
    if depth == 0:
        for pattern, reason in _BASH_RULES:
            if pattern.search(masked):
                return reason
    else:
        for segment in _segments(masked):
            argv = _argv(segment)
            if not argv:
                continue
            line = " ".join([os.path.basename(argv[0]), *argv[1:]])
            for pattern, reason in _BASH_RULES:
                if pattern.match(line):
                    return reason
    if depth < _MAX_NESTING:
        for body in bodies:
            hit = _command_rule_reason(body, depth + 1)
            if hit:
                return hit
    return None


# -- exchange-tool execution gate (dv-8664) ----------------------------------
#
# Any execution of a JS file under an EXCHANGE_TOOL_DIRS tree asks, unless the
# script is on READ_ONLY_SCRIPT_NAMES. "Execution" = node/nodejs (bare,
# absolute, or an npm/node alias basename) running the file, under any path
# spelling: bare name while the shell's cwd is the exchange dir (tracked
# through a preceding `cd` in the same command), `./x.js`, `fetchcrypto/x.js`,
# `~/tools/fetchcrypto/x.js`, an absolute path, or `cd ~/tools/fetchcrypto &&
# node x.js`. `npm run` / `npx` inside those dirs ask too, since the script
# name isn't visible on the command line. Reading/searching/editing the files
# (grep, head, cat, sed -n, less, wc, the Read tool) never goes through a
# Bash-tool node/npm invocation, so it is untouched by this rule.
#
# Directory membership from an explicit path (text) or a `cd` *within the
# same command* is judged structurally (path components), not by resolving
# against the real filesystem -- deterministic in tests, and doesn't care
# which user's home the real paths sit under.
#
# A bare/relative script name with no such in-command evidence is different:
# the Bash tool's shell persists its cwd *across separate tool calls*, so a
# `cd ~/tools/fetchcrypto` in one call followed by a bare `node perp.js` in
# the next is invisible here -- `working_directory` is the turn's nominal
# cwd, not the real, currently-persisted shell cwd. For that case we fall
# back to what's actually on disk right now (dv-8664 follow-up): a brand-new
# script still gates even called bare, at the accepted cost of also gating
# some unrelated project's same-named script.

_NODE_PROGRAM_BASENAMES = {"node", "nodejs"}

_EXCHANGE_DIR_IN_PATH = re.compile(
    r"(?<![\w.-])(?:" + "|".join(re.escape(d) for d in EXCHANGE_TOOL_DIRS) + r")(?=/)"
)


def _is_node_program(head: str) -> bool:
    return os.path.basename(head) in _NODE_PROGRAM_BASENAMES


def _dir_is_exchange(path_text: str) -> bool:
    parts = [p for p in path_text.split("/") if p]
    return any(p in EXCHANGE_TOOL_DIRS for p in parts)


def _exchange_tools_root() -> Path:
    return Path.home() / "tools"


def _scripts_on_disk() -> "frozenset[str]":
    """Basenames (no ``.js``) of every script file currently sitting under
    an ``EXCHANGE_TOOL_DIRS`` tree, including its ``lib/`` subdir. Read fresh
    on every call -- a script added since the process started still counts."""
    names = set()
    root = _exchange_tools_root()
    for d in EXCHANGE_TOOL_DIRS:
        for sub in (root / d, root / d / "lib"):
            try:
                entries = list(sub.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_file() and entry.suffix == ".js":
                    names.add(entry.stem)
    return frozenset(names)


def _resolve_cd(target: str, effective_dir: Optional[str]) -> Optional[str]:
    """Best-effort textual ``cd`` resolution; ``None`` if it can't be followed."""
    if "$" in target or "`" in target:
        return None
    if target.startswith("/") or target.startswith("~"):
        return target
    if effective_dir is None:
        return None
    return effective_dir.rstrip("/") + "/" + target


def _exchange_script_reason(token: str, effective_dir: Optional[str]) -> Optional[str]:
    """Reason string if *token* names a gated JS file, else ``None``.

    *effective_dir* is ``None`` unless a `cd` ran earlier in this same
    command and resolved cleanly -- it is never seeded from the turn's
    nominal cwd, which can't be trusted to match the shell's real, currently
    persisted cwd (see module note above).
    """
    if not token.endswith(".js"):
        return None
    name = Path(token).stem
    if _EXCHANGE_DIR_IN_PATH.search(token):
        in_dir: Optional[bool] = True
    elif token.startswith("/") or token.startswith("~"):
        # Absolute/home path that doesn't mention either exchange dir --
        # explicitly resolvable, and it says "elsewhere".
        in_dir = False
    elif effective_dir is not None:
        in_dir = _dir_is_exchange(effective_dir)
    else:
        in_dir = None  # not resolvably outside the exchange dirs
    if in_dir is False:
        return None
    if in_dir is None and name not in _scripts_on_disk():
        return None
    if name in READ_ONLY_SCRIPT_NAMES:
        return None
    return (
        f"exchange tool script ({name}.js -- fetchcrypto or fetchmexc; not on "
        "the read-only allowlist)"
    )


def _money_script_reason(command: str) -> Optional[str]:
    """Ask for any node/npm execution inside an exchange tool dir (dv-8664)."""
    effective_dir: Optional[str] = None
    for argv in _commands(command):
        head = argv[0]
        base = os.path.basename(head)
        if base == "cd":
            target = argv[1] if len(argv) > 1 else "~"
            effective_dir = _resolve_cd(target, effective_dir)
            continue
        if _is_node_program(head):
            rest = argv[1:]
            if "--check" in rest:
                continue  # parses only, never executes
            script_args = [w for w in rest if not w.startswith("-")]
            if not script_args:
                continue
            reason = _exchange_script_reason(script_args[0], effective_dir)
            if reason:
                return reason
            continue
        if base == "npm" and len(argv) >= 2 and argv[1] == "run":
            if effective_dir is not None and _dir_is_exchange(effective_dir):
                return "exchange tool command (npm run -- fetchcrypto or fetchmexc)"
            continue
        if base == "npx":
            if effective_dir is not None and _dir_is_exchange(effective_dir):
                return "exchange tool command (npx -- fetchcrypto or fetchmexc)"
    return None


def _temp_roots() -> Tuple[str, ...]:
    return tuple({"/tmp", "/var/tmp", os.path.normpath(tempfile.gettempdir())})


def _is_temp_path(target: str, cwd: Path) -> bool:
    """True when *target* is strictly inside a temp dir (never the dir itself)."""
    if "$" in target or "`" in target:
        return False
    path = os.path.expanduser(target)
    if not os.path.isabs(path):
        path = os.path.join(str(cwd), path)
    path = os.path.normpath(path)
    return any(path.startswith(root + "/") for root in _temp_roots())


def _deletion_reason(commands: List[List[str]], cwd: Path) -> Optional[str]:
    """Deleting anything outside the temp dirs needs the owner (dv-1601)."""
    for argv in commands:
        name = os.path.basename(argv[0])
        if name in _DELETE_COMMANDS:
            flags = [w for w in argv[1:] if w.startswith("-") and w != "--"]
            targets = [w for w in argv[1:] if not w.startswith("-")]
            if targets and all(_is_temp_path(t, cwd) for t in targets):
                continue
            forced = any(set(f.lstrip("-")) & {"r", "R", "f"} for f in flags)
            if name == "rm" and forced:
                return "recursive/forced file deletion (rm)"
            return f"file deletion ({name})"
        if name == "find":
            if "-delete" in argv:
                return "file deletion (find -delete)"
            for idx, word in enumerate(argv):
                if word in ("-exec", "-execdir", "-ok") and idx + 1 < len(argv):
                    if os.path.basename(argv[idx + 1]) in _DELETE_COMMANDS:
                        return "file deletion (find -exec rm)"
    return None


def _all_local(argv: List[str]) -> bool:
    """True when every host the command names is this machine."""
    text = " ".join(argv)
    hosts = _URL_HOST.findall(text)
    if hosts:
        return all(h.lower() in _LOCAL_HOSTS for h in hosts)
    return bool(_BARE_LOCAL.search(text))


def _egress_reason(commands: List[List[str]]) -> Optional[str]:
    """Sending data to another host needs the owner (dv-1601).

    Plain downloads (``curl URL``, ``wget URL``, ``git pull``) are not gated;
    uploads, remote shells and pushes are. Requests to localhost are exempt.
    """
    for argv in commands:
        name = os.path.basename(argv[0])
        args = argv[1:]
        if name in _TRANSFER_COMMANDS:
            return _TRANSFER_COMMANDS[name]
        if name == "rsync":
            if any(_RSYNC_REMOTE.match(a) for a in args if not a.startswith("-")):
                return "file sync to another host (rsync)"
        elif name == "git":
            if "push" in args:
                return "git push (sends commits to a remote)"
        elif name == "curl":
            sends = any(
                _CURL_LONG_UPLOAD.match(a) or _CURL_SHORT_UPLOAD.match(a) for a in args
            )
            for idx, word in enumerate(args):
                method = ""
                if word in ("-X", "--request") and idx + 1 < len(args):
                    method = args[idx + 1]
                elif word.startswith("--request="):
                    method = word.split("=", 1)[1]
                elif re.match(r"^-[A-Za-z]*X", word):
                    method = word[word.index("X") + 1 :] or (
                        args[idx + 1] if idx + 1 < len(args) else ""
                    )
                if method.upper() in _SENDING_METHODS:
                    sends = True
            if sends and not _all_local(argv):
                return "data upload to another host (curl)"
        elif name == "wget":
            if any(_WGET_UPLOAD.match(a) for a in args) and not _all_local(argv):
                return "data upload to another host (wget)"
        elif name == "aws":
            if len(args) >= 2 and args[0] == "s3" and args[1] in ("cp", "sync", "mv"):
                return "cloud file transfer (aws s3)"
        elif name == "gsutil":
            if args and args[0] in ("cp", "rsync", "mv"):
                return "cloud file transfer (gsutil)"
        elif name == "gh":
            if args[:2] == ["gist", "create"]:
                return "publishing a gist (gh gist create)"
    return None


def _under_system_prefix(path_str: str) -> bool:
    try:
        resolved = Path(path_str).expanduser()
    except (ValueError, RuntimeError):
        return False
    p = resolved.as_posix()
    return any(p == pre or p.startswith(pre + "/") for pre in _SYSTEM_PREFIXES)


def _is_safe_redirect_target(path_str: str) -> bool:
    try:
        resolved = Path(path_str).expanduser()
    except (ValueError, RuntimeError):
        return False
    return resolved.as_posix() in _SAFE_REDIRECT_TARGETS


def classify(
    tool_name: str,
    tool_input: Dict[str, Any],
    working_directory: Path,
) -> Optional[str]:
    """Return a reason string if the op needs confirmation, else None."""
    if tool_name in _BASH_TOOLS:
        command = tool_input.get("command", "") or ""
        rule_reason = _command_rule_reason(command)
        if rule_reason:
            return rule_reason
        for pattern, reason in _LITERAL_RULES:
            if pattern.search(command):
                return reason
        money_reason = _money_script_reason(command)
        if money_reason:
            return money_reason
        # dv-535b: a pump branch an agent-filed task built over privileged
        # paths (sandbox, security, tool rights, ...) is merged only after an
        # explicit owner OK. The pump marks it; this is the button.
        merge_reason = privileged_merge_reason(command, working_directory)
        if merge_reason:
            return merge_reason
        commands = _commands(command)
        parsed_reason = _deletion_reason(commands, working_directory) or _egress_reason(
            commands
        )
        if parsed_reason:
            return parsed_reason
        if _EMBEDDED_RM.search(command) and not any(
            os.path.basename(argv[0]) == "rm" for argv in commands
        ):
            return "recursive/forced file deletion (rm)"
        # Redirect into a system path (e.g. `echo x > /etc/hosts`). Discarding
        # a stream into /dev/null (or the equivalent stdout/stderr/stdin
        # pseudo-files) is exempt — there's nothing there to damage.
        for m in _REDIRECT_RE.finditer(command):
            path = m.group("path")
            if _is_safe_redirect_target(path):
                continue
            if _under_system_prefix(path):
                return f"redirect into system path ({path})"
        return None

    if tool_name in _FILE_WRITE_TOOLS:
        path_str = tool_input.get("file_path") or tool_input.get("path")
        if not path_str:
            return None
        # Only system/sensitive locations are gated; ordinary cross-directory
        # writes (e.g. the notes vault) run freely, and the security denylist
        # is the backstop for secret files.
        if _under_system_prefix(str(path_str)):
            return f"write to system path ({path_str})"
        return None

    return None


# -- exchange_* agent tools (dv-8664) ---------------------------------------
#
# The bot's exchange tools (src/claude/exchange_tools.py) run the same
# fetchcrypto / fetchmexc scripts as the Bash rule above, but as named MCP
# tools, so they are gated by tool name instead of by command text.
#
# Default-deny: every tool whose name starts with ``exchange_`` asks the
# owner, on every profile (not only full-access ones), unless it is listed in
# EXCHANGE_READ_ONLY_TOOLS. A new exchange tool nobody marked asks.
# EXCHANGE_DRY_RUN_TOOLS are write tools that honour ``dry_run: true`` (the
# script prints its preview and exits before any signed write): such a call
# runs without a button; any other value asks.

EXCHANGE_TOOL_PREFIX = "exchange_"
EXCHANGE_READ_ONLY_TOOLS: "frozenset[str]" = frozenset(
    {
        "exchange_accounts",
        "exchange_report",
        "exchange_open_orders",
        "exchange_trades",
        "exchange_analytics",
    }
)
EXCHANGE_DRY_RUN_TOOLS: "frozenset[str]" = frozenset(
    {
        "exchange_place_order",
        "exchange_close_position",
        "exchange_move_stop",
        "exchange_modify_order",
        "exchange_cancel_order",
        "exchange_transfer",
    }
)
# The MCP server the bot mounts its own tools under, on both backends
# (``mcp__agent__<tool>``). A same-named tool on another server is never
# treated as read-only.
_EXCHANGE_TOOL_SERVER = "agent"
# Shown on the owner's confirmation, in this order, when present.
_EXCHANGE_PREVIEW_KEYS = (
    "exchange",
    "account",
    "market",
    "symbol",
    "side",
    "type",
    "quantity",
    "quote_amount",
    "margin",
    "leverage",
    "price",
    "stop_price",
    "take_profit",
    "isolated",
    "order_id",
    "from",
    "to",
    "asset",
    "amount",
)


def _split_tool_name(tool_name: str) -> Tuple[Optional[str], str]:
    """``(server, bare name)``; server is None for an unprefixed name."""
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3:
            return parts[1], parts[-1]
    return None, tool_name


def exchange_tool_ask(
    tool_name: str, tool_input: Dict[str, Any]
) -> Optional[Tuple[str, str]]:
    """``(reason, preview)`` when an exchange tool call needs the owner, else None."""
    server, name = _split_tool_name(tool_name or "")
    if not name.startswith(EXCHANGE_TOOL_PREFIX):
        return None
    ours = server in (None, _EXCHANGE_TOOL_SERVER)
    if ours and name in EXCHANGE_READ_ONLY_TOOLS:
        return None
    args: Dict[str, Any] = tool_input or {}
    # A harness may wrap MCP arguments; judge the arguments themselves.
    if isinstance(args.get("arguments"), dict):
        args = args["arguments"]
    if ours and name in EXCHANGE_DRY_RUN_TOOLS and args.get("dry_run") is True:
        return None
    action = name[len(EXCHANGE_TOOL_PREFIX) :].replace("_", " ")
    head = " ".join(
        str(args[key])
        for key in ("exchange", "account", "market", "symbol", "side")
        if args.get(key) not in (None, "")
    )
    reason = f"exchange: {action}" + (f" ({head})" if head else "")
    lines = [
        f"{key}: {args[key]}"
        for key in _EXCHANGE_PREVIEW_KEYS
        if args.get(key) not in (None, "")
    ]
    known = set(_EXCHANGE_PREVIEW_KEYS) | {"dry_run"}
    lines += [f"{key}: {args[key]}" for key in sorted(args) if key not in known]
    return reason, "\n".join([tool_name, *lines])
