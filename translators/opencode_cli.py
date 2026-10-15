"""OpenCode CLI adapter for models configured in the user's OpenCode account."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .prompt import system_instructions, user_prompt

TIMEOUT = 120
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*\Z")
AGENT_NAME = "harmony-translator"


class OpenCodeCLIError(RuntimeError):
    """OpenCode CLI is missing, failed, or returned an invalid response."""


def find_executable() -> str | None:
    executable = shutil.which("opencode")
    if executable:
        return executable

    local_app_data = Path(
        os.environ.get(
            "LOCALAPPDATA",
            str(Path.home() / "AppData" / "Local"),
        )
    )
    bundled = (
        local_app_data
        / "Programs"
        / "@opencodedesktop"
        / "resources"
        / "opencode-cli.exe"
    )
    return str(bundled) if bundled.is_file() else None


def _executable() -> str:
    executable = find_executable()
    if not executable:
        raise OpenCodeCLIError(
            "OpenCode CLI was not found. Install OpenCode and ensure "
            "`opencode` is available on PATH."
        )
    return executable


def list_models(timeout: int = 30) -> list[str]:
    """Return configured provider/model ids reported by `opencode models`."""
    executable = _executable()
    try:
        result = subprocess.run(
            [executable, "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise OpenCodeCLIError("Timed out while listing OpenCode models") from error
    except OSError as error:
        raise OpenCodeCLIError(f"Could not start OpenCode CLI: {error}") from error

    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise OpenCodeCLIError(
            f"`opencode models` failed ({result.returncode}): "
            f"{detail[:300] or 'no error details'}"
        )

    models = list(dict.fromkeys(
        line.strip()
        for line in result.stdout.splitlines()
        if MODEL_ID.fullmatch(line.strip()) and "/" in line
    ))
    if not models:
        raise OpenCodeCLIError(
            "`opencode models` returned no provider/model ids. "
            "Configure a provider in OpenCode, then refresh the list."
        )
    return models


def _translation_output(stdout: str) -> str:
    text_parts: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise OpenCodeCLIError(
                "OpenCode CLI returned a non-JSON event with JSON output enabled"
            ) from error
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text = part["text"].strip()
            if text:
                text_parts.append(text)
    result = "\n".join(text_parts).strip()
    if not result:
        raise OpenCodeCLIError("OpenCode CLI returned no translation text")
    return result


def translate(
    text: str,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    timeout: int = TIMEOUT,
) -> str:
    """Translate with OpenCode's configured credentials and a tools-disabled agent."""
    if not MODEL_ID.fullmatch(model) or "/" not in model:
        raise ValueError("OpenCode model must use provider/model format")

    executable = _executable()
    with tempfile.TemporaryDirectory(prefix="harmony-translator-opencode-") as temp:
        project = Path(temp)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "agent": {
                AGENT_NAME: {
                    "mode": "primary",
                    "description": "Translate text without using tools",
                    "prompt": system_instructions(
                        instructions, source_language, target_language
                    ),
                    "permission": {
                        "read": "deny",
                        "edit": "deny",
                        "glob": "deny",
                        "grep": "deny",
                        "list": "deny",
                        "bash": "deny",
                        "task": "deny",
                        "external_directory": "deny",
                        "todowrite": "deny",
                        "webfetch": "deny",
                        "websearch": "deny",
                        "lsp": "deny",
                        "skill": "deny",
                        "question": "deny",
                        "doom_loop": "deny",
                    },
                    "tools": {"*": False},
                    "temperature": 0.3,
                },
            },
        }
        (project / "opencode.json").write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        command = [
            executable,
            "run",
            "--model",
            model,
            "--agent",
            AGENT_NAME,
            "--format",
            "json",
        ]
        try:
            result = subprocess.run(
                command,
                input=user_prompt(text, source_language, target_language),
                capture_output=True,
                cwd=project,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise OpenCodeCLIError(
                f"OpenCode CLI timed out after {timeout} seconds"
            ) from error
        except OSError as error:
            raise OpenCodeCLIError(f"Could not start OpenCode CLI: {error}") from error

    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise OpenCodeCLIError(
            f"`opencode run` failed ({result.returncode}): "
            f"{detail[:300] or 'no error details'}"
        )
    return _translation_output(result.stdout)


def stream_translate(
    text: str,
    model: str,
    instructions: str = "",
    source_language: str = "auto",
    target_language: str = "ar",
    timeout: int = TIMEOUT,
):
    """Yield text events from OpenCode CLI's line-oriented JSON output."""
    if not MODEL_ID.fullmatch(model) or "/" not in model:
        raise ValueError("OpenCode model must use provider/model format")

    executable = _executable()
    with tempfile.TemporaryDirectory(prefix="harmony-translator-opencode-") as temp:
        project = Path(temp)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "agent": {
                AGENT_NAME: {
                    "mode": "primary",
                    "description": "Translate text without using tools",
                    "prompt": system_instructions(
                        instructions, source_language, target_language),
                    "permission": {
                        "read": "deny", "edit": "deny", "glob": "deny",
                        "grep": "deny", "list": "deny", "bash": "deny",
                        "task": "deny", "external_directory": "deny",
                        "todowrite": "deny", "webfetch": "deny",
                        "websearch": "deny", "lsp": "deny", "skill": "deny",
                        "question": "deny", "doom_loop": "deny",
                    },
                    "tools": {"*": False},
                    "temperature": 0.3,
                },
            },
        }
        (project / "opencode.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8")
        command = [
            executable, "run", "--model", model, "--agent", AGENT_NAME,
            "--format", "json",
        ]
        received_text = False
        timed_out = threading.Event()
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    cwd=project,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError as error:
                raise OpenCodeCLIError(
                    f"Could not start OpenCode CLI: {error}") from error

            def stop_after_timeout() -> None:
                timed_out.set()
                try:
                    process.kill()
                except OSError:
                    pass

            timer = threading.Timer(
                timeout, stop_after_timeout,
            )
            timer.start()
            try:
                assert process.stdin is not None
                process.stdin.write(user_prompt(
                    text, source_language, target_language))
                process.stdin.close()
                assert process.stdout is not None
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise OpenCodeCLIError(
                            "OpenCode CLI returned a non-JSON event with "
                            "JSON output enabled") from error
                    if not isinstance(event, dict) or event.get("type") != "text":
                        continue
                    part = event.get("part")
                    if isinstance(part, dict) and isinstance(
                            part.get("text"), str):
                        delta = part["text"]
                        if delta:
                            received_text = True
                            yield delta
                return_code = process.wait()
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()

            if timed_out.is_set():
                raise OpenCodeCLIError(
                    f"OpenCode CLI timed out after {timeout} seconds")
            if return_code:
                stderr.seek(0)
                detail = stderr.read().strip()
                raise OpenCodeCLIError(
                    f"`opencode run` failed ({return_code}): "
                    f"{detail[:300] or 'no error details'}")
            if not received_text:
                raise OpenCodeCLIError(
                    "OpenCode CLI returned no translation text")


def chat(
    messages: list[dict[str, str]],
    model: str,
    system_prompt: str,
    timeout: int = TIMEOUT,
) -> str:
    """Continue a conversation using OpenCode's configured model and tools-disabled agent."""
    if not MODEL_ID.fullmatch(model) or "/" not in model:
        raise ValueError("OpenCode model must use provider/model format")
    if not messages or messages[-1].get("role") != "user":
        raise ValueError("A user message is required to ask the model.")

    dialogue = "\n\n".join(
        f"{'Student' if item['role'] == 'user' else 'Tutor'}:\n{item['content']}"
        for item in messages
    )
    executable = _executable()
    with tempfile.TemporaryDirectory(prefix="harmony-translator-opencode-") as temp:
        project = Path(temp)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "agent": {
                AGENT_NAME: {
                    "mode": "primary",
                    "description": "Tutor in a conversation without using tools",
                    "prompt": system_prompt,
                    "permission": {
                        "read": "deny",
                        "edit": "deny",
                        "glob": "deny",
                        "grep": "deny",
                        "list": "deny",
                        "bash": "deny",
                        "task": "deny",
                        "external_directory": "deny",
                        "todowrite": "deny",
                        "webfetch": "deny",
                        "websearch": "deny",
                        "lsp": "deny",
                        "skill": "deny",
                        "question": "deny",
                        "doom_loop": "deny",
                    },
                    "tools": {"*": False},
                    "temperature": 0.3,
                },
            },
        }
        (project / "opencode.json").write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                [
                    executable, "run", "--model", model,
                    "--agent", AGENT_NAME, "--format", "json",
                ],
                input=dialogue,
                capture_output=True,
                cwd=project,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise OpenCodeCLIError(
                f"OpenCode CLI timed out after {timeout} seconds"
            ) from error
        except OSError as error:
            raise OpenCodeCLIError(f"Could not start OpenCode CLI: {error}") from error

    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise OpenCodeCLIError(
            f"`opencode run` failed ({result.returncode}): "
            f"{detail[:300] or 'no error details'}"
        )
    return _translation_output(result.stdout)
