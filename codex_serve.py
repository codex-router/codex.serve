import os
import asyncio
import json
import codecs
from typing import List, Optional, Dict
from uuid import uuid4
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()

class RunRequest(BaseModel):
    agent: str
    args: List[str]
    stdin: str
    env: Optional[Dict[str, str]] = None
    sessionId: Optional[str] = None

class RunResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int

# Supported agent providers, configurable via env (comma-separated)
DEFAULT_AGENT_LIST = ["codex"]
AGENT_LIST = [
    agent.strip()
    for agent in os.environ.get("AGENT_LIST", ",".join(DEFAULT_AGENT_LIST)).split(",")
    if agent.strip()
]

# Optional Docker configuration
DOCKER_IMAGE = os.environ.get("CODEX_AGENT_IMAGE")

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

@app.post("/run")
async def run_agent(req: RunRequest):
    if req.agent not in AGENT_LIST:
        raise HTTPException(status_code=400, detail=f"Unsupported agent: {req.agent}")

    normalized_session_id = req.sessionId.strip() if req.sessionId else ""
    sessionId = normalized_session_id if normalized_session_id else str(uuid4())

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
            if req.stdin:
                process.stdin.write(req.stdin.encode())
                await process.stdin.drain()
            process.stdin.close()

            # Queue to aggregate chunks from both stdout and stderr
            queue = asyncio.Queue()

            async def read_stream(stream, type_label):
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                buffer = ""

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

                # Signal this stream is done
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
