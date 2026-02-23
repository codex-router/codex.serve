import os
import asyncio
import json
import codecs
import base64
from pathlib import Path
from typing import List, Optional, Dict
from uuid import uuid4
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()


class ContextFileItem(BaseModel):
    """A file item to include as context in an agent run.

    Provide either ``content`` (plain text) or ``base64Content`` (base64-encoded
    bytes, useful for binary or non-UTF-8 files).  If both are supplied,
    ``base64Content`` takes precedence.
    """
    path: str
    content: Optional[str] = None
    base64Content: Optional[str] = None


class RunRequest(BaseModel):
    agent: str
    args: List[str]
    stdin: str
    env: Optional[Dict[str, str]] = None
    sessionId: Optional[str] = None
    contextFiles: Optional[List[ContextFileItem]] = None

class RunResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class InsightFileResult(BaseModel):
    path: str
    content: str


class InsightRunRequest(BaseModel):
    repoPath: str
    outPath: str
    include: Optional[List[str]] = None
    exclude: Optional[List[str]] = None
    maxFilesPerModule: Optional[int] = None
    maxCharsPerFile: Optional[int] = None
    dryRun: bool = False
    env: Optional[Dict[str, str]] = None


class InsightRunResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    outputDir: str
    files: List[InsightFileResult]
    count: int

# Supported agent providers, configurable via env (comma-separated)
DEFAULT_AGENT_LIST = ["codex"]
AGENT_LIST = [
    agent.strip()
    for agent in os.environ.get("AGENT_LIST", ",".join(DEFAULT_AGENT_LIST)).split(",")
    if agent.strip()
]

# Optional Docker configuration
DOCKER_IMAGE = os.environ.get("CODEX_AGENT_IMAGE")
INSIGHT_DOCKER_IMAGE = os.environ.get("CODEX_INSIGHT_IMAGE", "craftslab/codex-insight:latest")

DEFAULT_MODEL_LIST = []
MODEL_LIST = [
    model.strip()
    for model in os.environ.get("MODEL_LIST", ",".join(DEFAULT_MODEL_LIST)).split(",")
    if model.strip()
]

RUN_SESSIONS: Dict[str, asyncio.subprocess.Process] = {}
STOP_REQUESTED_SESSIONS = set()
SESSIONS_LOCK = asyncio.Lock()


def _parse_response_timeout_seconds(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        timeout_seconds = float(normalized)
    except ValueError:
        return None
    if timeout_seconds <= 0:
        return None
    return timeout_seconds


RESPONSE_TIMEOUT_SECONDS = _parse_response_timeout_seconds(
    os.environ.get("RUN_RESPONSE_TIMEOUT_SECONDS")
)

INSIGHT_RESPONSE_TIMEOUT_SECONDS = _parse_response_timeout_seconds(
    os.environ.get("INSIGHT_RESPONSE_TIMEOUT_SECONDS")
)

MAX_CONTEXT_FILES = 20
MAX_CONTEXT_FILE_CHARS = 12_000
MAX_INSIGHT_FILES = 200
MAX_INSIGHT_FILE_CHARS = 200_000


def _is_default_codex_insight_image(image: str) -> bool:
    normalized = (image or "").strip().lower()
    return normalized in {"craftslab/codex-insight:latest", "codex-insight:latest"}


def _resolve_context_file_content(item: ContextFileItem) -> Optional[str]:
    """Return the text content for a ContextFileItem.

    Decodes ``base64Content`` when present (falling back to UTF-8 with
    replacement characters for binary files); otherwise returns ``content``.
    Returns ``None`` when the item carries no usable content.
    """
    if item.base64Content:
        try:
            raw_bytes = base64.b64decode(item.base64Content)
            return raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            return None
    if item.content is not None:
        return item.content
    return None


def _build_stdin_with_context(stdin: str, context_files: Optional[List[ContextFileItem]]) -> str:
    prompt_text = stdin or ""
    if not context_files:
        return prompt_text

    lines = [
        prompt_text.rstrip("\n"),
        "",
        "Execution note:",
        "- Inline file contents below are provided intentionally as task context.",
        "- Do not request filesystem permission or claim missing file access.",
        "- If user asks to modify files, respond with direct edits based on this context (prefer unified diff).",
        "",
        "Referenced file context:",
    ]
    included_count = 0

    for item in context_files:
        if included_count >= MAX_CONTEXT_FILES:
            break
        path = (item.path or "").strip() if item.path else ""
        if not path:
            continue
        content = _resolve_context_file_content(item)
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)

        if len(content) > MAX_CONTEXT_FILE_CHARS:
            content = content[:MAX_CONTEXT_FILE_CHARS] + "\n\n[truncated by codex.serve context limit]"

        lines.append("")
        lines.append(f"--- FILE: {path} ---")
        lines.append(content)
        lines.append(f"--- END FILE: {path} ---")
        included_count += 1

    if included_count == 0:
        return prompt_text

    return "\n".join(lines).strip() + "\n"


def _extract_model_from_args(args: List[str]) -> Optional[str]:
    for idx, arg in enumerate(args):
        if arg in ("--model", "-m"):
            if idx + 1 < len(args):
                model = args[idx + 1].strip()
                return model or None
            return None
        if arg.startswith("--model="):
            model = arg.split("=", 1)[1].strip()
            return model or None
    return None


def _strip_model_args(args: List[str]) -> List[str]:
    normalized_args: List[str] = []
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg in ("--model", "-m"):
            idx += 2
            continue
        if arg.startswith("--model="):
            idx += 1
            continue
        normalized_args.append(arg)
        idx += 1
    return normalized_args


def _build_docker_env(agent: str, args: List[str], req_env: Optional[Dict[str, str]]) -> Dict[str, str]:
    docker_env: Dict[str, str] = {}

    # Default LiteLLM settings from codex.serve runtime env (e.g., docker-compose).
    for env_key in ("LITELLM_BASE_URL", "LITELLM_API_KEY"):
        env_val = os.environ.get(env_key)
        if env_val:
            docker_env[env_key] = env_val

    # Request env can optionally override defaults.
    docker_env.update(req_env or {})

    # Required by codex.agent entrypoint for provider-specific env mapping.
    docker_env["AGENT_PROVIDER_NAME"] = agent

    configured_model = (docker_env.get("LITELLM_MODEL") or "").strip() or None
    if configured_model:
        docker_env["LITELLM_MODEL"] = configured_model
    elif docker_env.get("LITELLM_MODEL") is not None:
        docker_env.pop("LITELLM_MODEL", None)

    # If no explicit model env provided, infer from common agent flags.
    if not docker_env.get("LITELLM_MODEL"):
        inferred_model = (_extract_model_from_args(args) or "").strip() or None
        if inferred_model:
            docker_env["LITELLM_MODEL"] = inferred_model

    return docker_env


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass

    process.kill()
    await process.wait()


async def _await_with_deadline(coro, deadline: Optional[float]):
    if deadline is None:
        return await coro

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError()

    return await asyncio.wait_for(coro, timeout=remaining)


async def _register_session(sessionId: str, process: asyncio.subprocess.Process) -> None:
    async with SESSIONS_LOCK:
        RUN_SESSIONS[sessionId] = process


async def _unregister_session(sessionId: str, process: Optional[asyncio.subprocess.Process]) -> None:
    async with SESSIONS_LOCK:
        current = RUN_SESSIONS.get(sessionId)
        if process is None or current is process:
            RUN_SESSIONS.pop(sessionId, None)
        STOP_REQUESTED_SESSIONS.discard(sessionId)


async def _mark_stop_requested(sessionId: str) -> None:
    async with SESSIONS_LOCK:
        STOP_REQUESTED_SESSIONS.add(sessionId)


async def _consume_stop_requested(sessionId: str) -> bool:
    async with SESSIONS_LOCK:
        if sessionId in STOP_REQUESTED_SESSIONS:
            STOP_REQUESTED_SESSIONS.remove(sessionId)
            return True
        return False


async def _get_active_session_process(sessionId: str) -> Optional[asyncio.subprocess.Process]:
    async with SESSIONS_LOCK:
        process = RUN_SESSIONS.get(sessionId)
        if process is None or process.returncode is not None:
            return None
        return process


def _normalize_required_path(value: str, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{field_name} cannot be empty")
    return os.path.abspath(normalized)


def _host_path_to_container_path(host_path: str, mount_root: str) -> str:
    rel_path = os.path.relpath(host_path, mount_root)
    if rel_path == ".":
        return "/workspace"
    return "/workspace/" + rel_path.replace("\\", "/")


def _collect_insight_files(output_dir: str) -> List[InsightFileResult]:
    out_path = Path(output_dir)
    if not out_path.exists() or not out_path.is_dir():
        return []

    results: List[InsightFileResult] = []
    for page in sorted(out_path.glob("*.md")):
        if len(results) >= MAX_INSIGHT_FILES:
            break
        try:
            content = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(content) > MAX_INSIGHT_FILE_CHARS:
            content = content[:MAX_INSIGHT_FILE_CHARS] + "\n\n[truncated by codex.serve return limit]\n"
        results.append(
            InsightFileResult(
                path=page.name,
                content=content,
            )
        )
    return results


@app.get("/models")
async def get_models():
    return {
        "models": MODEL_LIST,
        "count": len(MODEL_LIST),
    }


@app.get("/agents")
async def get_agents():
    agents = sorted(set(AGENT_LIST))
    return {
        "agents": agents,
        "count": len(agents),
    }


@app.post("/sessions/{sessionId}/stop")
async def stop_session(sessionId: str):
    normalizedSessionId = sessionId.strip()
    if not normalizedSessionId:
        raise HTTPException(status_code=400, detail="sessionId cannot be empty")

    process = await _get_active_session_process(normalizedSessionId)
    if process is None:
        raise HTTPException(status_code=404, detail=f"Session not found or already finished: {normalizedSessionId}")

    await _mark_stop_requested(normalizedSessionId)
    await _terminate_process(process)
    return {
        "sessionId": normalizedSessionId,
        "status": "stopped",
    }


@app.post("/insight/run", response_model=InsightRunResponse)
async def run_insight(req: InsightRunRequest):
    repo_path = _normalize_required_path(req.repoPath, "repoPath")
    out_path = _normalize_required_path(req.outPath, "outPath")

    if not os.path.isdir(repo_path):
        raise HTTPException(status_code=400, detail=f"repoPath is not a directory: {repo_path}")

    out_parent = os.path.dirname(out_path) or os.path.sep
    if not os.path.exists(out_parent):
        raise HTTPException(status_code=400, detail=f"Parent directory for outPath does not exist: {out_parent}")

    os.makedirs(out_path, exist_ok=True)

    try:
        mount_root = os.path.commonpath([repo_path, out_path])
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="repoPath and outPath must be on the same filesystem root for Docker volume mounting",
        ) from exc

    if not mount_root:
        raise HTTPException(status_code=400, detail="Failed to determine common mount root")

    container_repo = _host_path_to_container_path(repo_path, mount_root)
    container_out = _host_path_to_container_path(out_path, mount_root)

    docker_env: Dict[str, str] = {}
    for env_key in ("LITELLM_BASE_URL", "LITELLM_API_KEY"):
        env_val = os.environ.get(env_key)
        if env_val:
            docker_env[env_key] = env_val

    request_env = req.env or {}
    docker_env.update(request_env)

    if _is_default_codex_insight_image(INSIGHT_DOCKER_IMAGE):
        docker_env.pop("LITELLM_MODEL", None)

        configured_model = (os.environ.get("INSIGHT_MODEL") or "").strip()
        request_model_override = (request_env.get("INSIGHT_MODEL") or "").strip()
        selected_model = request_model_override or configured_model

        if selected_model:
            docker_env["LITELLM_MODEL"] = selected_model
    else:
        configured_model = (os.environ.get("LITELLM_MODEL") or "").strip()
        if configured_model and "LITELLM_MODEL" not in request_env:
            docker_env["LITELLM_MODEL"] = configured_model

    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{mount_root}:/workspace",
    ]

    for env_key, env_val in docker_env.items():
        command.extend(["-e", f"{env_key}={env_val}"])

    command.append(INSIGHT_DOCKER_IMAGE)
    command.extend(["--repo", container_repo, "--out", container_out])

    for pattern in req.include or []:
        command.extend(["--include", pattern])

    for pattern in req.exclude or []:
        command.extend(["--exclude", pattern])

    if req.maxFilesPerModule is not None:
        command.extend(["--max-files-per-module", str(req.maxFilesPerModule)])

    if req.maxCharsPerFile is not None:
        command.extend(["--max-chars-per-file", str(req.maxCharsPerFile)])

    if req.dryRun:
        command.append("--dry-run")

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        if INSIGHT_RESPONSE_TIMEOUT_SECONDS is not None:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=INSIGHT_RESPONSE_TIMEOUT_SECONDS,
            )
        else:
            stdout_bytes, stderr_bytes = await process.communicate()
    except asyncio.TimeoutError as exc:
        await _terminate_process(process)
        raise HTTPException(
            status_code=504,
            detail=(
                "Request timed out while waiting for codex-insight response "
                f"({INSIGHT_RESPONSE_TIMEOUT_SECONDS}s)."
            ),
        ) from exc

    stdout_text = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    insight_files = _collect_insight_files(out_path) if process.returncode == 0 else []

    return InsightRunResponse(
        stdout=stdout_text,
        stderr=stderr_text,
        exit_code=process.returncode if process.returncode is not None else 1,
        outputDir=out_path,
        files=insight_files,
        count=len(insight_files),
    )

@app.post("/run")
async def run_agent(req: RunRequest):
    if req.agent not in AGENT_LIST:
        raise HTTPException(status_code=400, detail=f"Unsupported agent: {req.agent}")

    normalized_session_id = req.sessionId.strip() if req.sessionId else ""
    sessionId = normalized_session_id if normalized_session_id else str(uuid4())
    stdin_payload = _build_stdin_with_context(req.stdin, req.contextFiles)

    existing_process = await _get_active_session_process(sessionId)
    if existing_process is not None:
        raise HTTPException(status_code=409, detail=f"Session is already running: {sessionId}")

    popen_env = os.environ.copy()
    normalized_req_args = list(req.args)

    if DOCKER_IMAGE:
        # Run inside Docker
        command = ["docker", "run", "--rm", "-i"]

        normalized_args = normalized_req_args
        docker_env = _build_docker_env(req.agent, normalized_req_args, req.env)

        # opencode and codex in codex.agent expect model via LITELLM_MODEL and will
        # inject a provider-aware --model value for non-interactive runs.
        if req.agent in ("opencode", "codex"):
            normalized_args = _strip_model_args(normalized_req_args)

        for k, v in docker_env.items():
            command.extend(["-e", f"{k}={v}"])

        command.append(DOCKER_IMAGE)
        # Use simple agent name inside container (matches Dockerfile symlinks)
        command.append(req.agent)
        command.extend(normalized_args)
    else:
        # Run locally
        executable = req.agent
        command = [executable] + normalized_req_args
        if req.env:
            popen_env.update(req.env)

    async def stream_generator():
        read_tasks = []
        process = None
        try:
            yield json.dumps({"type": "session", "id": sessionId}) + "\n"

            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=popen_env
            )
            await _register_session(sessionId, process)

            # Write stdin
            if stdin_payload:
                process.stdin.write(stdin_payload.encode())
                await process.stdin.drain()
            process.stdin.close()

            # Queue to aggregate chunks from both stdout and stderr
            queue = asyncio.Queue()

            async def read_stream(stream, type_label):
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                buffer = ""
                try:
                    while True:
                        chunk = await stream.read(4096)
                        if not chunk:
                            break

                        buffer += decoder.decode(chunk)

                        while True:
                            newline_index = buffer.find("\n")
                            if newline_index == -1:
                                break
                            line = buffer[: newline_index + 1]
                            buffer = buffer[newline_index + 1 :]
                            await queue.put({"type": type_label, "data": line})

                    buffer += decoder.decode(b"", final=True)
                    if buffer:
                        await queue.put({"type": type_label, "data": buffer})
                except Exception as stream_error:
                    await queue.put(
                        {
                            "type": "stderr",
                            "data": f"{type_label} stream read failed: {str(stream_error)}\n",
                        }
                    )
                finally:
                    # Always signal completion so active_streams cannot hang forever.
                    await queue.put(None)

            # Start reading tasks
            read_tasks = [
                asyncio.create_task(read_stream(process.stdout, "stdout")),
                asyncio.create_task(read_stream(process.stderr, "stderr")),
            ]

            deadline = None
            if RESPONSE_TIMEOUT_SECONDS is not None:
                deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_SECONDS

            active_streams = 2
            while active_streams > 0:
                item = await _await_with_deadline(queue.get(), deadline)
                if item is None:
                    active_streams -= 1
                else:
                    yield json.dumps(item) + "\n"

            exit_code = await _await_with_deadline(process.wait(), deadline)
            stopped_by_api = await _consume_stop_requested(sessionId)
            if stopped_by_api:
                yield json.dumps({"type": "stderr", "data": "Session stopped via API.\n"}) + "\n"
                yield json.dumps({"type": "exit", "code": 0}) + "\n"
            else:
                yield json.dumps({"type": "exit", "code": exit_code}) + "\n"

        except asyncio.TimeoutError:
            if process is not None:
                await _terminate_process(process)
            timeout_msg = (
                "Request timed out while waiting for agent response "
                f"({RESPONSE_TIMEOUT_SECONDS}s)."
            )
            yield json.dumps({"type": "stderr", "data": timeout_msg}) + "\n"
            yield json.dumps({"type": "exit", "code": 124}) + "\n"

        except Exception as e:
            error_data = {"type": "stderr", "data": f"Internal Server Error: {str(e)}"}
            yield json.dumps(error_data) + "\n"
            yield json.dumps({"type": "exit", "code": 1}) + "\n"

        finally:
            for task in read_tasks:
                if not task.done():
                    task.cancel()
            if read_tasks:
                await asyncio.gather(*read_tasks, return_exceptions=True)
            await _unregister_session(sessionId, process)

    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
