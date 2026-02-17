import os
import asyncio
import json
import ssl
import urllib.request
import urllib.error
import urllib.parse
from typing import List, Optional, Dict
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()

class RunRequest(BaseModel):
    cli: str
    args: List[str]
    stdin: str
    env: Optional[Dict[str, str]] = None

class RunResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int

# Configuration for paths (could be env vars)
CLI_PATHS = {
    "claude": os.environ.get("CLAUDE_PATH", "claude"),
    "codex": os.environ.get("CODEX_PATH", "codex"),
    "gemini": os.environ.get("GEMINI_PATH", "gemini"),
    "opencode": os.environ.get("OPENCODE_PATH", "opencode"),
    "qwen": os.environ.get("QWEN_PATH", "qwen"),
}

# Optional Docker configuration
DOCKER_IMAGE = os.environ.get("CODEX_DOCKER_IMAGE")


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _build_model_urls(base_url: str) -> List[str]:
    normalized = base_url.rstrip("/")
    urls = []
    path = urllib.parse.urlparse(normalized).path.rstrip("/")

    if normalized.endswith("/models"):
        urls.append(normalized)
    elif normalized.endswith("/v1"):
        urls.append(_join_url(normalized, "/models"))
    else:
        urls.append(_join_url(normalized, "/models"))
        urls.append(_join_url(normalized, "/v1/models"))
        # Some OpenAI-compatible gateways expose models under /openai/models.
        if path in ("", "/"):
            urls.append(_join_url(normalized, "/openai/models"))
            urls.append(_join_url(normalized, "/openai/v1/models"))

    # Deduplicate while preserving order.
    return list(dict.fromkeys(urls))


def _extract_model_from_args(args: List[str]) -> Optional[str]:
    for idx, arg in enumerate(args):
        if arg in ("--model", "-m"):
            if idx + 1 < len(args):
                model = args[idx + 1].strip()
                return model or None
            return None
    return None


def _build_docker_env(cli: str, args: List[str], req_env: Optional[Dict[str, str]]) -> Dict[str, str]:
    docker_env = dict(req_env or {})

    # Required by codex.docker entrypoint for provider-specific env mapping.
    docker_env["CLI_PROVIDER_NAME"] = cli

    # Use only LITELLM_BASE_URL for base URL configuration.
    base_url = docker_env.get("LITELLM_BASE_URL")
    if base_url:
        docker_env["LITELLM_BASE_URL"] = base_url

    # If no explicit model env provided, infer from common CLI flags.
    if not docker_env.get("LITELLM_MODEL"):
        inferred_model = _extract_model_from_args(args)
        if inferred_model:
            docker_env["LITELLM_MODEL"] = inferred_model

    return docker_env


async def _fetch_litellm_models(url: str, api_key: Optional[str]) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": "codex-serve/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        # Keep compatibility with providers that expect these key headers.
        headers["api-key"] = api_key
        headers["x-api-key"] = api_key

    req = urllib.request.Request(url, headers=headers, method="GET")

    ssl_context = ssl.create_default_context()
    try:
        import certifi

        ssl_context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    def _do_request() -> dict:
        with urllib.request.urlopen(req, timeout=15, context=ssl_context) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body)

    return await asyncio.to_thread(_do_request)


@app.get("/models")
async def get_models():
    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")

    if not base_url:
        raise HTTPException(
            status_code=400,
            detail="Missing LiteLLM base URL. Set LITELLM_BASE_URL."
        )

    candidate_urls = _build_model_urls(base_url)

    payload = None
    last_error = None
    last_http_status = None

    for url in candidate_urls:
        try:
            payload = await _fetch_litellm_models(url, api_key)
            break
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {err.code} from {url}: {err_body}"
            last_http_status = err.code
        except urllib.error.URLError as err:
            last_error = f"Failed to reach {url}: {err.reason}"
        except json.JSONDecodeError:
            last_error = f"Invalid JSON from {url}"
        except Exception as err:
            last_error = f"Error from {url}: {str(err)}"

    if payload is None:
        if last_http_status is not None and 400 <= last_http_status < 500:
            raise HTTPException(status_code=last_http_status, detail=last_error or "Upstream rejected request")
        raise HTTPException(status_code=502, detail=last_error or "Failed to fetch models")

    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raw_models = payload if isinstance(payload, list) else []

    model_ids = []
    for item in raw_models:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                model_ids.append(model_id)
        elif isinstance(item, str):
            model_ids.append(item)

    return {
        "models": sorted(set(model_ids)),
        "count": len(set(model_ids)),
    }


@app.get("/clis")
async def get_clis():
    clis = sorted(CLI_PATHS.keys())
    return {
        "clis": clis,
        "count": len(clis),
    }

@app.post("/run")
async def run_cli(req: RunRequest):
    if req.cli not in CLI_PATHS:
        raise HTTPException(status_code=400, detail=f"Unsupported CLI: {req.cli}")

    popen_env = os.environ.copy()

    if DOCKER_IMAGE:
        # Run inside Docker
        command = ["docker", "run", "--rm", "-i"]

        docker_env = _build_docker_env(req.cli, req.args, req.env)
        for k, v in docker_env.items():
            command.extend(["-e", f"{k}={v}"])

        command.append(DOCKER_IMAGE)
        # Use simple CLI name inside container (matches Dockerfile symlinks)
        command.append(req.cli)
        command.extend(req.args)
    else:
        # Run locally
        executable = CLI_PATHS[req.cli]
        command = [executable] + req.args
        if req.env:
            popen_env.update(req.env)

    async def stream_generator():
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=popen_env
            )

            # Write stdin
            if req.stdin:
                process.stdin.write(req.stdin.encode())
                await process.stdin.drain()
            process.stdin.close()

            # Queue to aggregate chunks from both stdout and stderr
            queue = asyncio.Queue()

            async def read_stream(stream, type_label):
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    await queue.put({"type": type_label, "data": chunk.decode('utf-8', errors='replace')})
                # Signal this stream is done
                await queue.put(None)

            # Start reading tasks
            asyncio.create_task(read_stream(process.stdout, "stdout"))
            asyncio.create_task(read_stream(process.stderr, "stderr"))

            active_streams = 2
            while active_streams > 0:
                item = await queue.get()
                if item is None:
                    active_streams -= 1
                else:
                    yield json.dumps(item) + "\n"

            exit_code = await process.wait()
            yield json.dumps({"type": "exit", "code": exit_code}) + "\n"

        except Exception as e:
            error_data = {"type": "stderr", "data": f"Internal Server Error: {str(e)}"}
            yield json.dumps(error_data) + "\n"
            yield json.dumps({"type": "exit", "code": 1}) + "\n"

    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
