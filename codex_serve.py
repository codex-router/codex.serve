import os
import asyncio
import json
import codecs
import base64
import shutil
import tempfile
import urllib.request
import urllib.error
import urllib.parse
import socket
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
    repoPath: Optional[str] = None
    repo_path: Optional[str] = None
    repo: Optional[str] = None
    workspaceRoot: Optional[str] = None
    outPath: Optional[str] = None
    out_path: Optional[str] = None
    outputDir: Optional[str] = None
    output_dir: Optional[str] = None
    files: Optional[List[ContextFileItem]] = None
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


class GraphRunRequest(BaseModel):
    code: str
    file_paths: List[str]
    framework_hint: Optional[str] = None
    metadata: Optional[List[Dict]] = None
    http_connections: Optional[str] = None
    env: Optional[Dict[str, str]] = None


class GraphRunResponse(BaseModel):
    graph: Dict
    usage: Optional[Dict] = None
    cost: Optional[Dict] = None

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

DEFAULT_AGENT_MODEL = []
AGENT_MODEL = [
    model.strip()
    for model in os.environ.get("AGENT_MODEL", ",".join(DEFAULT_AGENT_MODEL)).split(",")
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

GRAPH_RESPONSE_TIMEOUT_SECONDS = _parse_response_timeout_seconds(
    os.environ.get("GRAPH_RESPONSE_TIMEOUT_SECONDS")
)

GRAPH_BASE_URL = (os.environ.get("GRAPH_BASE_URL") or "http://localhost:52104").rstrip("/")
GRAPH_MODEL = (os.environ.get("GRAPH_MODEL") or os.environ.get("LITELLM_MODEL") or "").strip()
CODEX_GRAPH_IMAGE = (os.environ.get("CODEX_GRAPH_IMAGE") or "craftslab/codex-graph:latest").strip()
GRAPH_AUTO_START_ENABLED = (os.environ.get("GRAPH_AUTO_START") or "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
GRAPH_CONTAINER_NAME = (os.environ.get("GRAPH_CONTAINER_NAME") or "codex-graph").strip()
GRAPH_HEALTH_CHECK_TIMEOUT_SECONDS = (
    _parse_response_timeout_seconds(os.environ.get("GRAPH_HEALTH_CHECK_TIMEOUT_SECONDS")) or 60.0
)


def _build_graph_base_url_candidates(base_url: str) -> List[str]:
    candidates: List[str] = []
    normalized = (base_url or "").strip().rstrip("/")
    if normalized:
        candidates.append(normalized)

    if os.path.exists("/.dockerenv") and normalized:
        try:
            parsed = urllib.parse.urlparse(normalized)
            host = (parsed.hostname or "").lower()
            if host in {"localhost", "127.0.0.1", "::1"}:
                replacement = "host.docker.internal"
                if host == "::1":
                    replacement = "[host.docker.internal]"
                alt_netloc = parsed.netloc.replace(parsed.hostname, replacement)
                alternative = urllib.parse.urlunparse(parsed._replace(netloc=alt_netloc)).rstrip("/")
                if alternative and alternative not in candidates:
                    candidates.append(alternative)
        except Exception:
            pass

    return candidates


GRAPH_BASE_URL_CANDIDATES = _build_graph_base_url_candidates(GRAPH_BASE_URL)

GRAPH_START_LOCK = asyncio.Lock()

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
    for env_key in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_SSL_VERIFY", "LITELLM_CA_BUNDLE"):
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


def _resolve_requested_output_dir(req: InsightRunRequest) -> Optional[str]:
    candidates = [req.outPath, req.out_path, req.outputDir, req.output_dir]
    for candidate in candidates:
        if candidate and candidate.strip():
            return os.path.abspath(candidate.strip())
    return None


def _copy_tree_contents(source_dir: str, destination_dir: str) -> None:
    source = Path(source_dir)
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)

    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _host_path_to_container_path(host_path: str, mount_root: str) -> str:
    rel_path = os.path.relpath(host_path, mount_root)
    if rel_path == ".":
        return "/workspace"
    return "/workspace/" + rel_path.replace("\\", "/")


def _is_running_in_docker_container() -> bool:
    return os.path.exists("/.dockerenv")


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


def _normalize_repo_file_path(path: str) -> str:
    normalized = (path or "").strip().replace("\\", "/")
    normalized = normalized.lstrip("/")
    if not normalized:
        return ""
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _resolve_context_file_bytes(item: ContextFileItem) -> Optional[bytes]:
    if item.base64Content:
        try:
            return base64.b64decode(item.base64Content)
        except Exception:
            return None
    if item.content is not None:
        return item.content.encode("utf-8", errors="replace")
    return b""


def _write_uploaded_repo_files(repo_dir: str, files: List[ContextFileItem]) -> int:
    written = 0
    for item in files:
        if item is None:
            continue
        rel_path = _normalize_repo_file_path(item.path)
        if not rel_path:
            continue
        payload = _resolve_context_file_bytes(item)
        if payload is None:
            continue
        destination = Path(repo_dir) / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        written += 1
    return written


async def _run_subprocess_capture(
    command: List[str],
    timeout: Optional[float] = None,
    timeout_message: Optional[str] = None,
    cwd: Optional[str] = None,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        if timeout is not None:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
        else:
            stdout_bytes, stderr_bytes = await process.communicate()
    except asyncio.TimeoutError as exc:
        await _terminate_process(process)
        if timeout_message:
            raise HTTPException(status_code=504, detail=timeout_message) from exc
        raise

    stdout_text = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    return process.returncode if process.returncode is not None else 1, stdout_text, stderr_text


def _build_insight_args(req: InsightRunRequest) -> List[str]:
    args: List[str] = []
    for pattern in req.include or []:
        args.extend(["--include", pattern])
    for pattern in req.exclude or []:
        args.extend(["--exclude", pattern])
    if req.maxFilesPerModule is not None:
        args.extend(["--max-files-per-module", str(req.maxFilesPerModule)])
    if req.maxCharsPerFile is not None:
        args.extend(["--max-chars-per-file", str(req.maxCharsPerFile)])
    if req.dryRun:
        args.append("--dry-run")
    return args


async def _post_json(url: str, payload: Dict, timeout_seconds: Optional[float]) -> tuple[int, str]:
    def _do_post() -> tuple[int, str]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        timeout_arg = timeout_seconds if timeout_seconds is not None else None
        try:
            with urllib.request.urlopen(req, timeout=timeout_arg) as response:
                status = getattr(response, "status", 200)
                response_body = response.read().decode("utf-8", errors="replace")
                return status, response_body
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read() if exc.fp is not None else b""
            body_text = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
            return exc.code, body_text

    return await asyncio.to_thread(_do_post)


async def _get_json(url: str, timeout_seconds: Optional[float]) -> tuple[int, str]:
    def _do_get() -> tuple[int, str]:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
            },
            method="GET",
        )

        timeout_arg = timeout_seconds if timeout_seconds is not None else None
        try:
            with urllib.request.urlopen(req, timeout=timeout_arg) as response:
                status = getattr(response, "status", 200)
                response_body = response.read().decode("utf-8", errors="replace")
                return status, response_body
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read() if exc.fp is not None else b""
            body_text = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
            return exc.code, body_text

    return await asyncio.to_thread(_do_get)


async def _is_graph_healthy() -> bool:
    for base_url in GRAPH_BASE_URL_CANDIDATES:
        health_url = f"{base_url}/health"
        try:
            status_code, _ = await _get_json(health_url, timeout_seconds=5.0)
        except Exception:
            continue
        if 200 <= status_code < 300:
            return True
    return False


async def _resolve_graph_base_url_for_requests() -> str:
    for base_url in GRAPH_BASE_URL_CANDIDATES:
        health_url = f"{base_url}/health"
        try:
            status_code, _ = await _get_json(health_url, timeout_seconds=5.0)
        except Exception:
            continue
        if 200 <= status_code < 300:
            return base_url
    return GRAPH_BASE_URL


async def _is_graph_container_running() -> bool:
    if not GRAPH_CONTAINER_NAME:
        return False
    command = [
        "docker",
        "inspect",
        "-f",
        "{{.State.Running}}",
        GRAPH_CONTAINER_NAME,
    ]
    code, stdout_text, _ = await _run_subprocess_capture(command, timeout=15.0)
    return code == 0 and stdout_text.strip().lower() == "true"


async def _start_graph_backend_with_image() -> tuple[int, str, str]:
    if not CODEX_GRAPH_IMAGE:
        raise HTTPException(status_code=502, detail="CODEX_GRAPH_IMAGE is not configured")

    graph_env: Dict[str, str] = {}
    for env_key in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_SSL_VERIFY", "LITELLM_CA_BUNDLE"):
        env_val = os.environ.get(env_key)
        if env_val:
            graph_env[env_key] = env_val
    if GRAPH_MODEL:
        graph_env["LITELLM_MODEL"] = GRAPH_MODEL

    if GRAPH_CONTAINER_NAME and await _is_graph_container_running():
        return 0, "", ""

    if GRAPH_CONTAINER_NAME:
        # Remove stale container if exists and stopped.
        await _run_subprocess_capture(["docker", "rm", "-f", GRAPH_CONTAINER_NAME], timeout=15.0)

    command = ["docker", "run", "-d"]
    if GRAPH_CONTAINER_NAME:
        command.extend(["--name", GRAPH_CONTAINER_NAME])
    command.extend(["-p", "52104:52104"])
    for env_key, env_val in graph_env.items():
        command.extend(["-e", f"{env_key}={env_val}"])
    command.append(CODEX_GRAPH_IMAGE)

    return await _run_subprocess_capture(command, timeout=120.0)


async def _ensure_graph_backend_ready() -> None:
    if await _is_graph_healthy():
        return

    if not GRAPH_AUTO_START_ENABLED:
        raise HTTPException(
            status_code=502,
            detail=(
                "codex.graph backend is not healthy and auto-start is disabled. "
                "Enable GRAPH_AUTO_START or start codex.graph backend manually."
            ),
        )

    async with GRAPH_START_LOCK:
        if await _is_graph_healthy():
            return

        start_code, _, start_stderr = await _start_graph_backend_with_image()

        if start_code != 0:
            raise HTTPException(
                status_code=502,
                detail=start_stderr.strip() or "Failed to start codex.graph backend via docker compose",
            )

        deadline = asyncio.get_running_loop().time() + GRAPH_HEALTH_CHECK_TIMEOUT_SECONDS
        while True:
            if await _is_graph_healthy():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Timed out waiting for codex.graph health endpoint after startup "
                        f"({GRAPH_HEALTH_CHECK_TIMEOUT_SECONDS}s)."
                    ),
                )
            await asyncio.sleep(1.0)


@app.get("/models")
async def get_models():
    return {
        "models": AGENT_MODEL,
        "count": len(AGENT_MODEL),
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
    uploaded_files = req.files or []
    if len(uploaded_files) == 0:
        raise HTTPException(status_code=400, detail="files is required")

    requested_output_dir = _resolve_requested_output_dir(req)
    if requested_output_dir:
        response_output_dir = requested_output_dir
    else:
        response_output_dir = tempfile.mkdtemp(prefix="codex-insight-out-")

    docker_env: Dict[str, str] = {}
    for env_key in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_SSL_VERIFY", "LITELLM_CA_BUNDLE"):
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
    temp_root = tempfile.mkdtemp(prefix="codex-insight-upload-")
    repo_path = os.path.join(temp_root, "repo")
    transient_out_path = os.path.join(temp_root, "out")
    os.makedirs(repo_path, exist_ok=True)
    os.makedirs(transient_out_path, exist_ok=True)

    try:
        written_count = _write_uploaded_repo_files(repo_path, uploaded_files)
        if written_count == 0:
            raise HTTPException(status_code=400, detail="No valid files were provided")

        container_name = f"codex-insight-{uuid4().hex}"
        container_repo = "/tmp/codex-repo"
        container_out = "/tmp/codex-out"

        create_command = ["docker", "create", "--name", container_name]
        for env_key, env_val in docker_env.items():
            create_command.extend(["-e", f"{env_key}={env_val}"])
        create_command.append(INSIGHT_DOCKER_IMAGE)
        create_command.extend(["--repo", container_repo, "--out", container_out])
        create_command.extend(_build_insight_args(req))

        create_code, _, create_stderr = await _run_subprocess_capture(create_command)
        if create_code != 0:
            raise HTTPException(status_code=400, detail=create_stderr.strip() or "Failed to prepare insight container")

        try:
            cp_in_code, _, cp_in_stderr = await _run_subprocess_capture(
                ["docker", "cp", f"{repo_path}{os.sep}.", f"{container_name}:{container_repo}"]
            )
            if cp_in_code != 0:
                raise HTTPException(status_code=400, detail=cp_in_stderr.strip() or "Failed to upload files to insight container")

            timeout_message = (
                "Request timed out while waiting for codex-insight response "
                f"({INSIGHT_RESPONSE_TIMEOUT_SECONDS}s)."
            )
            run_code, stdout_text, stderr_text = await _run_subprocess_capture(
                ["docker", "start", "-a", container_name],
                timeout=INSIGHT_RESPONSE_TIMEOUT_SECONDS,
                timeout_message=timeout_message,
            )

            if run_code == 0:
                cp_out_code, _, cp_out_stderr = await _run_subprocess_capture(
                    ["docker", "cp", f"{container_name}:{container_out}{os.sep}.", transient_out_path]
                )
                missing_output_in_dry_run = (
                    req.dryRun
                    and cp_out_code != 0
                    and "Could not find the file" in cp_out_stderr
                    and container_out in cp_out_stderr
                )
                if cp_out_code != 0 and not missing_output_in_dry_run:
                    raise HTTPException(status_code=400, detail=cp_out_stderr.strip() or "Failed to collect insight output")

                if cp_out_code == 0:
                    try:
                        _copy_tree_contents(transient_out_path, response_output_dir)
                    except OSError as exc:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Failed to write insight output directory: {exc}",
                        ) from exc

            insight_files = _collect_insight_files(response_output_dir) if run_code == 0 else []
        finally:
            await _run_subprocess_capture(["docker", "rm", "-f", container_name])
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    return InsightRunResponse(
        stdout=stdout_text,
        stderr=stderr_text,
        exit_code=run_code,
        outputDir=response_output_dir,
        files=insight_files,
        count=len(insight_files),
    )


@app.post("/graph/run", response_model=GraphRunResponse)
async def run_graph(req: GraphRunRequest):
    await _ensure_graph_backend_ready()
    graph_base_url = await _resolve_graph_base_url_for_requests()

    code = (req.code or "").strip()
    file_paths = req.file_paths or []

    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    if not file_paths:
        raise HTTPException(status_code=400, detail="file_paths is required")

    payload: Dict = {
        "code": req.code,
        "file_paths": file_paths,
    }
    if req.framework_hint:
        payload["framework_hint"] = req.framework_hint
    if req.metadata is not None:
        payload["metadata"] = req.metadata
    if req.http_connections:
        payload["http_connections"] = req.http_connections

    graph_env: Dict[str, str] = {}
    for env_key in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_SSL_VERIFY", "LITELLM_CA_BUNDLE"):
        env_val = os.environ.get(env_key)
        if env_val:
            graph_env[env_key] = env_val

    request_env = req.env or {}
    request_graph_model = (request_env.get("GRAPH_MODEL") or "").strip()
    request_litellm_model = (request_env.get("LITELLM_MODEL") or "").strip()

    graph_env.update(request_env)
    graph_env.pop("GRAPH_MODEL", None)

    selected_model = request_graph_model or request_litellm_model or GRAPH_MODEL
    if selected_model:
        graph_env["LITELLM_MODEL"] = selected_model
    else:
        graph_env.pop("LITELLM_MODEL", None)

    if graph_env:
        payload["env"] = graph_env

    graph_analyze_url = f"{graph_base_url}/analyze"
    timeout_message = (
        "Request timed out while waiting for codex.graph response "
        f"({GRAPH_RESPONSE_TIMEOUT_SECONDS}s)."
    )

    try:
        status_code, response_body = await _post_json(
            graph_analyze_url,
            payload,
            GRAPH_RESPONSE_TIMEOUT_SECONDS,
        )
    except (TimeoutError, socket.timeout) as exc:
        raise HTTPException(status_code=504, detail=timeout_message) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError) or isinstance(exc.reason, socket.timeout):
            raise HTTPException(status_code=504, detail=timeout_message) from exc
        raise HTTPException(status_code=502, detail=f"Failed to call codex.graph: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to call codex.graph: {exc}") from exc

    if status_code < 200 or status_code >= 300:
        detail = response_body.strip() or f"codex.graph returned status {status_code}"
        raise HTTPException(status_code=502, detail=detail)

    try:
        parsed = json.loads(response_body) if response_body.strip() else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Invalid JSON response from codex.graph") from exc

    graph = parsed.get("graph")
    if not isinstance(graph, dict):
        raise HTTPException(status_code=502, detail="Invalid /analyze response from codex.graph: missing graph")

    return GraphRunResponse(
        graph=graph,
        usage=parsed.get("usage"),
        cost=parsed.get("cost"),
    )

@app.post("/agent/run")
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
