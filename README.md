# codex.serve

HTTP server implementation for the Codex Gerrit plugin. This service exposes a REST API to execute supported AI CLIs (Codex, Claude, Gemini, OpenCode, Qwen) remotely, decoupling the execution environment from the Gerrit server.

## Features

- Exposes a `POST /run` endpoint to execute CLI commands.
- Exposes a `GET /models` endpoint to fetch available LiteLLM models.
- Exposes a `GET /clis` endpoint to list supported CLI names.
- Supports streaming output via newline-delimited JSON (NDJSON).
- Supports all CLIs used by `codex.gerrit`.
- Handles environment variable propagation (e.g., LiteLLM config).

## Requirements

- Python 3.8+
- `pip`

## Installation

### Local Python Installation

1. Install dependencies:

```bash
pip install -r requirements.txt
```

### Docker Build

You can build the server image using the provided script:

```bash
./build.sh
```

This creates the image `craftslab/codex-serve:latest`.

## Configuration

The server looks for CLI executables in the system PATH by default. You can override specific CLI paths using environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_PATH` | `claude` | Path to Claude CLI |
| `CODEX_PATH` | `codex` | Path to Codex CLI |
| `GEMINI_PATH` | `gemini` | Path to Gemini CLI |
| `OPENCODE_PATH` | `opencode` | Path to OpenCode CLI |
| `QWEN_PATH` | `qwen` | Path to Qwen CLI |
| `LITELLM_API_BASE` | *(unset)* | Default LiteLLM base URL used by `GET /models` (alias of `LITELLM_BASE_URL`) |
| `LITELLM_BASE_URL` | *(unset)* | Default LiteLLM base URL used by `GET /models` (alias of `LITELLM_API_BASE`) |
| `LITELLM_API_KEY` | *(unset)* | Default LiteLLM API key used by `GET /models` when query param is omitted |

### Docker Mode

To run the CLIs inside a Docker container (e.g. built from `codex.docker/Dockerfile`), set the `CODEX_DOCKER_IMAGE` environment variable.

```bash
export CODEX_DOCKER_IMAGE=my-codex-image:latest
python codex_serve.py
```

When enabled:
1. `codex.serve` calls `docker run --rm -i ...` for every request.
2. Environment variables from the request (like `LITELLM_API_KEY`) are passed via `-e` flags.
3. `CLI_PROVIDER_NAME` is automatically set from the requested `cli`.
4. `LITELLM_BASE_URL` and `LITELLM_API_BASE` are normalized as aliases (if either is provided, both are passed).
5. `LITELLM_MODEL` is inferred from `--model`/`-m` args when not explicitly provided.
6. The paths configured in `CODEX_PATH` etc. refer to paths *inside* the execution container.
7. If `codex.serve` itself runs in Docker, mount `/var/run/docker.sock` so it can start sibling containers.

## Usage

### Run with Python

Start the server locally:

```bash
python codex_serve.py
```

The server will start on `http://0.0.0.0:8000`.

### Run with Docker Compose (Recommended)

To run `codex.serve` in a container while orchestrating the AI CLI environment, use Docker Compose. This setup uses the "Sibling Containers" pattern, allowing the verified server container to spawn execution containers on the host Docker daemon.

1.  Build the CLI environment image (see `codex.docker/README.md`).
2.  Start the service:

```bash
docker-compose up --build
```

This configuration:
- Builds/Runs `codex.serve` (defined in `Dockerfile`) which has the Docker client installed.
- Mounts the host's Docker socket (`/var/run/docker.sock`) so it can spawn sibling containers.
- Configures `CODEX_DOCKER_IMAGE` to `craftslab/codex-cli-env:latest` for executing CLIs safely. The server container will spawn this image for each request.

See [docker-compose.yml](docker-compose.yml) for details.

### Smoke Test (Docker Mode)

To verify Docker mode end-to-end (including `CODEX_DOCKER_IMAGE`), run:

```bash
./test.sh
```

This test now validates:
- The CLI image built from `codex.docker/Dockerfile` is Ubuntu-based and all supported CLIs are callable.
- A `codex.serve` container built from this module's `Dockerfile` can execute `POST /run` requests by launching the configured `CODEX_DOCKER_IMAGE`.

## API

### `GET /models`

Fetches available model IDs from LiteLLM.

The server tries both `<base>/models` and `<base>/v1/models`.

This endpoint uses environment configuration only:

- `LITELLM_API_BASE` or `LITELLM_BASE_URL` (required; either one)
- `LITELLM_API_KEY` (optional)

**Example:**

```bash
curl "http://localhost:8000/models"
```

**Response:**

```json
{
  "models": ["gpt-4", "claude-3-sonnet"],
  "count": 2
}
```

If LiteLLM cannot be reached or returns invalid data, the endpoint returns `502`.
If both `LITELLM_API_BASE` and `LITELLM_BASE_URL` are unset, it returns `400`.

### `GET /clis`

Returns the supported CLI names based on the keys in server-side `CLI_PATHS`.

**Example:**

```bash
curl "http://localhost:8000/clis"
```

**Response:**

```json
{
  "clis": ["claude", "codex", "gemini", "opencode", "qwen"],
  "count": 5
}
```

### `POST /run`

Executes a CLI command.

**Request Body:**

```json
{
  "cli": "codex",
  "args": ["--model", "gpt-4"],
  "stdin": "Prompt text...",
  "env": {
    "LITELLM_BASE_URL": "..."
  }
}
```

**Response:**

The response is a stream of newline-delimited JSON objects (NDJSON).

```json
{"type": "stdout", "data": "partial output..."}
{"type": "stderr", "data": "log message..."}
{"type": "stdout", "data": "more output..."}
{"type": "exit", "code": 0}
```
