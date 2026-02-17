import os
import asyncio
import json
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

# Supported CLI providers, configurable via env (comma-separated)
DEFAULT_CLI_LIST = ["codex"]
CLI_LIST = [
    cli.strip()
    for cli in os.environ.get("CLI_LIST", ",".join(DEFAULT_CLI_LIST)).split(",")
    if cli.strip()
]

# Optional Docker configuration
DOCKER_IMAGE = os.environ.get("CODEX_DOCKER_IMAGE")

DEFAULT_MODEL_LIST = []
MODEL_LIST = [
    model.strip()
    for model in os.environ.get("MODEL_LIST", ",".join(DEFAULT_MODEL_LIST)).split(",")
    if model.strip()
]


def _extract_model_from_args(args: List[str]) -> Optional[str]:
    for idx, arg in enumerate(args):
        if arg in ("--model", "-m"):
            if idx + 1 < len(args):
                model = args[idx + 1].strip()
                return model or None
            return None
    return None


def _build_docker_env(cli: str, args: List[str], req_env: Optional[Dict[str, str]]) -> Dict[str, str]:
    docker_env: Dict[str, str] = {}

    # Default LiteLLM settings from codex.serve runtime env (e.g., docker-compose).
    for env_key in ("LITELLM_BASE_URL", "LITELLM_API_KEY"):
        env_val = os.environ.get(env_key)
        if env_val:
            docker_env[env_key] = env_val

    # Request env can optionally override defaults.
    docker_env.update(req_env or {})

    # Required by codex.docker entrypoint for provider-specific env mapping.
    docker_env["CLI_PROVIDER_NAME"] = cli

    # If no explicit model env provided, infer from common CLI flags.
    if not docker_env.get("LITELLM_MODEL"):
        inferred_model = _extract_model_from_args(args)
        if inferred_model:
            docker_env["LITELLM_MODEL"] = inferred_model

    return docker_env


@app.get("/models")
async def get_models():
    return {
        "models": MODEL_LIST,
        "count": len(MODEL_LIST),
    }


@app.get("/clis")
async def get_clis():
    clis = sorted(set(CLI_LIST))
    return {
        "clis": clis,
        "count": len(clis),
    }

@app.post("/run")
async def run_cli(req: RunRequest):
    if req.cli not in CLI_LIST:
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
        executable = req.cli
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
