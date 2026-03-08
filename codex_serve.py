import os
import asyncio
import json
import codecs
import base64
import logging
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.error
import urllib.parse
import socket
import ssl
import math
import shlex
from pathlib import Path
from typing import List, Optional, Dict, Any
from uuid import uuid4
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()
logger = logging.getLogger("codex.serve")
logger.setLevel(logging.INFO)


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


class SandboxRunRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    timeoutSeconds: Optional[float] = None
    settingsPath: Optional[str] = None
    env: Optional[Dict[str, str]] = None


class SandboxRunResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    command: str
    timed_out: bool = False
    timeout_seconds: Optional[float] = None

# Supported agent providers, configurable via env (comma-separated)
DEFAULT_AGENT_LIST = ["codex"]
AGENT_LIST = [
    agent.strip()
    for agent in os.environ.get("AGENT_LIST", ",".join(DEFAULT_AGENT_LIST)).split(",")
    if agent.strip()
]
TEAM_AGENT_NAME = "team"
OPENCLAW_AGENT_NAME = "openclaw"

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
TEAM_RUN_SESSIONS = set()
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

GRAPH_RESPONSE_TIMEOUT_SECONDS = (
    _parse_response_timeout_seconds(os.environ.get("GRAPH_RESPONSE_TIMEOUT_SECONDS")) or 120.0
)

def _default_graph_base_url() -> str:
    if os.path.exists("/.dockerenv"):
        return "http://host.docker.internal:52104"
    return "http://localhost:52104"


GRAPH_BASE_URL = (os.environ.get("GRAPH_BASE_URL") or _default_graph_base_url()).rstrip("/")
GRAPH_MODEL = (os.environ.get("GRAPH_MODEL") or os.environ.get("LITELLM_MODEL") or "").strip()
CODEX_GRAPH_IMAGE = (os.environ.get("CODEX_GRAPH_IMAGE") or "craftslab/codex-graph-cli:latest").strip()
GRAPH_AUTO_START_ENABLED = (os.environ.get("GRAPH_AUTO_START") or "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
GRAPH_CONTAINER_NAME = (os.environ.get("GRAPH_CONTAINER_NAME") or "codex-graph").strip()
GRAPH_HEALTH_CHECK_TIMEOUT_SECONDS = 60.0


def _parse_non_negative_int(value: Optional[str], default_value: int) -> int:
    if value is None:
        return default_value
    normalized = value.strip()
    if not normalized:
        return default_value
    try:
        parsed = int(normalized)
    except ValueError:
        return default_value
    return parsed if parsed >= 0 else default_value


GRAPH_ANALYZE_MAX_RETRIES = _parse_non_negative_int(
    os.environ.get("GRAPH_ANALYZE_MAX_RETRIES"),
    1,
)

GRAPH_ANALYZE_RETRY_DELAY_SECONDS = (
    _parse_response_timeout_seconds(os.environ.get("GRAPH_ANALYZE_RETRY_DELAY_SECONDS")) or 2.0
)

REQUEST_QUEUE_WAIT_TIMEOUT_SECONDS = _parse_response_timeout_seconds(
    os.environ.get("REQUEST_QUEUE_WAIT_TIMEOUT_SECONDS")
)
REQUEST_QUEUE_MAX_PENDING = _parse_non_negative_int(
    os.environ.get("REQUEST_QUEUE_MAX_PENDING"),
    100,
)
AGENT_MAX_CONCURRENT_REQUESTS = _parse_non_negative_int(
    os.environ.get("AGENT_MAX_CONCURRENT_REQUESTS"),
    4,
)
INSIGHT_MAX_CONCURRENT_REQUESTS = _parse_non_negative_int(
    os.environ.get("INSIGHT_MAX_CONCURRENT_REQUESTS"),
    2,
)
GRAPH_MAX_CONCURRENT_REQUESTS = _parse_non_negative_int(
    os.environ.get("GRAPH_MAX_CONCURRENT_REQUESTS"),
    4,
)
SANDBOX_MAX_CONCURRENT_REQUESTS = _parse_non_negative_int(
    os.environ.get("SANDBOX_MAX_CONCURRENT_REQUESTS"),
    2,
)

def _default_sandbox_base_url() -> str:
    if os.path.exists("/.dockerenv"):
        return "http://codex-sandbox:2000"
    return "http://localhost:2000"


SANDBOX_BASE_URL = (os.environ.get("SANDBOX_BASE_URL") or _default_sandbox_base_url()).rstrip("/")


def _build_sandbox_base_url_candidates(base_url: str) -> List[str]:
    candidates: List[str] = []

    def _add_candidate(value: Optional[str]) -> None:
        normalized_value = (value or "").strip().rstrip("/")
        if not normalized_value:
            return
        if normalized_value not in candidates:
            candidates.append(normalized_value)

    normalized = (base_url or "").strip().rstrip("/")
    _add_candidate(normalized)

    if normalized:
        try:
            parsed = urllib.parse.urlparse(normalized)
            host = (parsed.hostname or "").lower()
            alt_host: Optional[str] = None

            if os.path.exists("/.dockerenv"):
                if host in {"localhost", "127.0.0.1", "::1"}:
                    alt_host = "host.docker.internal"
                elif host == "host.docker.internal":
                    alt_host = "localhost"

            if alt_host:
                replacement = f"[{alt_host}]" if host == "::1" else alt_host
                alt_netloc = parsed.netloc.replace(parsed.hostname, replacement)
                alternative = urllib.parse.urlunparse(parsed._replace(netloc=alt_netloc)).rstrip("/")
                _add_candidate(alternative)
        except Exception:
            pass

    if os.path.exists("/.dockerenv"):
        _add_candidate("http://host.docker.internal:2000")
        _add_candidate("http://codex-sandbox:2000")
        _add_candidate("http://sandbox:2000")
        _add_candidate("http://piston_api:2000")
        _add_candidate("http://localhost:2000")
    else:
        _add_candidate("http://localhost:2000")

    return candidates


SANDBOX_BASE_URL_CANDIDATES = _build_sandbox_base_url_candidates(SANDBOX_BASE_URL)
SANDBOX_RUN_TIMEOUT_SECONDS = (
    _parse_response_timeout_seconds(os.environ.get("SANDBOX_RUN_TIMEOUT_SECONDS")) or 60.0
)
SANDBOX_HARD_TIMEOUT_SECONDS = (
    _parse_response_timeout_seconds(os.environ.get("SANDBOX_HARD_TIMEOUT_SECONDS")) or 3.0
)

AUTO_COMPRESS_ON_CONTEXT_OVERFLOW = (
    (os.environ.get("AUTO_COMPRESS_ON_CONTEXT_OVERFLOW") or "true").strip().lower()
    in {"1", "true", "yes", "on"}
)
AUTO_COMPRESS_MAX_CHARS = max(
    2_000,
    _parse_non_negative_int(os.environ.get("AUTO_COMPRESS_MAX_CHARS"), 24_000),
)
AUTO_COMPRESS_KEEP_HEAD_CHARS = max(
    0,
    _parse_non_negative_int(os.environ.get("AUTO_COMPRESS_KEEP_HEAD_CHARS"), 6_000),
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

CONTEXT_OVERFLOW_ERROR_PATTERNS = (
    "maximum context length",
    "context length exceeded",
    "context window",
    "too many tokens",
    "token limit exceeded",
    "prompt is too long",
    "input is too long",
    "request too large",
    "context overflow",
)


class _QueueLease:
    def __init__(self, semaphore: asyncio.Semaphore):
        self._semaphore = semaphore
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()


class RequestAdmissionQueue:
    def __init__(
        self,
        name: str,
        max_concurrency: int,
        max_pending: int,
        wait_timeout_seconds: Optional[float],
    ):
        self.name = name
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_pending = max(0, int(max_pending))
        self.wait_timeout_seconds = wait_timeout_seconds

        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._pending_lock = asyncio.Lock()
        self._pending = 0

    async def acquire(self) -> _QueueLease:
        async with self._pending_lock:
            if self._pending >= self.max_pending:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"{self.name} request queue is full "
                        f"(max pending={self.max_pending})."
                    ),
                )
            self._pending += 1

        acquired = False
        try:
            if self.wait_timeout_seconds is None:
                await self._semaphore.acquire()
            else:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=self.wait_timeout_seconds,
                )
            acquired = True
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"{self.name} request queue wait timeout "
                    f"({self.wait_timeout_seconds}s)."
                ),
            ) from exc
        finally:
            async with self._pending_lock:
                self._pending -= 1

        if not acquired:
            raise HTTPException(status_code=503, detail=f"{self.name} request queue unavailable")

        return _QueueLease(self._semaphore)


def _is_default_codex_insight_image(image: str) -> bool:
    normalized = (image or "").strip().lower()
    return normalized in {"craftslab/codex-insight:latest", "codex-insight:latest"}


AGENT_REQUEST_QUEUE = RequestAdmissionQueue(
    name="agent.run",
    max_concurrency=AGENT_MAX_CONCURRENT_REQUESTS,
    max_pending=REQUEST_QUEUE_MAX_PENDING,
    wait_timeout_seconds=REQUEST_QUEUE_WAIT_TIMEOUT_SECONDS,
)

INSIGHT_REQUEST_QUEUE = RequestAdmissionQueue(
    name="insight.run",
    max_concurrency=INSIGHT_MAX_CONCURRENT_REQUESTS,
    max_pending=REQUEST_QUEUE_MAX_PENDING,
    wait_timeout_seconds=REQUEST_QUEUE_WAIT_TIMEOUT_SECONDS,
)

GRAPH_REQUEST_QUEUE = RequestAdmissionQueue(
    name="graph.run",
    max_concurrency=GRAPH_MAX_CONCURRENT_REQUESTS,
    max_pending=REQUEST_QUEUE_MAX_PENDING,
    wait_timeout_seconds=REQUEST_QUEUE_WAIT_TIMEOUT_SECONDS,
)

SANDBOX_REQUEST_QUEUE = RequestAdmissionQueue(
    name="sandbox.run",
    max_concurrency=SANDBOX_MAX_CONCURRENT_REQUESTS,
    max_pending=REQUEST_QUEUE_MAX_PENDING,
    wait_timeout_seconds=REQUEST_QUEUE_WAIT_TIMEOUT_SECONDS,
)


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


def _is_context_overflow_error(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(pattern in normalized for pattern in CONTEXT_OVERFLOW_ERROR_PATTERNS)


def _compress_stdin_payload(payload: str, max_chars: int, keep_head_chars: int) -> str:
    source = payload or ""
    if len(source) <= max_chars:
        return source

    marker = (
        "\n\n[message history compressed automatically by codex.serve "
        "because model context length was exceeded]\n\n"
    )
    budget = max(0, max_chars - len(marker))
    if budget <= 0:
        return source[:max_chars]

    head_budget = min(max(0, keep_head_chars), budget)
    tail_budget = max(0, budget - head_budget)

    if tail_budget == 0:
        compressed = source[:budget]
    else:
        head = source[:head_budget].rstrip()
        tail = source[-tail_budget:].lstrip()
        compressed = f"{head}{marker}{tail}"

    if len(compressed) > max_chars:
        compressed = compressed[:max_chars]
    return compressed


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


def _replace_model_args(args: List[str], model: str) -> List[str]:
    replaced_args: List[str] = []
    idx = 0
    replaced = False
    while idx < len(args):
        arg = args[idx]
        if arg in ("--model", "-m"):
            replaced_args.extend([arg, model])
            replaced = True
            idx += 2
            continue
        if arg.startswith("--model="):
            replaced_args.append(f"--model={model}")
            replaced = True
            idx += 1
            continue
        replaced_args.append(arg)
        idx += 1

    if not replaced:
        replaced_args.extend(["--model", model])
    return replaced_args


def _openclaw_env_value(req_env: Optional[Dict[str, str]], key: str, default: str = "") -> str:
    raw_value = (req_env or {}).get(key)
    if raw_value is None:
        raw_value = os.environ.get(key)
    if raw_value is None:
        raw_value = default
    return str(raw_value).strip()


def _resolve_docker_compose_command() -> List[str]:
    docker_path = shutil.which("docker")
    if docker_path:
        try:
            result = subprocess.run(
                [docker_path, "compose", "version"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if result.returncode == 0:
                return [docker_path, "compose"]
        except Exception:
            pass

    docker_compose_path = shutil.which("docker-compose")
    if docker_compose_path:
        return [docker_compose_path]

    raise HTTPException(
        status_code=500,
        detail=(
            "OpenClaw requires Docker Compose, but neither 'docker compose' nor "
            "'docker-compose' is available."
        ),
    )


def _resolve_openclaw_compose_paths(req_env: Optional[Dict[str, str]]) -> tuple[str, str]:
    compose_file = _openclaw_env_value(req_env, "OPENCLAW_COMPOSE_FILE")
    project_dir = _openclaw_env_value(req_env, "OPENCLAW_PROJECT_DIR")

    if project_dir:
        project_dir = os.path.abspath(os.path.expanduser(project_dir))

    if compose_file:
        compose_file = os.path.abspath(os.path.expanduser(compose_file))
    elif project_dir:
        compose_file = os.path.join(project_dir, "docker-compose.yml")

    if compose_file and not project_dir:
        project_dir = os.path.dirname(compose_file)

    return compose_file, project_dir


def _build_openclaw_compose_env(
    req_env: Optional[Dict[str, str]],
    project_dir: str,
) -> Dict[str, str]:
    compose_env = os.environ.copy()
    if req_env:
        compose_env.update({key: str(value) for key, value in req_env.items()})

    resolved_project_dir = project_dir or _openclaw_env_value(req_env, "OPENCLAW_PROJECT_DIR")
    if resolved_project_dir:
        resolved_project_dir = os.path.abspath(os.path.expanduser(resolved_project_dir))
    else:
        resolved_project_dir = "/workspace/openclaw"

    default_config_dir = os.path.join(resolved_project_dir, ".openclaw")
    default_workspace_dir = os.path.join(resolved_project_dir, "workspace")

    def _set_if_missing_or_blank(key: str, value: str) -> None:
        existing_value = compose_env.get(key)
        if existing_value is None or not str(existing_value).strip():
            compose_env[key] = value

    _set_if_missing_or_blank("OPENCLAW_PROJECT_DIR", resolved_project_dir)
    _set_if_missing_or_blank("OPENCLAWCONFIGDIR", default_config_dir)
    _set_if_missing_or_blank("OPENCLAWWORKSPACEDIR", default_workspace_dir)
    _set_if_missing_or_blank("OPENCLAWGATEWAYTOKEN", "openclaw-dev-token")

    for path_value in [default_config_dir, default_workspace_dir]:
        try:
            os.makedirs(path_value, exist_ok=True)
        except OSError:
            logger.warning("Failed to ensure OpenClaw runtime path exists: %s", path_value)

    return compose_env


def _write_openclaw_compose_env_file(compose_env: Dict[str, str]) -> str:
    env_keys = sorted(
        key
        for key in compose_env.keys()
        if key.startswith("OPENCLAW") or key == "DOCKER_HOST"
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".openclaw.env",
        delete=False,
    ) as env_file:
        for key in env_keys:
            value = str(compose_env.get(key, ""))
            escaped_value = value.replace("\\", "\\\\").replace("\n", "\\n")
            env_file.write(f"{key}={escaped_value}\n")
        return env_file.name


def _run_openclaw_compose_command(
    compose_command: List[str],
    compose_file: str,
    project_dir: str,
    extra_args: List[str],
    timeout_seconds: float,
    req_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    compose_env = _build_openclaw_compose_env(req_env, project_dir)
    env_file_path = _write_openclaw_compose_env_file(compose_env)

    command = list(compose_command)
    if project_dir:
        command.extend(["--project-directory", project_dir])
    command.extend(["--env-file", env_file_path])
    command.extend(["-f", compose_file])
    command.extend(extra_args)

    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout_seconds),
            env=compose_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                "OpenClaw Docker Compose command timed out: "
                + " ".join(command)
            ),
        ) from exc
    finally:
        try:
            os.unlink(env_file_path)
        except OSError:
            logger.warning("Failed to remove temporary OpenClaw env file: %s", env_file_path)


def _get_openclaw_compose_services(
    compose_command: List[str],
    compose_file: str,
    project_dir: str,
    req_env: Optional[Dict[str, str]] = None,
) -> List[str]:
    result = _run_openclaw_compose_command(
        compose_command,
        compose_file,
        project_dir,
        ["config", "--services"],
        timeout_seconds=30.0,
        req_env=req_env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "failed to list OpenClaw Compose services").strip()
        raise HTTPException(
            status_code=502,
            detail=f"Failed to inspect OpenClaw Docker Compose services: {detail}",
        )

    services: List[str] = []
    for line in (result.stdout or "").splitlines():
        service_name = line.strip()
        if service_name and service_name not in services:
            services.append(service_name)

    if services:
        return services

    raise HTTPException(
        status_code=502,
        detail="OpenClaw Docker Compose project does not define any services.",
    )


def _looks_like_openclaw_service_name(service_name: str) -> bool:
    normalized = (service_name or "").strip().lower()
    if not normalized:
        return False
    return (
        "openclaw" in normalized
        or normalized.endswith("-gateway")
        or normalized.endswith("_gateway")
        or normalized.endswith("-cli")
        or normalized.endswith("_cli")
    )


def _find_openclaw_compose_fallbacks(
    compose_file: str,
    project_dir: str,
) -> List[tuple[str, str]]:
    candidates: List[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add_candidate(candidate_file: str, candidate_project_dir: str) -> None:
        if not candidate_file:
            return
        absolute_file = os.path.abspath(os.path.expanduser(candidate_file))
        absolute_project_dir = os.path.abspath(os.path.expanduser(candidate_project_dir or os.path.dirname(absolute_file)))
        key = (absolute_file, absolute_project_dir)
        if key in seen or not os.path.isfile(absolute_file):
            return
        seen.add(key)
        candidates.append(key)

    if project_dir:
        _add_candidate(os.path.join(project_dir, "docker-compose.yml"), project_dir)
        _add_candidate(os.path.join(project_dir, "compose.yml"), project_dir)

    compose_dir = os.path.dirname(compose_file) if compose_file else ""
    if compose_dir:
        _add_candidate(os.path.join(compose_dir, "openclaw", "docker-compose.yml"), os.path.join(compose_dir, "openclaw"))
        _add_candidate(os.path.join(compose_dir, "openclaw", "compose.yml"), os.path.join(compose_dir, "openclaw"))

    return candidates


def _resolve_openclaw_compose_target(
    compose_command: List[str],
    compose_file: str,
    project_dir: str,
    req_env: Optional[Dict[str, str]] = None,
) -> tuple[str, str, List[str]]:
    available_services = _get_openclaw_compose_services(
        compose_command,
        compose_file,
        project_dir,
        req_env=req_env,
    )
    if any(_looks_like_openclaw_service_name(service) for service in available_services):
        return compose_file, project_dir, available_services

    for fallback_compose_file, fallback_project_dir in _find_openclaw_compose_fallbacks(compose_file, project_dir):
        if fallback_compose_file == compose_file and fallback_project_dir == project_dir:
            continue
        fallback_services = _get_openclaw_compose_services(
            compose_command,
            fallback_compose_file,
            fallback_project_dir,
            req_env=req_env,
        )
        if not any(_looks_like_openclaw_service_name(service) for service in fallback_services):
            continue

        logger.warning(
            "OpenClaw compose file '%s' did not expose OpenClaw services; using '%s' instead",
            compose_file,
            fallback_compose_file,
        )
        return fallback_compose_file, fallback_project_dir, fallback_services

    available = ", ".join(available_services)
    raise HTTPException(
        status_code=502,
        detail=(
            "Configured OpenClaw Compose project does not expose any OpenClaw services. "
            f"Compose file: {compose_file}. Available services: {available}. "
            "Set OPENCLAW_COMPOSE_FILE or OPENCLAW_PROJECT_DIR to the real OpenClaw project."
        ),
    )


def _resolve_openclaw_service_name(
    requested_service: str,
    available_services: List[str],
    fallback_candidates: List[str],
    purpose: str,
) -> str:
    normalized_to_actual = {service.strip().lower(): service for service in available_services if service.strip()}

    def _match_candidate(candidate: str) -> Optional[str]:
        normalized_candidate = (candidate or "").strip().lower()
        if not normalized_candidate:
            return None
        return normalized_to_actual.get(normalized_candidate)

    requested_actual = _match_candidate(requested_service)
    if requested_actual:
        return requested_actual

    candidate_order: List[str] = []
    for candidate in [requested_service, *fallback_candidates]:
        normalized_candidate = (candidate or "").strip()
        if normalized_candidate and normalized_candidate.lower() not in {
            item.lower() for item in candidate_order
        }:
            candidate_order.append(normalized_candidate)

    for candidate in candidate_order:
        matched = _match_candidate(candidate)
        if matched:
            if requested_service and candidate.lower() != requested_service.strip().lower():
                logger.warning(
                    "OpenClaw %s service '%s' was not found; using '%s' instead",
                    purpose,
                    requested_service,
                    matched,
                )
            return matched

    purpose_tokens = [token for token in fallback_candidates if token]
    heuristic_matches: List[str] = []
    for service in available_services:
        normalized_service = service.lower()
        if any(token.lower() in normalized_service for token in purpose_tokens):
            heuristic_matches.append(service)

    if len(heuristic_matches) == 1:
        matched = heuristic_matches[0]
        if requested_service:
            logger.warning(
                "OpenClaw %s service '%s' was not found; using detected service '%s'",
                purpose,
                requested_service,
                matched,
            )
        return matched

    available = ", ".join(available_services)
    requested_label = requested_service or "(unset)"
    raise HTTPException(
        status_code=502,
        detail=(
            f"OpenClaw {purpose} service '{requested_label}' was not found in the Compose project. "
            f"Available services: {available}"
        ),
    )


def _resolve_openclaw_tui_url(
    req_env: Optional[Dict[str, str]],
    gateway_service: str,
) -> str:
    configured_url = _openclaw_env_value(req_env, "OPENCLAW_TUI_URL")
    if configured_url:
        return configured_url

    gateway_port = _openclaw_env_value(req_env, "OPENCLAW_GATEWAY_PORT", "18789") or "18789"
    return f"ws://{gateway_service}:{gateway_port}"


def _ensure_openclaw_runtime_config(
    compose_command: List[str],
    compose_file: str,
    project_dir: str,
    req_env: Optional[Dict[str, str]],
    cli_service: str,
) -> None:
    config_updates: List[tuple[str, str]] = []

    tools_profile = _openclaw_env_value(req_env, "OPENCLAW_TOOLS_PROFILE", "full")
    if tools_profile:
        config_updates.append(("tools.profile", tools_profile))

    gateway_mode = _openclaw_env_value(req_env, "OPENCLAW_GATEWAY_MODE", "local")
    if gateway_mode:
        config_updates.append(("gateway.mode", gateway_mode))

    gateway_port = _openclaw_env_value(req_env, "OPENCLAW_GATEWAY_PORT", "18789")
    if gateway_port:
        config_updates.append(("gateway.port", gateway_port))

    gateway_bind = _openclaw_env_value(req_env, "OPENCLAW_GATEWAY_BIND", "lan")
    if gateway_bind:
        config_updates.append(("gateway.bind", gateway_bind))

    control_ui_allow_insecure_auth = _openclaw_env_value(
        req_env,
        "OPENCLAW_CONTROL_UI_ALLOW_INSECURE_AUTH",
    )
    if control_ui_allow_insecure_auth:
        config_updates.append(
            (
                "gateway.controlUi.allowInsecureAuth",
                control_ui_allow_insecure_auth.lower(),
            )
        )

    for config_path, config_value in config_updates:
        result = _run_openclaw_compose_command(
            compose_command,
            compose_file,
            project_dir,
            ["run", "-T", "--rm", cli_service, "config", "set", config_path, config_value],
            timeout_seconds=120.0,
            req_env=req_env,
        )
        if result.returncode == 0:
            continue

        detail = (result.stderr or result.stdout or "failed to update OpenClaw config").strip()
        raise HTTPException(
            status_code=502,
            detail=f"Failed to apply OpenClaw config '{config_path}': {detail}",
        )


def _ensure_openclaw_gateway_running(
    compose_command: List[str],
    compose_file: str,
    project_dir: str,
    req_env: Optional[Dict[str, str]],
    gateway_service: str,
) -> None:
    auto_start_enabled = _parse_bool(
        _openclaw_env_value(req_env, "OPENCLAW_AUTO_START_GATEWAY", "true"),
        True,
    )
    if not auto_start_enabled:
        return

    result = _run_openclaw_compose_command(
        compose_command,
        compose_file,
        project_dir,
        ["up", "-d", gateway_service],
        timeout_seconds=120.0,
        req_env=req_env,
    )
    if result.returncode == 0:
        return

    detail = (result.stderr or result.stdout or "failed to start openclaw-gateway").strip()
    raise HTTPException(
        status_code=502,
        detail=f"Failed to start OpenClaw gateway via Docker Compose: {detail}",
    )


def _build_openclaw_command(
    args: List[str],
    stdin_payload: str,
    req_env: Optional[Dict[str, str]],
    session_id: Optional[str],
) -> tuple[List[str], Dict[str, str]]:
    compose_file, project_dir = _resolve_openclaw_compose_paths(req_env)
    if not compose_file:
        raise HTTPException(
            status_code=400,
            detail=(
                "OpenClaw requires OPENCLAW_COMPOSE_FILE or OPENCLAW_PROJECT_DIR to point "
                "at the OpenClaw Docker Compose project."
            ),
        )

    if not os.path.isfile(compose_file):
        raise HTTPException(
            status_code=400,
            detail=f"OpenClaw compose file not found: {compose_file}",
        )

    popen_env = os.environ.copy()
    if req_env:
        popen_env.update(req_env)

    normalized_args = _strip_model_args(list(args))
    command = _resolve_docker_compose_command()
    compose_file, project_dir, available_services = _resolve_openclaw_compose_target(
        command,
        compose_file,
        project_dir,
        req_env=req_env,
    )
    cli_service = _resolve_openclaw_service_name(
        _openclaw_env_value(req_env, "OPENCLAW_CLI_SERVICE", "openclaw-cli") or "openclaw-cli",
        available_services,
        ["openclaw-cli", "openclaw", "cli"],
        "CLI",
    )
    gateway_service = _resolve_openclaw_service_name(
        _openclaw_env_value(req_env, "OPENCLAW_GATEWAY_SERVICE", "openclaw-gateway")
        or "openclaw-gateway",
        available_services,
        ["openclaw-gateway", "gateway"],
        "gateway",
    )
    _ensure_openclaw_runtime_config(command, compose_file, project_dir, req_env, cli_service)
    _ensure_openclaw_gateway_running(command, compose_file, project_dir, req_env, gateway_service)

    tui_url = _resolve_openclaw_tui_url(req_env, gateway_service)
    tui_token = _openclaw_env_value(req_env, "OPENCLAW_TUI_TOKEN")
    tui_password = _openclaw_env_value(req_env, "OPENCLAW_TUI_PASSWORD")

    if project_dir:
        command.extend(["--project-directory", project_dir])
    command.extend(["-f", compose_file, "run", "--rm", cli_service, "tui"])

    if tui_url:
        command.extend(["--url", tui_url])
    if tui_token:
        command.extend(["--token", tui_token])
    if tui_password:
        command.extend(["--password", tui_password])

    if session_id:
        command.extend(["--session", session_id])

    command.extend(normalized_args)
    command.extend(["--message", stdin_payload or ""])
    return command, popen_env


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _parse_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _get_model_aliases(model_id: str) -> List[str]:
    normalized = (model_id or "").strip()
    if not normalized:
        return []
    aliases = [normalized.lower()]
    if "/" in normalized:
        aliases.append(normalized.split("/", 1)[1].strip().lower())
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _merge_model_metadata(entry: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(entry)
    for key in ("model_info", "litellm_params"):
        section = entry.get(key)
        if isinstance(section, dict):
            merged.update(section)
    return merged


def _extract_litellm_model_metadata(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    records = payload.get("data")
    if not isinstance(records, list):
        records = payload.get("models")
    if not isinstance(records, list):
        return {}

    metadata_by_alias: Dict[str, Dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        model_id = (
            item.get("model_name")
            or item.get("id")
            or item.get("model")
            or item.get("name")
            or ""
        )
        if not isinstance(model_id, str):
            continue
        aliases = _get_model_aliases(model_id)
        if not aliases:
            continue
        metadata = _merge_model_metadata(item)
        for alias in aliases:
            metadata_by_alias[alias] = metadata

    return metadata_by_alias


def _score_model(metadata: Optional[Dict[str, Any]], preferred_order_index: int) -> tuple:
    if not metadata:
        return (0, float("-inf"), float("-inf"), -preferred_order_index)

    performance_score = _parse_float(metadata.get("performance_score"))
    quality_score = _parse_float(metadata.get("quality_score"))
    latency_ms = (
        _parse_float(metadata.get("latency_ms"))
        or _parse_float(metadata.get("avg_latency_ms"))
        or _parse_float(metadata.get("response_time_ms"))
        or _parse_float(metadata.get("latency"))
    )
    if latency_ms is not None and latency_ms > 0 and latency_ms < 10:
        latency_ms = latency_ms * 1000

    rpm = (
        _parse_float(metadata.get("rpm"))
        or _parse_float(metadata.get("max_rpm"))
        or _parse_float(metadata.get("rate_limit_rpm"))
    )
    tpm = (
        _parse_float(metadata.get("tpm"))
        or _parse_float(metadata.get("max_tpm"))
        or _parse_float(metadata.get("rate_limit_tpm"))
    )

    performance_value = performance_score if performance_score is not None else 0.0
    if quality_score is not None:
        performance_value += quality_score
    if latency_ms is not None and latency_ms > 0:
        performance_value += 1000.0 / latency_ms

    rate_limit_value = 0.0
    if rpm is not None and rpm > 0:
        rate_limit_value += math.log1p(rpm)
    if tpm is not None and tpm > 0:
        rate_limit_value += math.log1p(tpm)

    has_metadata = 1 if (latency_ms is not None or rpm is not None or tpm is not None or performance_score is not None or quality_score is not None) else 0
    return (has_metadata, performance_value, rate_limit_value, -preferred_order_index)


def _build_ssl_context(verify_ssl: bool, ca_bundle: Optional[str]):
    if verify_ssl:
        if ca_bundle:
            return ssl.create_default_context(cafile=ca_bundle)
        return ssl.create_default_context()

    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    return unverified


async def _fetch_litellm_model_metadata(base_url: str, api_key: str, verify_ssl: bool, ca_bundle: Optional[str]) -> Dict[str, Dict[str, Any]]:
    normalized_base = (base_url or "").strip().rstrip("/")
    if not normalized_base:
        return {}

    ssl_context = _build_ssl_context(verify_ssl, ca_bundle)
    endpoints = ["/model/info", "/v1/model/info", "/models", "/v1/models"]
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def _do_fetch() -> Dict[str, Dict[str, Any]]:
        for endpoint in endpoints:
            url = f"{normalized_base}{endpoint}"
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=8.0, context=ssl_context) as response:
                    status = getattr(response, "status", 200)
                    if status < 200 or status >= 300:
                        continue
                    body = response.read().decode("utf-8", errors="replace")
            except Exception:
                continue

            try:
                payload = json.loads(body) if body.strip() else {}
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            metadata = _extract_litellm_model_metadata(payload)
            if metadata:
                return metadata
        return {}

    return await asyncio.to_thread(_do_fetch)


async def _resolve_auto_model(args: List[str], req_env: Optional[Dict[str, str]]) -> Optional[str]:
    requested_model = (_extract_model_from_args(args) or "").strip().lower()
    if requested_model != "auto":
        return None

    candidate_models = [model for model in AGENT_MODEL if model.strip() and model.strip().lower() != "auto"]
    if not candidate_models:
        return None

    merged_env: Dict[str, str] = {}
    for key in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_SSL_VERIFY", "LITELLM_CA_BUNDLE"):
        value = os.environ.get(key)
        if value is not None:
            merged_env[key] = value
    merged_env.update(req_env or {})

    base_url = (merged_env.get("LITELLM_BASE_URL") or "").strip()
    api_key = (merged_env.get("LITELLM_API_KEY") or "").strip()
    verify_ssl = _parse_bool(merged_env.get("LITELLM_SSL_VERIFY"), False)
    ca_bundle = (merged_env.get("LITELLM_CA_BUNDLE") or "").strip() or None

    metadata_by_alias: Dict[str, Dict[str, Any]] = {}
    if base_url:
        metadata_by_alias = await _fetch_litellm_model_metadata(base_url, api_key, verify_ssl, ca_bundle)

    best_model = candidate_models[0]
    best_score = _score_model(None, preferred_order_index=0)

    for index, model in enumerate(candidate_models):
        model_metadata = None
        for alias in _get_model_aliases(model):
            if alias in metadata_by_alias:
                model_metadata = metadata_by_alias[alias]
                break
        score = _score_model(model_metadata, preferred_order_index=index)
        if score > best_score:
            best_score = score
            best_model = model

    return best_model


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


def _build_agent_command(
    agent: str,
    args: List[str],
    req_env: Optional[Dict[str, str]],
    stdin_payload: Optional[str] = None,
    session_id: Optional[str] = None,
) -> tuple[List[str], Dict[str, str]]:
    if agent == OPENCLAW_AGENT_NAME:
        return _build_openclaw_command(args, stdin_payload or "", req_env, session_id)

    popen_env = os.environ.copy()
    normalized_args = list(args)

    if DOCKER_IMAGE:
        command = ["docker", "run", "--rm", "-i"]
        docker_env = _build_docker_env(agent, normalized_args, req_env)

        if agent in ("opencode", "codex", "kimi"):
            normalized_args = _strip_model_args(normalized_args)

        for env_key, env_val in docker_env.items():
            command.extend(["-e", f"{env_key}={env_val}"])

        command.append(DOCKER_IMAGE)
        command.append(agent)
        command.extend(normalized_args)
        return command, popen_env

    command = [agent] + normalized_args
    if req_env:
        popen_env.update(req_env)
    return command, popen_env


def _team_specialist_agents() -> List[str]:
    specialists: List[str] = []
    for item in AGENT_LIST:
        normalized = (item or "").strip()
        if not normalized or normalized == TEAM_AGENT_NAME:
            continue
        if normalized not in specialists:
            specialists.append(normalized)
    return specialists


def _build_team_round1_prompt(user_prompt: str, role: str, agent_name: str) -> str:
    return (
        "You are participating in a multi-agent collaboration.\\n"
        f"Your role: {role}.\\n"
        f"Agent identity: {agent_name}.\\n"
        "Task: Solve the user's request with your role-specific strengths.\\n"
        "Output format:\\n"
        "1) Findings\\n"
        "2) Proposed answer\\n"
        "3) Uncertainty and checks\\n"
        "Keep the response concise but high signal.\\n\\n"
        "User request:\\n"
        f"{user_prompt}"
    )


def _build_team_round2_prompt(
    user_prompt: str,
    role: str,
    agent_name: str,
    round1_responses: Dict[str, str],
) -> str:
    response_lines: List[str] = []
    for peer_name, peer_response in round1_responses.items():
        if peer_name == agent_name:
            continue
        response_lines.append(f"[{peer_name}]\\n{peer_response.strip()}")

    peers_block = "\\n\\n".join(response_lines).strip() or "(No peer responses available)"
    return (
        "You are in round 2 of a multi-agent internal debate.\\n"
        f"Your role: {role}.\\n"
        f"Agent identity: {agent_name}.\\n"
        "Review peer proposals, challenge weak assumptions, and improve your prior solution.\\n"
        "Output format:\\n"
        "1) Critique of peers\\n"
        "2) Revised proposal\\n"
        "3) Confidence level and remaining risks\\n\\n"
        "Original user request:\\n"
        f"{user_prompt}\\n\\n"
        "Peer responses from round 1:\\n"
        f"{peers_block}"
    )


def _build_team_synthesis_prompt(
    user_prompt: str,
    coordinator_agent: str,
    round1_responses: Dict[str, str],
    round2_responses: Dict[str, str],
) -> str:
    round1_lines = [
        f"[{agent_name}]\\n{content.strip()}"
        for agent_name, content in round1_responses.items()
    ]
    round2_lines = [
        f"[{agent_name}]\\n{content.strip()}"
        for agent_name, content in round2_responses.items()
    ]

    round1_block = "\\n\\n".join(round1_lines).strip() or "(No round 1 responses available)"
    round2_block = "\\n\\n".join(round2_lines).strip() or "(No round 2 responses available)"

    return (
        "You are the synthesis coordinator in a multi-agent system.\\n"
        f"Coordinator agent identity: {coordinator_agent}.\\n"
        "Produce ONE final answer for the user.\\n"
        "Requirements:\\n"
        "- Merge the strongest ideas from all agents.\\n"
        "- Resolve conflicts explicitly when recommendations differ.\\n"
        "- Prioritize correctness, clear assumptions, and reduced hallucinations.\\n"
        "- Do not mention internal roles, debate rounds, or hidden chain-of-thought.\\n"
        "- Keep only the final user-facing answer.\\n\\n"
        "Original user request:\\n"
        f"{user_prompt}\\n\\n"
        "Round 1 specialist outputs:\\n"
        f"{round1_block}\\n\\n"
        "Round 2 debate outputs:\\n"
        f"{round2_block}"
    )


async def _execute_agent_once(
    agent: str,
    args: List[str],
    stdin_payload: str,
    req_env: Optional[Dict[str, str]],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    timeout_message = (
        "Request timed out while waiting for agent response "
        f"({RESPONSE_TIMEOUT_SECONDS}s)."
    )

    current_payload = stdin_payload
    compression_retried = False
    while True:
        command, popen_env = _build_agent_command(
            agent,
            args,
            req_env,
            stdin_payload=current_payload,
            session_id=session_id,
        )
        exit_code, stdout_text, stderr_text = await _run_subprocess_capture_with_stdin(
            command,
            stdin_text="" if agent == OPENCLAW_AGENT_NAME else current_payload,
            timeout=RESPONSE_TIMEOUT_SECONDS,
            timeout_message=timeout_message,
            env=popen_env,
        )

        should_retry_with_compression = (
            AUTO_COMPRESS_ON_CONTEXT_OVERFLOW
            and not compression_retried
            and exit_code != 0
            and _is_context_overflow_error(stderr_text)
        )
        if should_retry_with_compression:
            compressed_payload = _compress_stdin_payload(
                current_payload,
                AUTO_COMPRESS_MAX_CHARS,
                AUTO_COMPRESS_KEEP_HEAD_CHARS,
            )
            if compressed_payload != current_payload:
                current_payload = compressed_payload
                compression_retried = True
                continue

        return {
            "agent": agent,
            "exit_code": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "compressed_retry": compression_retried,
        }


async def _execute_team_collaboration(
    session_id: str,
    normalized_req_args: List[str],
    stdin_payload: str,
    req_env: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    await _assert_session_not_stopped(session_id)

    specialists = _team_specialist_agents()
    if not specialists:
        raise HTTPException(
            status_code=400,
            detail=(
                "Team mode requires at least one specialist agent in AGENT_LIST "
                f"besides '{TEAM_AGENT_NAME}'."
            ),
        )

    team_roles = [
        "coordinator",
        "research expert",
        "logic expert",
        "creative expert",
    ]

    role_by_agent: Dict[str, str] = {}
    for index, agent_name in enumerate(specialists):
        role_by_agent[agent_name] = team_roles[index % len(team_roles)]

    round1_tasks = [
        _execute_agent_once(
            agent_name,
            normalized_req_args,
            _build_team_round1_prompt(stdin_payload, role_by_agent[agent_name], agent_name),
            req_env,
            session_id=f"{session_id}-{agent_name}",
        )
        for agent_name in specialists
    ]
    round1_results = await asyncio.gather(*round1_tasks)
    await _assert_session_not_stopped(session_id)
    round1_stdout_map: Dict[str, str] = {
        result["agent"]: (result.get("stdout") or "").strip() for result in round1_results
    }

    round2_tasks = [
        _execute_agent_once(
            agent_name,
            normalized_req_args,
            _build_team_round2_prompt(
                stdin_payload,
                role_by_agent[agent_name],
                agent_name,
                round1_stdout_map,
            ),
            req_env,
            session_id=f"{session_id}-{agent_name}",
        )
        for agent_name in specialists
    ]
    round2_results = await asyncio.gather(*round2_tasks)
    await _assert_session_not_stopped(session_id)
    round2_stdout_map: Dict[str, str] = {
        result["agent"]: (result.get("stdout") or "").strip() for result in round2_results
    }

    coordinator_agent = specialists[0]
    synthesis_result = await _execute_agent_once(
        coordinator_agent,
        normalized_req_args,
        _build_team_synthesis_prompt(
            stdin_payload,
            coordinator_agent,
            round1_stdout_map,
            round2_stdout_map,
        ),
        req_env,
        session_id=f"{session_id}-{coordinator_agent}",
    )

    return {
        "specialists": specialists,
        "roles": role_by_agent,
        "round1": round1_results,
        "round2": round2_results,
        "synthesis": synthesis_result,
        "coordinator": coordinator_agent,
    }


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


async def _is_team_session_active(sessionId: str) -> bool:
    async with SESSIONS_LOCK:
        return sessionId in TEAM_RUN_SESSIONS


async def _register_team_session(sessionId: str) -> None:
    async with SESSIONS_LOCK:
        TEAM_RUN_SESSIONS.add(sessionId)


async def _unregister_team_session(sessionId: str) -> None:
    async with SESSIONS_LOCK:
        TEAM_RUN_SESSIONS.discard(sessionId)
        STOP_REQUESTED_SESSIONS.discard(sessionId)


async def _assert_session_not_stopped(sessionId: str) -> None:
    stopped = await _consume_stop_requested(sessionId)
    if stopped:
        raise asyncio.CancelledError("Session stopped via API.")


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
    env: Optional[Dict[str, str]] = None,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
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


async def _run_subprocess_capture_with_stdin(
    command: List[str],
    stdin_text: str,
    timeout: Optional[float] = None,
    timeout_message: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    try:
        payload = (stdin_text or "").encode("utf-8")
        if timeout is not None:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(payload), timeout=timeout)
        else:
            stdout_bytes, stderr_bytes = await process.communicate(payload)
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


def _extract_nested_detail_text(value) -> str:
    current = value
    for _ in range(3):
        if isinstance(current, dict) and "detail" in current:
            current = current.get("detail")
            continue
        if isinstance(current, str):
            stripped = current.strip()
            if not stripped:
                return ""
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    current = json.loads(stripped)
                    continue
                except Exception:
                    return stripped
            return stripped
        if current is None:
            return ""
        if isinstance(current, (list, dict)):
            try:
                return json.dumps(current, ensure_ascii=False)
            except Exception:
                return str(current)
        return str(current).strip()
    return str(current).strip() if current is not None else ""


def _normalize_graph_upstream_error(status_code: int, response_body: str) -> str:
    raw = (response_body or "").strip()
    if not raw:
        return f"codex.graph returned status {status_code} with an empty response body"

    detail = ""
    try:
        parsed = json.loads(raw)
        detail = _extract_nested_detail_text(parsed)
    except Exception:
        detail = _extract_nested_detail_text(raw)

    normalized = (detail or "").strip()
    if not normalized:
        return (
            f"codex.graph returned status {status_code} with an empty error detail. "
            "Check codex.graph backend logs and LiteLLM configuration."
        )

    lowered = normalized.lower()
    if lowered == "analysis failed:" or lowered == "analysis failed":
        return (
            f"codex.graph analysis failed (status {status_code}) with no root-cause detail. "
            "Check codex.graph backend logs and LiteLLM configuration."
        )

    return normalized


def _build_sandbox_script(command_text: str, cwd_value: Optional[str], env_map: Dict[str, str]) -> str:
    lines: List[str] = ["#!/usr/bin/env bash", "set -euo pipefail"]

    if cwd_value:
        lines.append(f"cd {shlex.quote(cwd_value)}")

    for key in sorted(env_map.keys()):
        normalized_key = (key or "").strip()
        if not normalized_key:
            continue
        value = env_map[key]
        lines.append(f"export {normalized_key}={shlex.quote(value)}")

    lines.append(command_text)
    return "\n".join(lines) + "\n"


def _infer_framework_hint(file_paths: List[str]) -> Optional[str]:
    extension_to_framework = {
        "py": "python",
        "java": "java",
        "kt": "kotlin",
        "kts": "kotlin",
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "go": "go",
        "rs": "rust",
        "c": "c",
        "h": "c",
        "cc": "cpp",
        "cpp": "cpp",
        "cxx": "cpp",
        "hpp": "cpp",
        "cs": "csharp",
        "swift": "swift",
        "php": "php",
        "rb": "ruby",
    }
    for raw_path in file_paths or []:
        path = (raw_path or "").strip().lower()
        if not path or "." not in path:
            continue
        ext = path.rsplit(".", 1)[-1].strip()
        if ext in extension_to_framework:
            return extension_to_framework[ext]
    return None


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


async def _probe_sandbox_upstream(base_url: str, timeout_seconds: float) -> Dict[str, Any]:
    probe_timeout = max(1.0, min(timeout_seconds, 5.0))
    health_url = f"{base_url}/health"
    runtimes_url = f"{base_url}/api/v2/runtimes"

    result: Dict[str, Any] = {
        "base_url": base_url,
        "health_ok": False,
        "runtimes_ok": False,
        "health_status": None,
        "runtimes_status": None,
        "health_error": "",
        "runtimes_error": "",
    }

    try:
        health_status, health_body = await _get_json(health_url, timeout_seconds=probe_timeout)
        result["health_status"] = health_status
        result["health_ok"] = 200 <= health_status < 300
        logger.info(
            "sandbox upstream health probe: url=%s status=%s body_len=%s",
            health_url,
            health_status,
            len(health_body or ""),
        )
    except Exception as exc:
        result["health_error"] = str(exc)
        logger.warning("sandbox upstream health probe failed: url=%s error=%s", health_url, exc)

    try:
        runtimes_status, runtimes_body = await _get_json(runtimes_url, timeout_seconds=probe_timeout)
        result["runtimes_status"] = runtimes_status
        result["runtimes_ok"] = 200 <= runtimes_status < 300
        logger.info(
            "sandbox upstream runtimes probe: url=%s status=%s body_len=%s",
            runtimes_url,
            runtimes_status,
            len(runtimes_body or ""),
        )
    except Exception as exc:
        result["runtimes_error"] = str(exc)
        logger.warning("sandbox upstream runtimes probe failed: url=%s error=%s", runtimes_url, exc)

    return result


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
    for env_key in (
        "GEMINI_API_KEY",
        "LITELLM_BASE_URL",
        "LITELLM_API_KEY",
        "LITELLM_SSL_VERIFY",
        "LITELLM_CA_BUNDLE",
    ):
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
                detail=start_stderr.strip() or "Failed to start codex.graph backend via docker run",
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
    team_session_active = await _is_team_session_active(normalizedSessionId)
    if process is None and not team_session_active:
        raise HTTPException(status_code=404, detail=f"Session not found or already finished: {normalizedSessionId}")

    await _mark_stop_requested(normalizedSessionId)
    if process is not None:
        await _terminate_process(process)
    return {
        "sessionId": normalizedSessionId,
        "status": "stopped",
    }


@app.post("/insight/run", response_model=InsightRunResponse)
async def run_insight(req: InsightRunRequest):
    queue_lease = await INSIGHT_REQUEST_QUEUE.acquire()
    try:
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
    finally:
        queue_lease.release()


@app.post("/graph/run", response_model=GraphRunResponse)
async def run_graph(req: GraphRunRequest):
    queue_lease = await GRAPH_REQUEST_QUEUE.acquire()
    try:
        code = (req.code or "").strip()
        file_paths = [item.strip() for item in (req.file_paths or []) if (item or "").strip()]

        if not code:
            raise HTTPException(status_code=400, detail="code is required")
        if not file_paths:
            raise HTTPException(status_code=400, detail="file_paths is required")

        framework_hint = (req.framework_hint or "").strip() or (_infer_framework_hint(file_paths) or "")
        analyze_payload: Dict[str, Any] = {
            "code": code,
            "file_paths": file_paths,
        }
        if framework_hint:
            analyze_payload["framework_hint"] = framework_hint
        if req.metadata is not None:
            analyze_payload["metadata"] = req.metadata
        if req.http_connections is not None:
            analyze_payload["http_connections"] = req.http_connections
        analyze_payload_json = json.dumps(analyze_payload, ensure_ascii=False)

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

        timeout_message = (
            "Request timed out while waiting for codex.graph response "
            f"({GRAPH_RESPONSE_TIMEOUT_SECONDS}s)."
        )

        max_attempts = GRAPH_ANALYZE_MAX_RETRIES + 1
        for attempt in range(1, max_attempts + 1):
            try:
                graph_command = ["docker", "run", "--rm", "-i"]
                for env_key, env_val in graph_env.items():
                    graph_command.extend(["-e", f"{env_key}={env_val}"])
                graph_command.append(CODEX_GRAPH_IMAGE)
                graph_command.extend([
                    "analyze",
                    "--request-json", "-",
                    "--pretty",
                ])
                response_code, response_body, response_stderr = await _run_subprocess_capture_with_stdin(
                    graph_command,
                    stdin_text=analyze_payload_json,
                    timeout=GRAPH_RESPONSE_TIMEOUT_SECONDS,
                    timeout_message=timeout_message,
                )
            except (TimeoutError, socket.timeout) as exc:
                if attempt < max_attempts:
                    await asyncio.sleep(GRAPH_ANALYZE_RETRY_DELAY_SECONDS)
                    continue
                raise HTTPException(status_code=504, detail=timeout_message) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Failed to call codex.graph: {exc}") from exc

            if response_code == 0:
                break

            should_retry = attempt < max_attempts
            if should_retry and attempt < max_attempts:
                await asyncio.sleep(GRAPH_ANALYZE_RETRY_DELAY_SECONDS)
                continue

            detail_body = (response_stderr or "").strip() or (response_body or "").strip()
            if not detail_body:
                detail_body = "codex.graph CLI container exited with no output"
            detail = f"codex.graph CLI execution failed (exit {response_code}): {detail_body}"
            if attempt > 1:
                detail = f"{detail} (after {attempt} attempts)"
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
    finally:
        queue_lease.release()


@app.post("/sandbox/run", response_model=SandboxRunResponse)
async def run_sandbox(req: SandboxRunRequest):
    queue_lease = await SANDBOX_REQUEST_QUEUE.acquire()
    try:
        command_text = (req.command or "").strip()
        if not command_text:
            raise HTTPException(status_code=400, detail="command is required")

        cwd_value = (req.cwd or "").strip() or None
        requested_timeout_seconds = req.timeoutSeconds
        timeout_seconds = requested_timeout_seconds
        if timeout_seconds is None or timeout_seconds <= 0:
            timeout_seconds = SANDBOX_RUN_TIMEOUT_SECONDS

        if SANDBOX_HARD_TIMEOUT_SECONDS > 0 and timeout_seconds > SANDBOX_HARD_TIMEOUT_SECONDS:
            logger.info(
                "sandbox.run timeout capped: requested=%s effective=%s",
                timeout_seconds,
                SANDBOX_HARD_TIMEOUT_SECONDS,
            )
            timeout_seconds = SANDBOX_HARD_TIMEOUT_SECONDS

        requested_env: Dict[str, str] = {}
        if req.env:
            for key, value in req.env.items():
                normalized_key = (key or "").strip()
                if not normalized_key:
                    continue
                requested_env[normalized_key] = "" if value is None else str(value)

        script_content = _build_sandbox_script(command_text, cwd_value, requested_env)
        timeout_ms = max(1, int(math.ceil(timeout_seconds * 1000.0)))

        payload = {
            "language": "bash",
            "version": "*",
            "files": [
                {
                    "name": "main.sh",
                    "content": script_content,
                }
            ],
            "stdin": "",
            "compile_timeout": timeout_ms,
            "run_timeout": timeout_ms,
            "compile_cpu_time": timeout_ms,
            "run_cpu_time": timeout_ms,
        }

        logger.info(
            "sandbox.run request: requested_timeout_seconds=%s timeout_seconds=%s cwd=%s candidates=%s command_preview=%s",
            requested_timeout_seconds,
            timeout_seconds,
            cwd_value,
            SANDBOX_BASE_URL_CANDIDATES,
            command_text[:200],
        )

        last_status_code: Optional[int] = None
        last_response_body = ""
        connection_errors: List[str] = []
        probe_summaries: List[str] = []

        for sandbox_base_url in SANDBOX_BASE_URL_CANDIDATES:
            upstream_url = f"{sandbox_base_url}/api/v2/execute"

            probe_result = await _probe_sandbox_upstream(sandbox_base_url, timeout_seconds=timeout_seconds)
            probe_summary = (
                f"{sandbox_base_url} health={probe_result.get('health_status')}"
                f" runtimes={probe_result.get('runtimes_status')}"
            )
            probe_summaries.append(probe_summary)
            logger.info("sandbox upstream probe summary: %s", probe_summary)

            try:
                status_code, response_body = await _post_json(
                    upstream_url,
                    payload,
                    timeout_seconds=timeout_seconds,
                )
                last_status_code = status_code
                last_response_body = response_body
                logger.info(
                    "sandbox execute response: upstream=%s status=%s body_len=%s",
                    upstream_url,
                    status_code,
                    len(response_body or ""),
                )
            except (TimeoutError, socket.timeout):
                logger.warning(
                    "sandbox execute timed out: upstream=%s timeout_seconds=%s",
                    upstream_url,
                    timeout_seconds,
                )
                return SandboxRunResponse(
                    stdout="",
                    stderr=(
                        "Request timed out while waiting for sandbox response "
                        f"({timeout_seconds}s)."
                    ),
                    exit_code=124,
                    command=command_text,
                    timed_out=True,
                    timeout_seconds=timeout_seconds,
                )
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                connection_errors.append(f"{upstream_url}: {reason}")
                logger.warning(
                    "sandbox execute connection failed: upstream=%s error=%s",
                    upstream_url,
                    reason,
                )
                continue

            if not (200 <= status_code < 300):
                detail = _normalize_graph_upstream_error(status_code, response_body)
                raise HTTPException(
                    status_code=502,
                    detail=f"codex-sandbox request failed: {detail}",
                )

            try:
                parsed = json.loads(response_body) if response_body.strip() else {}
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=502, detail="Invalid JSON response from codex-sandbox") from exc

            run_result = parsed.get("run") if isinstance(parsed, dict) else None
            if not isinstance(run_result, dict):
                raise HTTPException(status_code=502, detail="Invalid /execute response from codex-sandbox: missing run")

            stdout_text = str(run_result.get("stdout") or "")
            stderr_text = str(run_result.get("stderr") or "")

            run_status = str(run_result.get("status") or "")
            run_message = str(run_result.get("message") or "")
            timed_out = run_status == "TO"

            code_value = run_result.get("code")
            if isinstance(code_value, int):
                exit_code = code_value
            elif timed_out:
                exit_code = 124
            else:
                exit_code = 1

            if run_message:
                if stderr_text:
                    stderr_text = f"{stderr_text.rstrip()}\n{run_message}".strip()
                else:
                    stderr_text = run_message

            return SandboxRunResponse(
                stdout=stdout_text,
                stderr=stderr_text,
                exit_code=exit_code,
                command=command_text,
                timed_out=timed_out,
                timeout_seconds=timeout_seconds,
            )

        if connection_errors:
            logger.error(
                "sandbox upstream connection failures: probes=%s errors=%s",
                probe_summaries,
                connection_errors,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "codex-sandbox connection failed. "
                    + " ; ".join(connection_errors)
                    + " ; probes="
                    + " | ".join(probe_summaries)
                    + " ; set SANDBOX_BASE_URL to a reachable codex-sandbox endpoint."
                ),
            )

        if last_status_code is not None:
            detail = _normalize_graph_upstream_error(last_status_code, last_response_body)
            logger.error(
                "sandbox upstream request failed: status=%s detail=%s probes=%s",
                last_status_code,
                detail,
                probe_summaries,
            )
            raise HTTPException(
                status_code=502,
                detail=f"codex-sandbox request failed: {detail}",
            )

        logger.error("sandbox upstream unreachable: probes=%s", probe_summaries)
        raise HTTPException(
            status_code=502,
            detail="codex-sandbox request failed: no reachable upstream endpoints",
        )
    finally:
        queue_lease.release()

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

    if req.agent == TEAM_AGENT_NAME and await _is_team_session_active(sessionId):
        raise HTTPException(status_code=409, detail=f"Session is already running: {sessionId}")

    normalized_req_args = list(req.args)
    auto_selected_model = await _resolve_auto_model(normalized_req_args, req.env)
    if auto_selected_model:
        normalized_req_args = _replace_model_args(normalized_req_args, auto_selected_model)

    queue_lease = await AGENT_REQUEST_QUEUE.acquire()

    async def stream_generator():
        yield json.dumps({"type": "session", "id": sessionId}) + "\n"

        if req.agent == TEAM_AGENT_NAME:
            try:
                await _register_team_session(sessionId)
                yield json.dumps(
                    {
                        "type": "stderr",
                        "data": "Team mode enabled. Running multi-agent collaboration in parallel.\n",
                    }
                ) + "\n"
                team_result = await _execute_team_collaboration(
                    session_id=sessionId,
                    normalized_req_args=normalized_req_args,
                    stdin_payload=stdin_payload,
                    req_env=req.env,
                )

                for round_key in ("round1", "round2"):
                    for item in team_result.get(round_key, []):
                        agent_name = item.get("agent") or "unknown"
                        exit_code = item.get("exit_code", 1)
                        retry_note = " with compressed-retry" if item.get("compressed_retry") else ""
                        yield json.dumps(
                            {
                                "type": "stderr",
                                "data": (
                                    f"[team/{round_key}] {agent_name} finished with exit {exit_code}{retry_note}.\n"
                                ),
                            }
                        ) + "\n"

                synthesis = team_result.get("synthesis") or {}
                synthesis_stdout = (synthesis.get("stdout") or "").strip()
                synthesis_stderr = (synthesis.get("stderr") or "").strip()
                synthesis_code = synthesis.get("exit_code", 1)

                if synthesis_stderr:
                    yield json.dumps(
                        {
                            "type": "stderr",
                            "data": f"[team/synthesis] {synthesis_stderr}\n",
                        }
                    ) + "\n"

                if synthesis_stdout:
                    if not synthesis_stdout.endswith("\n"):
                        synthesis_stdout += "\n"
                    yield json.dumps(
                        {
                            "type": "stdout",
                            "data": synthesis_stdout,
                        }
                    ) + "\n"

                yield json.dumps({"type": "exit", "code": synthesis_code}) + "\n"
            except asyncio.CancelledError:
                yield json.dumps({"type": "stderr", "data": "Session stopped via API.\n"}) + "\n"
                yield json.dumps({"type": "exit", "code": 0}) + "\n"
            except asyncio.TimeoutError:
                timeout_msg = (
                    "Request timed out while waiting for team response "
                    f"({RESPONSE_TIMEOUT_SECONDS}s)."
                )
                yield json.dumps({"type": "stderr", "data": timeout_msg}) + "\n"
                yield json.dumps({"type": "exit", "code": 124}) + "\n"
            except HTTPException as http_error:
                detail = str(http_error.detail) if http_error.detail is not None else "Team execution failed"
                yield json.dumps({"type": "stderr", "data": f"{detail}\n"}) + "\n"
                yield json.dumps({"type": "exit", "code": 1}) + "\n"
            except Exception as error:
                yield json.dumps(
                    {"type": "stderr", "data": f"Internal Server Error: {str(error)}"}
                ) + "\n"
                yield json.dumps({"type": "exit", "code": 1}) + "\n"
            finally:
                await _unregister_team_session(sessionId)
                queue_lease.release()
            return

        stdin_payload_current = stdin_payload
        compression_retried = False

        try:
            while True:
                read_tasks = []
                process = None
                stderr_events: List[str] = []
                try:
                    command, popen_env = _build_agent_command(
                        req.agent,
                        normalized_req_args,
                        req.env,
                        stdin_payload=stdin_payload_current,
                        session_id=sessionId,
                    )
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=popen_env
                    )
                    await _register_session(sessionId, process)

                    if stdin_payload_current and req.agent != OPENCLAW_AGENT_NAME:
                        process.stdin.write(stdin_payload_current.encode())
                        await process.stdin.drain()
                    process.stdin.close()

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
                            await queue.put(None)

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
                            continue

                        if item.get("type") == "stderr":
                            data = item.get("data")
                            if isinstance(data, str) and data:
                                stderr_events.append(data)
                                if len(stderr_events) > 40:
                                    stderr_events = stderr_events[-40:]

                        yield json.dumps(item) + "\n"

                    exit_code = await _await_with_deadline(process.wait(), deadline)
                    stopped_by_api = await _consume_stop_requested(sessionId)
                    if stopped_by_api:
                        yield json.dumps({"type": "stderr", "data": "Session stopped via API.\n"}) + "\n"
                        yield json.dumps({"type": "exit", "code": 0}) + "\n"
                        break

                    stderr_summary = "".join(stderr_events)
                    should_retry_with_compression = (
                        AUTO_COMPRESS_ON_CONTEXT_OVERFLOW
                        and not compression_retried
                        and exit_code != 0
                        and _is_context_overflow_error(stderr_summary)
                    )

                    if should_retry_with_compression:
                        compressed_payload = _compress_stdin_payload(
                            stdin_payload_current,
                            AUTO_COMPRESS_MAX_CHARS,
                            AUTO_COMPRESS_KEEP_HEAD_CHARS,
                        )
                        if compressed_payload != stdin_payload_current:
                            compression_retried = True
                            stdin_payload_current = compressed_payload
                            yield json.dumps(
                                {
                                    "type": "stderr",
                                    "data": (
                                        "Detected model context overflow. "
                                        "Retrying once with compressed message history.\n"
                                    ),
                                }
                            ) + "\n"
                            continue

                    yield json.dumps({"type": "exit", "code": exit_code}) + "\n"
                    break

                except asyncio.TimeoutError:
                    if process is not None:
                        await _terminate_process(process)
                    timeout_msg = (
                        "Request timed out while waiting for agent response "
                        f"({RESPONSE_TIMEOUT_SECONDS}s)."
                    )
                    yield json.dumps({"type": "stderr", "data": timeout_msg}) + "\n"
                    yield json.dumps({"type": "exit", "code": 124}) + "\n"
                    break

                except Exception as e:
                    error_data = {"type": "stderr", "data": f"Internal Server Error: {str(e)}"}
                    yield json.dumps(error_data) + "\n"
                    yield json.dumps({"type": "exit", "code": 1}) + "\n"
                    break

                finally:
                    for task in read_tasks:
                        if not task.done():
                            task.cancel()
                    if read_tasks:
                        await asyncio.gather(*read_tasks, return_exceptions=True)
                    await _unregister_session(sessionId, process)

        finally:
            queue_lease.release()

    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
