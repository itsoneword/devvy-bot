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

* regexes over the command string: ``_BASH_RULES`` match at the start of a
  command a shell runs, so text inside quotes and heredocs is data unless a
  shell executes it (``sh -c 'kill 1'``, ``bash <<EOF``, ``"$(pkill x)"``)
  -- see ``_walk`` / ``_executed_texts`` (dv-af70, dv-3d18);
  ``_LITERAL_RULES`` (SQL, fork bomb) match the raw text, because SQL lives
  in literals;
* the exchange-tool execution gate (``_money_script_reason``, dv-8664):
  confirm-by-default for any node/npm execution inside an
  ``EXCHANGE_TOOL_DIRS`` tree, except ``READ_ONLY_SCRIPT_NAMES``;
* deletion and outbound-transfer rules over the same parsed commands
  (``_deletion_reason`` / ``_egress_reason``);
* system-path rules for file-writing tools;
* merging a pump branch the pump flagged ``pump_privileged`` (dv-535b,
  ``src/storage/board_policy.py``).

All of them match the command *text*. ``python3 x.py`` shows a script name, not
what the script does -- these rules catch mistakes and plain injected
instructions, not a determined attacker.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from ..storage.board_policy import privileged_merge_reason
from .denylist import STREAM_PSEUDO_FILES

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
_SAFE_REDIRECT_TARGETS = STREAM_PSEUDO_FILES

_BASH_TOOLS = {"Bash", "bash", "shell"}
_FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "create_file", "edit_file"}


# -- parsed-command rules (deletion, outbound transfer) ----------------------

# Words that only wrap the real command.
_WRAPPERS = {"sudo", "nohup", "time", "command", "exec", "env", "nice", "ionice"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_MAX_NESTING = 3
# Words that open a list or a compound command, before the real command word.
_LIST_KEYWORDS = {
    "{",
    "$",
    "!",
    "then",
    "do",
    "else",
    "if",
    "elif",
    "while",
    "until",
}
# `timeout` / `xargs` flags whose value is the next word (`xargs -I {} kill`).
_VALUE_FLAGS = {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a", "-k"}
# Wrapper options that consume the following word. Joined short forms and
# --option=value already contain their value and need no extra skip.
_WRAPPER_VALUE_FLAGS = {
    "sudo": {
        "-u",
        "-g",
        "-h",
        "-p",
        "-C",
        "-r",
        "-t",
        "--user",
        "--group",
        "--host",
        "--prompt",
        "--close-from",
        "--role",
        "--type",
        "--other-user",
        "--chdir",
    },
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "ionice": {
        "-c",
        "-n",
        "-p",
        "-P",
        "-u",
        "--class",
        "--classdata",
        "--pid",
        "--pgid",
        "--uid",
    },
    "time": {"-f", "-o", "--format", "--output"},
    "exec": {"-a"},
}
# Commands that run the text they are fed (stdin, a pipe, `<(...)`) as a script.
_SCRIPT_READERS = _SHELLS | {"source", "."}
_UNKNOWN_SHELL_INPUT = "__unresolved_shell_input__"
# ssh options that take a value; anything after the host is the remote command.
_SSH_VALUE_FLAGS = set("bcDEeFIiJLlmOoPpQRSWw")

# `rm -rf /` written into interpreter code (`os.system('rm -rf /')`), where no
# shell parse can see it: a narrow net over the raw text, for the catastrophic
# targets only. Ordinary `rm -rf build` in a doc or a grep pattern is data
# (dv-3d18; it replaced the old any-`rm -rf`-anywhere net).
_EMBEDDED_RM = re.compile(
    r"\brm\s+(?:-\w+\s+)*-\w*[rRf]\w*\s+(?:-\w+\s+)*(?:--\s+)?"
    r"(?P<target>[^\s;&|()'\"`]+)"
)
_CATASTROPHIC_RM_TARGETS = {
    "/",
    "/*",
    "~",
    "~/",
    "~/*",
    "$HOME",
    "$HOME/",
    "$HOME/*",
    "${HOME}",
    "${HOME}/",
    "${HOME}/*",
    "..",
    "../",
    "../*",
}

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
_GH_RELEASE_WRITES = {"create", "upload", "edit", "delete", "delete-asset"}
_WGET_UPLOAD = re.compile(r"^--(post-data|post-file|body-data|body-file|method)\b")
_URL_HOST = re.compile(r"https?://(\[[^\]]+\]|[^/\s:'\"]+)")
_BARE_LOCAL = re.compile(
    r"(?:^|[\s'\"=])(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:[/\s'\"]|$)"
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "0.0.0.0"}
_RSYNC_REMOTE = re.compile(r"^(rsync://|[^/\s:]+@?[^/\s:]*:)")


# -- parsing a shell line (dv-1601, heredoc- and literal-aware since dv-3d18) --
#
# A risky verb counts only where a shell runs it. ``_segments`` splits a line
# into simple commands; heredoc bodies and quoted strings stay data, except in
# the positions where a shell executes them (``_executed_texts``): the script
# of ``sh -c`` / ``su -c``, ``watch``, ``eval``, ``trap``, ``ssh host '...'``,
# ``find -exec``, a heredoc / here-string / pipe / process substitution read
# by a shell or by ``source /dev/stdin``, and ``$(...)`` or backticks inside
# double quotes. Those are parsed again as command lines, recursively. So
# ``python3 - <<'PY'`` whose text mentions ``systemctl restart`` or
# ``x = (nc + 1)`` is not a command, while ``bash <<EOF`` with ``kill 1``,
# ``echo 'kill 1' | sh`` and ``bash -c 'kill 1'`` are.

_HEREDOC = re.compile(r"<<(-?)[ \t]*(\\?)(['\"]?)([A-Za-z_][A-Za-z0-9_-]*)\3")


@dataclass
class _Segment:
    """One simple command of a shell line.

    *text* keeps its quotes; heredoc bodies are cut out of it into
    *heredocs*. *piped*: the previous segment's output is this one's input.
    *owner*: for a ``<(...)`` / ``>(...)`` process substitution, the segment
    of the command it is an argument of.
    """

    text: str = ""
    heredocs: List[str] = field(default_factory=list)
    piped: bool = False
    owner: Optional["_Segment"] = None
    # Top-level list item (split on ``; && || newline`` outside any group or
    # substitution) and whether that item is this one simple command alone:
    # no pipe, ``&``, group, subshell or substitution (dv-3d18).
    item: int = 0
    plain: bool = False
    # Ends with a ``&`` job or defines a function: text order is not run
    # order anywhere in the line.
    unordered: bool = False
    # The list operator (``&&``, ``||``, ``;``, newline) that ends this
    # segment's top-level item, set on the item's last segment (dv-3d18).
    joiner: str = ""


def _heredoc_end(
    command: str, i: int, delimiter: str, strip_tabs: bool
) -> Tuple[int, int]:
    """``(end of body, end of terminator line)`` of a heredoc body at *i*.

    The one heredoc scanner, used by ``_segments`` and ``_mask_literals``. An
    unterminated body runs to the end of *command*.
    """
    n = len(command)
    while i < n:
        end = command.find("\n", i)
        end = n if end < 0 else end
        line = command[i:end]
        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
            return i, min(end + 1, n)
        i = end + 1
    return n, n


def _heredoc_body(command: str, start: int, body_end: int) -> str:
    body = command[start:body_end]
    return body[:-1] if body.endswith("\n") else body


def _segments(command: str) -> List[_Segment]:
    """Split a shell line into simple commands, respecting quotes and heredocs.

    Splits on ``; | & newline ( ) `` and ``$(`` outside quotes, so the command
    inside a pipe, a subshell or a substitution is a segment of its own. A
    heredoc body is never split into commands: it is kept on the segment that
    reads it (dv-3d18). A ``&`` inside a redirect (``2>&1``, ``&>f``) is not
    a separator. Each segment records its top-level list item and whether it
    runs alone in it (``plain``), for the free-root order rule.
    """
    out: List[_Segment] = []
    buf: List[str] = []
    cur = _Segment()
    pending: List[Tuple[str, bool, List[str]]] = []
    # one entry per open `(`: the owner segment of a process substitution,
    # None for any other paren
    parens: List[Optional[_Segment]] = []
    resume: Optional[_Segment] = None  # whose arguments the text after `)` is
    quote = ""
    item = 0
    complex_items: "set[int]" = set()
    in_backtick = False
    i, n = 0, len(command)

    def flush(
        piped: bool = False, owner: Optional[_Segment] = None
    ) -> Optional[_Segment]:
        nonlocal cur, buf
        text = "".join(buf).strip()
        buf = []
        done: Optional[_Segment] = None
        if text:
            cur.text = text
            cur.item = item
            out.append(cur)
            done = cur
            cur = _Segment(piped=piped, owner=owner)
        else:
            cur.piped = cur.piped or piped
            cur.owner = cur.owner or owner
        return done

    def end_item(joiner: str) -> None:
        nonlocal item
        if not parens and not in_backtick:
            if out and out[-1].item == item:
                out[-1].joiner = joiner
            item += 1

    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
        elif ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 1
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif (
            ch == "<"
            and command.startswith("<<", i)
            and not command.startswith("<<<", i)
        ):
            m = _HEREDOC.match(command, i)
            if not m:
                buf.append(ch)
            else:
                pending.append((m.group(4), m.group(1) == "-", cur.heredocs))
                buf.append(m.group(0))
                i = m.end()
                continue
        elif ch == "|" and command.startswith("||", i):
            flush()
            resume = None
            end_item("||")
            i += 1
        elif ch == "|":
            flush(piped=True)
            resume = None
            complex_items.add(item)
        elif ch == "&" and command.startswith("&&", i):
            flush()
            resume = None
            end_item("&&")
            i += 1
        elif ch == "&" and ((buf and buf[-1] in "<>") or command.startswith("&>", i)):
            buf.append(ch)  # a redirect (`2>&1`, `&>f`), not a separator
        elif ch == "(" and buf and buf[-1] in "<>":
            buf.pop()
            done = flush()
            owner = done or resume or cur
            parens.append(owner)
            cur.owner = owner
            complex_items.add(item)
        elif ch == "(":
            done = flush()
            if command[i + 1 :].lstrip(" \t").startswith(")") and (done or out):
                (done or out[-1]).unordered = True  # `name() { ...; }`
            parens.append(None)
            complex_items.add(item)
        elif ch == ")":
            flush()
            resume = parens.pop() if parens else None
            complex_items.add(item)
        elif ch in ";&\n`":
            done = flush()
            resume = None
            if ch == "&":
                if done or out:
                    (done or out[-1]).unordered = True  # a `&` job
                complex_items.add(item)
            if ch == "`":
                in_backtick = not in_backtick
                complex_items.add(item)
            else:
                end_item(ch)
            if ch == "\n" and pending:
                i += 1
                for delimiter, strip_tabs, owner_bodies in pending:
                    body_end, after = _heredoc_end(command, i, delimiter, strip_tabs)
                    owner_bodies.append(_heredoc_body(command, i, body_end))
                    i = after
                pending = []
                continue
        else:
            buf.append(ch)
        i += 1
    flush()
    sizes: Dict[int, int] = {}
    for seg in out:
        sizes[seg.item] = sizes.get(seg.item, 0) + 1
    for seg in out:
        seg.plain = (
            seg.item not in complex_items
            and sizes[seg.item] == 1
            and not any(mark in seg.text for mark in ("$(", "`", "<(", ">("))
        )
    return out


def _argv(segment: str, stripped: Optional[List[str]] = None) -> List[str]:
    """Words of one simple command, minus wrappers and env assignments.

    Each wrapper removed (``sudo``, ``xargs``, ...) is appended to *stripped*
    when given.
    """
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    while words:
        head = words[0]
        if head in _LIST_KEYWORDS:
            words = words[1:]
        elif _ENV_ASSIGNMENT.match(head):
            words = words[1:]
        elif os.path.basename(head) in _WRAPPERS:
            wrapper = os.path.basename(head)
            if stripped is not None:
                stripped.append(wrapper)
            words = words[1:]
            # Skip the wrapper's options and their values before the command.
            while words and words[0].startswith("-"):
                flag = words.pop(0)
                if flag == "--":
                    break
                if flag in _WRAPPER_VALUE_FLAGS.get(wrapper, ()) and words:
                    value = words.pop(0)
                    if wrapper == "env" and flag in ("-S", "--split-string"):
                        try:
                            words = shlex.split(value) + words
                        except ValueError:
                            words = value.split() + words
                elif wrapper == "env" and flag.startswith("--split-string="):
                    value = flag.partition("=")[2]
                    try:
                        words = shlex.split(value) + words
                    except ValueError:
                        words = value.split() + words
        elif os.path.basename(head) in ("timeout", "xargs"):
            if stripped is not None:
                stripped.append(os.path.basename(head))
            words = words[1:]
            while words and (words[0].startswith("-") or words[0][:1].isdigit()):
                if words[0] in _VALUE_FLAGS and len(words) > 1:
                    words = words[1:]
                words = words[1:]
        else:
            break
    return words


def _shell_script_arg(argv: List[str]) -> Tuple[Optional[str], bool]:
    """``(the -c script, reads its script from stdin)`` for a shell's argv."""
    args = argv[1:]
    idx = 0
    while idx < len(args):
        word = args[idx]
        if word in ("-o", "+o", "-O", "+O"):
            idx += 2
            continue
        if word.startswith("-") and not word.startswith("--") and len(word) > 1:
            if "c" in word:
                return (args[idx + 1] if idx + 1 < len(args) else None), False
            if "s" in word:
                return None, True
        elif word.startswith("<<<"):
            idx += 1 if word != "<<<" else 2
            continue
        elif word == "-" or re.match(r"^\d*[<>]", word):
            pass
        elif not word.startswith("-"):
            return None, False  # `bash script.sh`: the script is a file
        idx += 1
    return None, True


def _herestrings(argv: List[str]) -> List[str]:
    out = []
    for idx, word in enumerate(argv):
        if word == "<<<" and idx + 1 < len(argv):
            out.append(argv[idx + 1])
        elif word.startswith("<<<") and word != "<<<":
            out.append(word[3:])
    return out


def _quoted_substitutions(text: str) -> List[str]:
    """Bodies of ``$(...)`` and backticks inside double quotes in *text*."""
    out: List[str] = []
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = ""
        elif ch == "\\":
            # An escaped dollar or backtick is literal at this shell level.
            # A shell passed the text by `-c` is walked separately below.
            i += 1
        elif quote == '"' and ch == "`":
            end = text.find("`", i + 1)
            end = n if end < 0 else end
            out.append(text[i + 1 : end])
            i = end
        elif quote == '"' and text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            out.append(text[i + 2 : j - 1 if not depth else j])
            i = j - 1
        elif ch == '"':
            quote = "" if quote else '"'
        elif ch == "'" and not quote:
            quote = "'"
        i += 1
    return out


def _input_text(seg: _Segment, argv: List[str]) -> List[str]:
    """Literal output from *seg*, or a marker when it cannot be determined."""
    if argv and os.path.basename(argv[0]) == "echo":
        idx, escapes = 1, False
        while idx < len(argv) and re.fullmatch(r"-[neE]+", argv[idx]):
            for flag in argv[idx][1:]:
                if flag in "eE":
                    escapes = flag == "e"
            idx += 1
        output = " ".join(argv[idx:])
        if escapes and "\\" in output:
            output = _UNKNOWN_SHELL_INPUT
        return [*seg.heredocs, *_herestrings(argv), output]
    if argv and os.path.basename(argv[0]) == "printf" and len(argv) > 1:
        fmt, values = argv[1], argv[2:]
        if "\\" not in fmt and re.fullmatch(r"(?:[^%]|%%|%s)*", fmt):
            count = sum(m.group() == "%s" for m in re.finditer(r"%%|%s", fmt))
            if count:
                chunks = []
                for offset in range(0, max(len(values), 1), count):
                    args = (values[offset : offset + count] + [""] * count)[:count]
                    chunks.append(fmt % tuple(args))
                output = "".join(chunks)
            else:
                output = fmt.replace("%%", "%")
        else:
            # Unsupported conversions and escapes can produce commands that
            # are absent from the literal arguments. Ask when a shell reads it.
            output = _UNKNOWN_SHELL_INPUT
        return [*seg.heredocs, *_herestrings(argv), output]
    inputs = [*seg.heredocs, *_herestrings(argv)]
    if argv and os.path.basename(argv[0]) == "cat" and seg.heredocs:
        if all(word.startswith("<<") for word in argv[1:]):
            return inputs
    # A pipe stage such as `cat`, `sed` or `tee` may forward or transform
    # earlier output. Its arguments do not describe what the shell will run.
    return [*inputs, _UNKNOWN_SHELL_INPUT]


def _executed_texts(
    seg: _Segment, argv: List[str], prev: Optional[Tuple[_Segment, List[str]]]
) -> List[str]:
    """The text *seg* hands to a shell to run, each a command line of its own."""
    texts = _quoted_substitutions(seg.text)
    if seg.owner is not None:
        owner_argv = _argv(seg.owner.text)
        if owner_argv and os.path.basename(owner_argv[0]) in _SCRIPT_READERS:
            texts.extend(_input_text(seg, argv))  # `bash <(echo 'kill 1')`
    if not argv:
        return texts
    name = os.path.basename(argv[0])
    args = argv[1:]
    reads_stdin = False
    if name in _SHELLS or name == "su":
        script, reads_stdin = _shell_script_arg(argv)
        if name == "su":
            reads_stdin = False
        if script is not None:
            # shlex leaves \$ and \` escaped inside double quotes, while the
            # outer shell removes those escapes before passing a -c script.
            if re.search(r"(?:^|\s)-[A-Za-z]*c[A-Za-z]*\s+\"", seg.text):
                script = script.replace(r"\$", "$").replace(r"\`", "`")
            texts.append(script)
    elif name in ("source", "."):
        reads_stdin = not args or args[0] in ("/dev/stdin", "-", "/proc/self/fd/0")
    elif name in ("eval", "watch"):
        words, idx = args, 0
        while name == "watch" and idx < len(words) and words[idx].startswith("-"):
            idx += 2 if words[idx] in ("-n", "--interval") else 1
        texts.append(" ".join(words[idx:]))
    elif name == "trap" and args:
        texts.append(args[0])
    elif name == "ssh":
        idx = 0
        while idx < len(args) and args[idx].startswith("-"):
            flag = args[idx]
            idx += 2 if len(flag) == 2 and flag[1] in _SSH_VALUE_FLAGS else 1
        if idx + 1 < len(args):
            texts.append(" ".join(args[idx + 1 :]))
        else:
            reads_stdin = True
    elif name == "find":
        for idx, word in enumerate(args):
            if word in ("-exec", "-execdir", "-ok", "-okdir"):
                rest = args[idx + 1 :]
                end = next(
                    (k for k, w in enumerate(rest) if w in (";", "+")), len(rest)
                )
                if rest[:end]:
                    texts.append(shlex.join(rest[:end]))
    if reads_stdin:
        texts.extend(seg.heredocs)
        texts.extend(_herestrings(argv))
        if seg.piped and prev is not None:
            texts.extend(_input_text(*prev))
    return texts


def _walk(
    command: str, depth: int = 0, stripped: Optional[List[str]] = None
) -> List[Tuple[List[str], str]]:
    """``(argv, segment text with literals blanked)`` of every command run.

    Recurses into ``_executed_texts`` up to ``_MAX_NESTING`` levels.
    """
    found: List[Tuple[List[str], str]] = []
    prev: Optional[Tuple[_Segment, List[str]]] = None
    for seg in _segments(command):
        argv = _argv(seg.text, stripped)
        if argv:
            found.append((argv, _mask_literals(seg.text)[0]))
        if depth < _MAX_NESTING:
            for text in _executed_texts(seg, argv, prev):
                found.extend(_walk(text, depth + 1, stripped))
        prev = (seg, argv)
    return found


def _commands(
    command: str, depth: int = 0, stripped: Optional[List[str]] = None
) -> List[List[str]]:
    """Every simple command in *command* a shell runs, including inside
    ``sh -c '...'``, ``bash <<EOF`` and the other executed positions.

    *stripped* collects the wrappers ``_argv`` removed on the way.
    """
    return [argv for argv, _ in _walk(command, depth, stripped)]


# -- command rules vs. literal text (dv-af70, dv-3d18) -----------------------
#
# `kill` inside `python3 -c "... LIKE '%kill %'"` is SQL, not a command. A
# command rule must match at the *start* of a simple command a shell runs
# (after wrappers, `xargs`, a path prefix), with that command's own quoted
# words blanked. Literals given to anything but a shell are data: python,
# cat, sed, grep, `git commit -m`. dv-af70 re-checked every literal as a
# command line and so asked on doc text and grep patterns; dv-3d18 narrowed
# that to the executed positions above.


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
                body_end, after = _heredoc_end(command, i, delimiter, strip_tabs)
                out.append(_blank(command[i:body_end]) + command[body_end:after])
                bodies.append(_heredoc_body(command, i, body_end))
                i = after
            heredocs = []
        else:
            out.append(ch)
            i += 1
    return "".join(out), bodies


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _command_rule_reason(walked: List[Tuple[List[str], str]]) -> Optional[str]:
    """First ``_BASH_RULES`` hit on a command a shell runs (not on literal text)."""
    for original_argv, masked in walked:
        if original_argv and original_argv[0] == _UNKNOWN_SHELL_INPUT:
            return "unresolved script input to shell"
        if _docker_removal(original_argv):
            return "docker resource removal (docker rm/prune)"
        try:
            shlex.split(masked)
        except ValueError:
            # an unterminated quote masks nothing: search it all (fail-safe)
            for pattern, reason in _BASH_RULES:
                if pattern.search(masked):
                    return reason
            continue
        # `env -S 'cmd args'` splits its quoted value into executable words.
        # The normal literal mask hides that value, so use the parsed argv.
        argv = (
            original_argv
            if re.search(r"(?:^|\s)env\s+(?:-S|--split-string(?:=|\s))", masked)
            else _argv(masked)
        )
        if not argv:
            continue
        line = " ".join([os.path.basename(argv[0]), *argv[1:]])
        for pattern, reason in _BASH_RULES:
            if pattern.match(line):
                return reason
    return None


def _docker_removal(argv: List[str]) -> bool:
    """Match Docker's removal subcommand, never a later option or argument."""
    if not argv:
        return False
    name = os.path.basename(argv[0])
    args = argv[1:]
    if name == "docker-compose":
        args = ["compose", *args]
    elif name != "docker":
        return False
    idx = _skip_options(args, 0, _DOCKER_VALUE_OPTIONS)
    if idx >= len(args):
        return False
    if args[idx] in {"rm", "rmi", "prune"}:
        return True
    if args[idx] == "compose":
        # `docker compose -f dev.yml -p proj rm -f svc`
        idx = _skip_options(args, idx + 1, _COMPOSE_VALUE_OPTIONS)
        return idx < len(args) and args[idx] == "rm"
    return (
        args[idx] in _DOCKER_REMOVAL_GROUPS
        and idx + 1 < len(args)
        and args[idx + 1] in {"rm", "rmi", "prune"}
    )


_DOCKER_VALUE_OPTIONS = frozenset(
    {"-H", "--host", "--config", "-c", "--context", "-l", "--log-level"}
)
_COMPOSE_VALUE_OPTIONS = frozenset(
    {
        "-f",
        "--file",
        "-p",
        "--project-name",
        "--project-directory",
        "--env-file",
        "--profile",
        "--ansi",
        "--progress",
        "--parallel",
    }
)
# Management commands whose `rm` / `prune` removes a Docker resource.
_DOCKER_REMOVAL_GROUPS = frozenset(
    {
        "builder",
        "buildx",
        "config",
        "container",
        "context",
        "image",
        "manifest",
        "network",
        "node",
        "plugin",
        "secret",
        "service",
        "stack",
        "system",
        "volume",
    }
)


def _skip_options(args: List[str], idx: int, value_options: frozenset) -> int:
    """Index of the first non-option word from ``idx`` (``--opt=v`` is one word)."""
    while idx < len(args) and args[idx].startswith("-"):
        idx += 2 if args[idx] in value_options else 1
    return idx


def _embedded_rm_reason(command: str) -> Optional[str]:
    """``rm -rf /``, ``~``, ``..`` or a system path anywhere in the raw text."""
    for m in _EMBEDDED_RM.finditer(command):
        target = m.group("target")
        if target in _CATASTROPHIC_RM_TARGETS or (
            target.startswith("/")
            and _under_system_prefix(target)
            and not _is_temp_path(target, Path("/"))
        ):
            return "recursive/forced file deletion (rm)"
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


# -- a pump job's own worktree (dv-58db, dv-3d18) ----------------------------
#
# An unattended pump job runs in a disposable git worktree; its committed work
# is in git. Deleting inside it needs no owner, so the job turn carries the
# worktree as ``free_root`` (``src/model_router/job.py::pump_worktree_root``).
# Chat turns, Codex and non-pump jobs never set it. Fail closed: a target is
# free only when both its lexical and its symlink-resolved path are strictly
# inside the root, outside ``.git``, and spelled without variables, ``~``,
# braces or substitutions; a glob must start below the root and every match
# must pass.
#
# realpath runs before the line does, so ``ln -s /etc x && rm -rf x/y`` would
# slip past it: only commands that run *before* a deletion matter, and only
# when they could create, move or link a path (anything but a companion
# below). Text order is run order only between plain top-level list items
# (``; && || newline``): a substitution runs before the command holding it,
# pipeline members and ``&`` jobs run at the same time, a loop repeats, a
# function runs where it is called. So a deletion keeps the free root when its
# own item is plain and every command before it is a companion; anywhere else
# the old rule holds, every command in the line must be a companion. ``cd``
# and ``NAME=<literal>`` / ``NAME=$(mktemp)`` from earlier plain items are
# followed (dv-3d18) along every way the shell can take: a ``cd`` that fails
# leaves it where it was, so only ``&&`` guarantees the move, and after ``;``,
# a newline or ``||`` a target must be free from every directory it may be
# in. ``sed`` with an ``e`` command and ``git -c`` run code: no companions. ``sudo`` / ``xargs`` / ``env`` on the deletion withhold
# the free root from it.
#
# A job deletion that would be free run alone, but sits after a
# non-companion (``python3 x.py && rm x.py``), is denied at once with
# ``RUN_RM_ALONE`` instead of waiting on the owner (dv-3d18).

_FREE_ROOT_COMPANIONS = {
    "echo",
    "printf",
    "true",
    "false",
    ":",
    "test",
    "[",
    "ls",
    "pwd",
    "cat",
    "wc",
    "head",
    "tail",
    "grep",
    "stat",
    "touch",
    "mkdir",
    "mktemp",
    "cd",
    "sed",
    "sort",
    "uniq",
    "cut",
    "tr",
    "diff",
    "jq",
    "du",
    "df",
    "file",
    "basename",
    "dirname",
    "realpath",
    "readlink",
    "which",
    "date",
    "sleep",
    "id",
    "whoami",
}
_READ_ONLY_GIT = {"status", "log", "diff", "show", "rev-parse", "ls-files", "grep"}
_GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree"}
_GIT_CODE_FLAG = re.compile(r"^(?:-c|--config-env|--exec-path)(?:=|$)|^-c.")
# A sed ``e`` command (after an address, ``;``, ``{`` or line start) or an
# ``s`` command whose flags hold ``e``.
_SED_EXEC = re.compile(
    r"(?:^|[;\n{}0-9$/!,])\s*e(?:\s|;|$)|s([^\w\s\\]).*\1.*\1[gpiImMw0-9]*e"
)
_FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete"}
# Wrappers that change who deletes, what or where: no free root for that rm.
_WITHHOLDING_WRAPPERS = {"sudo", "xargs", "env"}
# Change the shell's directory (``source`` / ``eval`` may, too).
_DIR_CHANGERS = {"cd", "pushd", "popd", "source", ".", "eval"}
# First words of a loop, case, group or function: text order is not run order.
_COMPOUND_WORDS = {
    "for",
    "while",
    "until",
    "select",
    "case",
    "esac",
    "do",
    "done",
    "{",
    "}",
    "function",
    "coproc",
}
_GLOB_CHARS = ("*", "?", "[")
_REDIRECT_WORD = re.compile(r"^(?:\d+|&)?(?:>>?\|?|<<?<?-?|[<>]&)(?P<rest>.*)$")
_ASSIGNED_NAME = re.compile(r"(?:^|[^\w])([A-Za-z_]\w*):?\+?=")
_VARIABLE_TARGET = re.compile(r"^\$(?:\{(\w+)\}|(\w+))(?P<rest>.*)$")
_MKTEMP_ARGS = re.compile(r"^(?:-[dqut]+|[^-/$`\s][^/$`\s]*)$")
_MKTEMP_QUOTED = re.compile(
    r'^([A-Za-z_]\w*)="?(?:\$\((?P<a>[^()`$]*)\)|`(?P<b>[^`$]*)`)"?$'
)
RUN_RM_ALONE = (
    "file deletion after another command: run rm alone, as its own command "
    "with a literal path. Inside this job's worktree (or a temp dir) it then "
    "needs no approval; chained after an interpreter, ln, mv or cp it cannot "
    "be checked."
)


def _strictly_inside(path: str, root: str) -> bool:
    return path.startswith(root + "/")


def _free_target(target: str, cwd: Path, root: Path) -> bool:
    """True when deleting *target* cannot touch anything but the job's tree."""
    if not target or any(ch in target for ch in "$`{~"):
        return False
    path = target if os.path.isabs(target) else os.path.join(str(cwd), target)
    path = os.path.normpath(path)
    lexical_root = os.path.normpath(str(root))
    real_root = os.path.realpath(str(root))
    if not _strictly_inside(path, lexical_root):
        return False
    if os.path.relpath(path, lexical_root).split("/")[0] == ".git":
        return False
    real = os.path.realpath(path)
    if not _strictly_inside(real, real_root):
        return False
    return os.path.relpath(real, real_root).split("/")[0] != ".git"


def _is_free_path(target: str, cwd: Path, free_root: Optional[Path] = None) -> bool:
    """A temp path (always), or a path inside *free_root* (a pump job's tree)."""
    if _is_temp_path(target, cwd):
        return True
    if free_root is None:
        return False
    if not any(ch in target for ch in _GLOB_CHARS):
        return _free_target(target, cwd, free_root)
    # The glob's literal directory must already be below the root, so `*`
    # or `./*` at the root (the whole tree) keeps asking.
    cut = min(target.index(ch) for ch in _GLOB_CHARS if ch in target)
    prefix = target[:cut].rpartition("/")[0]
    if not prefix or not _free_target(prefix, cwd, free_root):
        return False
    matches = glob.glob(target, root_dir=str(cwd))
    return all(_free_target(m, cwd, free_root) for m in matches)


def _sed_runs_code(args: List[str]) -> bool:
    """GNU ``sed`` runs a shell via the ``e`` command or the ``s///e`` flag.

    Conservative: a script file (``-f``) or anything that looks like either
    counts (dv-3d18 review).
    """
    scripts: List[str] = []
    take = False
    for word in args:
        if take:
            scripts.append(word)
            take = False
        elif word in ("-e", "--expression"):
            take = True
        elif word.startswith("--expression="):
            scripts.append(word.split("=", 1)[1])
        elif word in ("-f", "--file") or word.startswith(("--file=", "-f")):
            return True
        elif word.startswith("-") and not word.startswith("--") and "e" in word:
            return True  # `-ne 'script'` and the like: be safe
    if not scripts:
        scripts = [w for w in args if not w.startswith("-")][:1]
    return any(_SED_EXEC.search(script) for script in scripts)


def _companion(argv: List[str]) -> bool:
    """True when *argv* cannot create, move or link a path (nor run code)."""
    name = os.path.basename(argv[0])
    if name == "sed":
        return not _sed_runs_code(argv[1:])
    if name in _DELETE_COMMANDS or name in _FREE_ROOT_COMPANIONS:
        return True
    if name == "find":
        return not any(word in _FIND_ACTIONS for word in argv)
    if name == "git":
        args, idx = argv[1:], 0
        while idx < len(args) and args[idx].startswith("-"):
            if _GIT_CODE_FLAG.match(args[idx]):
                return False  # `-c core.fsmonitor=...` / `core.pager` run code
            idx += 2 if args[idx] in _GIT_VALUE_FLAGS else 1
        rest = args[idx:]
        if rest and rest[0] in _READ_ONLY_GIT:
            return True
        return rest == ["branch", "--show-current"]
    return False


@dataclass
class _Run:
    """One command a shell runs, with where it sits in the line."""

    argv: List[str]
    wrappers: List[str]
    seg: _Segment
    # The one command of a plain top-level list item (not nested in a shell).
    plain: bool


def _runs(command: str) -> Tuple[List[_Run], List[_Segment]]:
    """Every command of *command* in run order, and its top-level segments."""
    segs = _segments(command)
    runs: List[_Run] = []
    prev: Optional[Tuple[_Segment, List[str]]] = None
    for seg in segs:
        wrappers: List[str] = []
        argv = _argv(seg.text, wrappers)
        if argv:
            runs.append(_Run(argv, wrappers, seg, seg.plain))
        for text in _executed_texts(seg, argv, prev):
            inner: List[str] = []
            for nested in _commands(text, 1, inner):
                runs.append(_Run(nested, inner or ["sh"], seg, False))
        prev = (seg, argv)
    return runs, segs


def _delete_targets(args: List[str]) -> List[str]:
    """The paths an ``rm`` / ``unlink`` / ``rmdir`` argv names (no redirects)."""
    targets: List[str] = []
    skip = False
    for word in args:
        if skip:
            skip = False
            continue
        redirect = _REDIRECT_WORD.match(word)
        if redirect:
            skip = not redirect.group("rest")
            continue
        if not word.startswith("-"):
            targets.append(word)
    return targets


def _expand(target: str, variables: Dict[str, str]) -> str:
    """*target* with a leading ``$NAME`` / ``${NAME}`` the line set literally."""
    m = _VARIABLE_TARGET.match(target)
    if not m:
        return target
    value = variables.get(m.group(1) or m.group(2) or "")
    rest = m.group("rest")
    if value is None or "$" in rest or "`" in rest:
        return target
    return value + rest


def _mktemp_assignment(item: List[_Segment]) -> Optional[str]:
    """``NAME`` of ``NAME=$(mktemp ...)`` (a path in a temp dir), else None."""
    name: Optional[str] = None
    words = ""
    if len(item) == 1:
        m = _MKTEMP_QUOTED.match(item[0].text)
        if m:
            name, words = m.group(1), m.group("a") or m.group("b") or ""
    elif len(item) == 2:
        m = re.fullmatch(r"([A-Za-z_]\w*)=\$?", item[0].text)
        if m:
            name, words = m.group(1), item[1].text
    try:
        argv = shlex.split(words)
    except ValueError:
        return None
    if name is None or not argv or argv[0] != "mktemp":
        return None
    if not all(_MKTEMP_ARGS.match(word) for word in argv[1:]):
        return None  # `-p DIR`, `--tmpdir`, a template with a directory
    return name


def _free_at(target: str, where: Optional[str], root: Optional[Path]) -> bool:
    if where is None and not target.startswith("/"):
        return False
    return _is_free_path(target, Path(where or "/"), root)


# One way the shell can reach a list item: its directory (None: unknown),
# whether the item before it succeeded, and the variables set on the way.
_Way = Tuple[Optional[str], bool, FrozenSet[Tuple[str, str]]]
_MAX_WAYS = 32


def _runs_after(joiner: str, ok: bool) -> bool:
    """Whether an item after *joiner* runs when the one before it was *ok*."""
    if joiner == "&&":
        return ok
    if joiner == "||":
        return not ok
    return True


def _free_in(
    target: str, wheres: Optional[Tuple[str, ...]], root: Optional[Path]
) -> bool:
    """*target* is free from every directory the shell may be in."""
    if wheres is None:
        return _free_at(target, None, root)
    return bool(wheres) and all(_free_at(target, w, root) for w in wheres)


def _deletion_reason(
    command: str, cwd: Path, free_root: Optional[Path] = None
) -> Optional[str]:
    """Deleting anything outside the temp dirs needs the owner (dv-1601).

    With *free_root* (a pump job), deleting inside it needs no owner either
    (dv-58db), under the order rule above. ``RUN_RM_ALONE`` when the only
    reason to ask is a command before the deletion (job turns only).
    """
    runs, segs = _runs(command)
    ordered = not any(
        seg.unordered or (seg.text.split() or [""])[0] in _COMPOUND_WORDS
        for seg in segs
    )
    whole_line_ok = all(_companion(r.argv) for r in runs) and not any(
        r.wrappers for r in runs
    )
    items: Dict[int, List[_Segment]] = {}
    for seg in segs:
        items.setdefault(seg.item, []).append(seg)
    moves = {r.seg.item for r in runs if os.path.basename(r.argv[0]) in _DIR_CHANGERS}
    start: Optional[str] = str(cwd)
    if not ordered and moves:
        start = None
    ways: Set[_Way] = {(start, True, frozenset())}
    history_ok = True  # every command so far is a companion
    followed = False  # a `cd` moved the shell away from the turn's cwd
    reason: Optional[str] = None
    run_alone = False
    by_item: Dict[int, List[_Run]] = {}
    for run in runs:
        by_item.setdefault(run.seg.item, []).append(run)
    joiner = ""  # the list operator before this item
    for index in sorted(items):
        item = items[index]
        running = {way for way in ways if _runs_after(joiner, way[1])}
        dirs = {way[0] for way in running}
        here: Optional[Tuple[str, ...]] = None
        if None not in dirs:
            here = tuple(sorted(d for d in dirs if d is not None))
        # A name is known only when every way into this item set it alike.
        variables: Dict[str, str] = (
            dict(frozenset.intersection(*(way[2] for way in running)))
            if running
            else {}
        )
        cd_target: Optional[str] = None
        lost_dir = False
        forget = False
        if ordered and history_ok:
            plain_assign = len(item) == 1 and item[0].plain
            mktemp = _mktemp_assignment(item)
            for seg in item:
                for name in _ASSIGNED_NAME.findall(seg.text):
                    variables.pop(name, None)
            if mktemp:
                variables[mktemp] = os.path.join(sorted(_temp_roots())[0], "mktemp.d")
            elif plain_assign and not by_item.get(index):
                try:
                    words = shlex.split(item[0].text)
                except ValueError:
                    words = []
                for word in words:
                    key, _, value = word.partition("=")
                    if _ENV_ASSIGNMENT.match(word) and not any(
                        ch in value for ch in "$`"
                    ):
                        variables[key] = value
        for run in by_item.get(index, []):
            argv = run.argv
            name = os.path.basename(argv[0])
            in_order = ordered and run.plain
            if name in _DELETE_COMMANDS:
                withheld = bool(set(run.wrappers) & _WITHHOLDING_WRAPPERS)
                if (in_order and history_ok and not withheld) or whole_line_ok:
                    root = free_root
                else:
                    root = None
                where = here if in_order or index not in moves else None
                alone_at = where
                if followed and not history_ok:
                    # Temp paths get no realpath check: `cd /tmp && ln -s ~ d
                    # && rm -rf d/x` must not resolve to a temp path.
                    where = None
                known = variables if in_order and history_ok else {}
                targets = [_expand(t, known) for t in _delete_targets(argv[1:])]
                if targets and all(_free_in(t, where, root) for t in targets):
                    pass
                elif (
                    free_root is not None
                    and not withheld
                    and targets
                    and all(_free_in(t, alone_at, free_root) for t in targets)
                ):
                    run_alone = True
                elif reason is None:
                    flags = [w for w in argv[1:] if w.startswith("-") and w != "--"]
                    forced = any(set(f.lstrip("-")) & {"r", "R", "f"} for f in flags)
                    if name == "rm" and forced:
                        reason = "recursive/forced file deletion (rm)"
                    else:
                        reason = f"file deletion ({name})"
            elif name == "find" and reason is None:
                if "-delete" in argv:
                    reason = "file deletion (find -delete)"
                for idx, word in enumerate(argv):
                    if word in ("-exec", "-execdir", "-ok") and idx + 1 < len(argv):
                        if os.path.basename(argv[idx + 1]) in _DELETE_COMMANDS:
                            reason = reason or "file deletion (find -exec rm)"
            # What this command leaves for the ones after it.
            if name in _DIR_CHANGERS:
                if in_order and history_ok and name == "cd" and len(argv) <= 2:
                    cd_target = argv[1] if len(argv) > 1 else "~"
                    followed = True
                else:
                    lost_dir = True
            elif not _companion(argv):
                # It may link or move a path, or set a variable; it cannot
                # change this shell's directory (that is ``_DIR_CHANGERS``).
                history_ok = False
                forget = True
            elif name == "printf" and "-v" in argv:
                forget = True
        # Where each way goes on: a `cd` that fails leaves the shell where it
        # was, so after `;`, a newline or `||` the next item may run in either
        # directory, and its targets must be free in both (dv-3d18 review).
        after = ways - running
        kept = frozenset() if forget else frozenset(variables.items())
        for old, _ok, _vars in running:
            new = old
            if lost_dir:
                old = new = None
            elif cd_target is not None:
                resolved = None if cd_target == "-" else _resolve_cd(cd_target, old)
                new = (
                    os.path.normpath(os.path.expanduser(resolved)) if resolved else None
                )
            after.add((new, True, kept))
            after.add((old, False, kept))
        if forget:
            after = {(d, ok, frozenset()) for d, ok, _vars in after}
        if len(after) > _MAX_WAYS:
            after = {(None, ok, frozenset()) for _d, ok, _vars in after}
        ways = after
        joiner = next((seg.joiner for seg in reversed(item) if seg.joiner), ";")
    if reason:
        return reason
    return RUN_RM_ALONE if run_alone else None


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
            # dv-b05e: every write to a repo's releases is public.
            if args[:1] == ["release"] and args[1:2] and args[1] in _GH_RELEASE_WRITES:
                return f"GitHub release (gh release {args[1]})"
    return None


# dv-b05e: these push to the public repo from a subprocess the egress rule
# never sees. Publishing is the owner's button, never a turn's. Matched on
# parsed commands, so `sh -c '…'` asks and a quoted mention
# (`git commit -m "release publish"`) does not; `release check` and
# `release later` push nothing and stay free.
_PUBLISH_SCRIPTS = {
    "release_public.py": "release_public.py publish",
    "release_public": "release_public.py publish",
    "release": "scripts/release publish",
}
# Runners that take the script as an argument: `python3 scripts/release
# publish` and `bash scripts/release publish` run the same push.
_SCRIPT_RUNNERS = {"poetry", "uv", "pipenv", "sh", "bash", "dash", "zsh"}


def _publish_reason(commands: List[List[str]]) -> Optional[str]:
    for argv in commands:
        runner = os.path.basename(argv[0])
        if runner in _PUBLISH_SCRIPTS:
            start = 0
        elif runner in _SCRIPT_RUNNERS or runner.startswith("python"):
            start = 1
        else:
            continue
        for idx in range(start, len(argv) - 1):
            what = _PUBLISH_SCRIPTS.get(os.path.basename(argv[idx]))
            if what and argv[idx + 1] == "publish":
                return f"publishing to the public repo ({what})"
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
    free_root: Optional[Path] = None,
) -> Optional[str]:
    """Return a reason string if the op needs confirmation, else None.

    *free_root*: a pump job's own worktree, where deleting needs no owner
    (dv-58db). ``None`` everywhere else.
    """
    if tool_name in _BASH_TOOLS:
        command = tool_input.get("command", "") or ""
        walked = _walk(command)
        commands = [argv for argv, _ in walked]
        rule_reason = _command_rule_reason(walked)
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
        # explicit owner OK. The pump marks it; this is the button. Checked
        # per command, so `pump/<id>` in doc text or another command is not
        # a merge (dv-3d18).
        for argv in commands:
            merge_reason = privileged_merge_reason(" ".join(argv), working_directory)
            if merge_reason:
                return merge_reason
        deletion = _deletion_reason(command, working_directory, free_root)
        parsed_reason = (
            (None if deletion == RUN_RM_ALONE else deletion)
            or _egress_reason(commands)
            or _publish_reason(commands)
            or _embedded_rm_reason(command)
        )
        if parsed_reason:
            return parsed_reason
        # Redirect into a system path (e.g. `echo x > /etc/hosts`), in a
        # command a shell runs. Discarding a stream into /dev/null (or the
        # equivalent stdout/stderr/stdin pseudo-files) is exempt — there's
        # nothing there to damage.
        for _, masked in walked:
            for m in _REDIRECT_RE.finditer(masked):
                path = m.group("path")
                if _is_safe_redirect_target(path):
                    continue
                if _under_system_prefix(path):
                    return f"redirect into system path ({path})"
        # Lowest priority: any real ask above goes to the owner instead.
        return deletion

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
