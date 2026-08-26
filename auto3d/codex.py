"""Thin wrapper around `codex exec` (non-interactive Codex CLI).

Every call streams the `--json` event log to a file, keeps the final agent message, and parses
it as JSON when a `--output-schema` was supplied. Sessions are resumable through the thread id
reported by the `thread.started` event, which is how the review loop keeps one conversation
across render/review turns.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .util import Auto3DError, REPO_ROOT, debug, extract_first_json, log, write_text


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def generated_images_dir() -> Path:
    return codex_home() / "generated_images"


@dataclass
class CodexResult:
    returncode: int
    thread_id: str | None
    last_message: str
    structured: Any
    usage: dict[str, int]
    duration: float
    timed_out: bool
    events_path: Path | None
    commands: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    agent_messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def codex_version(settings: Settings) -> str | None:
    try:
        completed = subprocess.run([settings.codex_bin, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or completed.stderr).strip()


def login_status(settings: Settings) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [settings.codex_bin, "login", "status"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run codex login status: {exc}"
    text = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0 and "not logged in" not in text.lower(), text


def config_overrides(settings: Settings, *, sandbox: str | None = None, network: bool | None = None) -> list[str]:
    """`-c key=value` overrides shared by new and resumed sessions."""
    mode = sandbox or settings.sandbox
    overrides = [
        'approval_policy="never"',
        f'sandbox_mode="{mode}"',
    ]
    allow_network = settings.network_in_sandbox if network is None else network
    overrides.append(f"sandbox_workspace_write.network_access={'true' if allow_network else 'false'}")
    if settings.reasoning_effort:
        overrides.append(f'model_reasoning_effort="{settings.reasoning_effort}"')
    return overrides


def build_command(
    settings: Settings,
    *,
    resume_thread: str | None = None,
    images: list[Path] | None = None,
    output_schema: Path | None = None,
    last_message: Path | None = None,
    sandbox: str | None = None,
    network: bool | None = None,
    cwd: Path | None = None,
    ephemeral: bool = False,
    model: str | None = None,
    extra_config: list[str] | None = None,
) -> list[str]:
    command: list[str] = [settings.codex_bin, "exec"]
    if resume_thread:
        command += ["resume", resume_thread]
    command.append("--json")
    command.append("--skip-git-repo-check")
    if ephemeral:
        command.append("--ephemeral")
    for override in config_overrides(settings, sandbox=sandbox, network=network) + list(extra_config or []):
        command += ["-c", override]
    chosen_model = model or settings.model
    if chosen_model:
        command += ["-m", chosen_model]
    if not resume_thread:
        command += ["--sandbox", sandbox or settings.sandbox]
        command += ["-C", str(cwd or REPO_ROOT)]
    for image in images or []:
        command += ["-i", str(image)]
    if output_schema is not None:
        command += ["--output-schema", str(output_schema)]
    if last_message is not None:
        command += ["-o", str(last_message)]
    command.append("-")  # prompt on stdin
    return command


def _summarize_item(item: dict[str, Any]) -> str | None:
    kind = item.get("type")
    if kind == "command_execution":
        command = str(item.get("command") or "")
        exit_code = item.get("exit_code")
        status = item.get("status")
        short = command if len(command) <= 140 else command[:137] + "…"
        tail = f" → exit {exit_code}" if exit_code is not None else f" ({status})" if status else ""
        return f"$ {short}{tail}"
    if kind == "agent_message":
        text = str(item.get("text") or "").strip().replace("\n", " ")
        return f"msg: {text[:200]}{'…' if len(text) > 200 else ''}"
    if kind == "file_change":
        changes = item.get("changes") or []
        paths = [str(change.get("path")) for change in changes if isinstance(change, dict)]
        return f"files: {', '.join(paths[:6])}{' …' if len(paths) > 6 else ''}"
    if kind == "reasoning":
        return None
    if kind == "error":
        return f"error: {item.get('message')}"
    if kind in {"mcp_tool_call", "web_search", "todo_list"}:
        return f"{kind}"
    return f"{kind}"


def run_codex(
    settings: Settings,
    prompt: str,
    *,
    label: str,
    events_path: Path,
    last_message_path: Path,
    resume_thread: str | None = None,
    images: list[Path] | None = None,
    output_schema: Path | None = None,
    sandbox: str | None = None,
    network: bool | None = None,
    cwd: Path | None = None,
    ephemeral: bool = False,
    model: str | None = None,
    timeout_s: float = 3600,
    extra_config: list[str] | None = None,
    prompt_path: Path | None = None,
) -> CodexResult:
    """Run one Codex turn. The prompt goes over stdin, events stream to `events_path`."""
    command = build_command(
        settings,
        resume_thread=resume_thread,
        images=images,
        output_schema=output_schema,
        last_message=last_message_path,
        sandbox=sandbox,
        network=network,
        cwd=cwd,
        ephemeral=ephemeral,
        model=model,
        extra_config=extra_config,
    )
    if prompt_path is not None:
        write_text(prompt_path, prompt)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    if last_message_path.exists():
        last_message_path.unlink()
    debug("$ " + " ".join(shlex.quote(part) for part in command))
    log(f"codex[{label}] starting{' (resume ' + resume_thread[:8] + '…)' if resume_thread else ''}")

    started = time.monotonic()
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd or REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except FileNotFoundError as exc:
        raise Auto3DError(f"codex binary not found: {settings.codex_bin!r} — install with `npm i -g @openai/codex`") from exc

    thread_id: str | None = None
    usage: dict[str, int] = {}
    commands: list[dict[str, Any]] = []
    errors: list[str] = []
    agent_messages: list[str] = []
    stderr_lines: list[str] = []
    lock = threading.Lock()

    def reader() -> None:
        nonlocal thread_id
        assert process.stdout is not None
        with events_path.open("a", encoding="utf-8") as sink:
            for raw in process.stdout:
                line = raw.rstrip("\n")
                sink.write(line + "\n")
                sink.flush()
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    debug(f"codex[{label}] {line[:200]}")
                    continue
                kind = event.get("type")
                with lock:
                    if kind == "thread.started":
                        thread_id = event.get("thread_id") or thread_id
                    elif kind == "turn.completed":
                        for key, value in (event.get("usage") or {}).items():
                            if isinstance(value, int):
                                usage[key] = usage.get(key, 0) + value
                    elif kind in {"turn.failed", "error"}:
                        message = event.get("error", {}).get("message") if isinstance(event.get("error"), dict) else event.get("message")
                        errors.append(str(message or event))
                        log(f"codex[{label}] {message}", level="warn")
                    elif kind == "item.completed":
                        item = event.get("item") or {}
                        if item.get("type") == "command_execution":
                            commands.append(
                                {
                                    "command": item.get("command"),
                                    "exit_code": item.get("exit_code"),
                                    "status": item.get("status"),
                                }
                            )
                        elif item.get("type") == "agent_message":
                            agent_messages.append(str(item.get("text") or ""))
                        elif item.get("type") == "error":
                            errors.append(str(item.get("message")))
                        summary = _summarize_item(item)
                        if summary:
                            log(f"codex[{label}] {summary}", level="debug" if summary.startswith("$") and not _is_interesting(summary) else "info")

    def stderr_reader() -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            stderr_lines.append(raw.rstrip("\n"))

    threads = [threading.Thread(target=reader, daemon=True), threading.Thread(target=stderr_reader, daemon=True)]
    for thread in threads:
        thread.start()
    assert process.stdin is not None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except BrokenPipeError:
        pass

    timed_out = False
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        log(f"codex[{label}] timed out after {int(timeout_s)}s — terminating", level="warn")
        process.kill()
        process.wait()
    for thread in threads:
        thread.join(timeout=10)
    for stream in (process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass

    duration = time.monotonic() - started
    last_message = ""
    if last_message_path.is_file():
        last_message = last_message_path.read_text(encoding="utf-8", errors="replace")
    elif agent_messages:
        last_message = agent_messages[-1]
    structured = extract_first_json(last_message) if last_message.strip() else None
    if output_schema is not None and structured is None and last_message.strip():
        log(f"codex[{label}] final message was not JSON; keeping raw text", level="warn")
    if process.returncode != 0 and not timed_out:
        tail = "\n".join(stderr_lines[-15:])
        errors.append(f"codex exited with {process.returncode}: {tail.strip()[-1500:]}")
        log(f"codex[{label}] exited with {process.returncode}", level="warn")
        if tail.strip():
            log(tail.strip()[-600:], level="warn")
    log(
        f"codex[{label}] finished in {int(duration)}s"
        + (f" · tokens in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}" if usage else "")
    )
    return CodexResult(
        returncode=process.returncode if not timed_out else 124,
        thread_id=thread_id,
        last_message=last_message,
        structured=structured,
        usage=usage,
        duration=duration,
        timed_out=timed_out,
        events_path=events_path,
        commands=commands,
        errors=errors,
        agent_messages=agent_messages,
    )


_INTERESTING = ("forge/", "auto3d", "state.py", "next.py", "generate_threejs_factory", "append_review", "cp ", "mv ")


def _is_interesting(summary: str) -> bool:
    return any(token in summary for token in _INTERESTING)
