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

@app.post("/run")
async def run_cli(req: RunRequest):
    if req.cli not in CLI_PATHS:
        raise HTTPException(status_code=400, detail=f"Unsupported CLI: {req.cli}")

    popen_env = os.environ.copy()

    if DOCKER_IMAGE:
        # Run inside Docker
        command = ["docker", "run", "--rm", "-i"]

        # Pass environment variables
        if req.env:
            for k, v in req.env.items():
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
