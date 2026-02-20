# codex.serve

HTTP server implementation for the Codex Gerrit plugin. This service exposes a REST API to execute supported AI agents remotely, decoupling the execution environment from the Gerrit server.

## Features

- Exposes a `POST /run` endpoint to execute agent commands.
- Exposes a `POST /sessions/{sessionId}/stop` endpoint to stop an active `/run` session.
- Exposes a `GET /models` endpoint to return model IDs from `MODEL_LIST`.
- Exposes a `GET /agents` endpoint to list supported agent names.
- Exposes a `POST /workspace/sync` endpoint to write patchset files to a local workspace path.
- Supports streaming output via newline-delimited JSON (NDJSON).
- Supports a configurable agent allowlist via `AGENT_LIST`.
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

The server reads supported agents from `AGENT_LIST` (comma-separated). In local mode, the selected agent name is executed directly from `PATH`.

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_LIST` | `codex` | Supported agent names (comma-separated) |
| `MODEL_LIST` | *(empty)* | Returned model IDs for `GET /models` (comma-separated) |
| `LITELLM_BASE_URL` | *(unset)* | Default LiteLLM base URL passed to execution container in Docker mode |
| `LITELLM_API_KEY` | *(unset)* | Default LiteLLM API key passed to execution container in Docker mode |
| `RUN_RESPONSE_TIMEOUT_SECONDS` | *(unset)* | Optional timeout (seconds) for `POST /run`; `<= 0`, empty, or invalid disables timeout |

### Docker Mode

To run the agents inside a Docker container (e.g. built from `codex.agent/Dockerfile`), set the `CODEX_AGENT_IMAGE` environment variable.

```bash
export CODEX_AGENT_IMAGE=my-codex-image:latest
python codex_serve.py
```

When enabled:
1. `codex.serve` calls `docker run --rm -i ...` for every request.
2. `LITELLM_BASE_URL` and `LITELLM_API_KEY` are inherited from `codex.serve` runtime env and passed via `-e` flags.
3. `AGENT_PROVIDER_NAME` is automatically set from the requested `agent`.
4. Request `env` values are optional and can override inherited defaults.
5. `LITELLM_MODEL` is inferred from `--model`/`-m` args when not explicitly provided.
6. The `agent` value is used as the executable name inside the execution container.
7. If `codex.serve` itself runs in Docker, mount `/var/run/docker.sock` so it can start sibling containers.

## Usage

### Run with Python

Start the server locally:

```bash
python codex_serve.py
```

The server will start on `http://0.0.0.0:8000`.

### Run with Docker Compose (Recommended)

To run `codex.serve` in a container while orchestrating the AI agent environment, use Docker Compose. This setup uses the "Sibling Containers" pattern, allowing the verified server container to spawn execution containers on the host Docker daemon.

1.  Build the agent environment image (see `codex.agent/README.md`).
2.  Start the service:

```bash
docker-compose up --build
```

This configuration:
- Builds/Runs `codex.serve` (defined in `Dockerfile`) which has the Docker client installed.
- Mounts the host's Docker socket (`/var/run/docker.sock`) so it can spawn sibling containers.
- Configures `CODEX_AGENT_IMAGE` to `craftslab/codex-agent:latest` for executing agents safely. The server container will spawn this image for each request.
- Sets `RUN_RESPONSE_TIMEOUT_SECONDS` in [docker-compose.yml](docker-compose.yml) (default `300`) to bound `POST /run` response time in container deployments.

See [docker-compose.yml](docker-compose.yml) for details.

### Smoke Test (Docker Mode)

To verify Docker mode end-to-end (including `CODEX_AGENT_IMAGE`), run:

```bash
./test.sh
```

This test now validates:
- The agent image built from `codex.agent/Dockerfile` is Ubuntu-based and all supported agents are callable.
- A `codex.serve` container built from this module's `Dockerfile` can execute `POST /run` requests by launching the configured `CODEX_AGENT_IMAGE`.

## API

### `GET /models`

Returns model IDs from `MODEL_LIST`.

If `MODEL_LIST` is unset, the default is `[]`.

**Example:**

```bash
curl "http://localhost:8000/models"
```

**Response:**

```json
{
  "models": [],
  "count": 0
}
```

### `GET /agents`

Returns the supported agent names from `AGENT_LIST`.

**Example:**

```bash
curl "http://localhost:8000/agents"
```

**Response:**

```json
{
  "agents": ["codex"],
  "count": 1
}
```

### `POST /run`

Executes a agent command.

**Request Body:**

```json
{
  "agent": "codex",
  "args": ["--model", "gpt-4"],
  "stdin": "Prompt text...",
  "sessionId": "optional-client-session-id"
}
```

`env` is optional. In Docker mode, `LITELLM_BASE_URL` and `LITELLM_API_KEY` are read from `codex.serve` process env by default.

`sessionId` is optional:
- If provided, it is used as the session identifier.
- If omitted, `codex.serve` generates a UUID session ID.
- If a session with the same ID is already running, `POST /run` returns `409`.

**Response:**

The response is a stream of newline-delimited JSON objects (NDJSON).

```json
{"type": "session", "id": "optional-client-session-id-or-generated-uuid"}
{"type": "stdout", "data": "partial output..."}
{"type": "stderr", "data": "log message..."}
{"type": "stdout", "data": "more output..."}
{"type": "exit", "code": 0}
```

### `POST /sessions/{sessionId}/stop`

Stops an active `/run` session process.

**Example:**

```bash
curl -X POST "http://localhost:8000/sessions/my-session/stop"
```

**Response (200):**

```json
{
  "sessionId": "my-session",
  "status": "stopped"
}
```

If the session does not exist or has already finished, this endpoint returns `404`.

If `RUN_RESPONSE_TIMEOUT_SECONDS` is configured and the timeout is reached before the process completes, the stream ends with:

```json
{"type": "stderr", "data": "Request timed out while waiting for agent response (...s)."}
{"type": "exit", "code": 124}
```

### `POST /workspace/sync`

Writes files into a local workspace directory on the machine running `codex.serve`.

**Request Body:**

```json
{
  "workspaceRoot": "/home/user/repo",
  "files": [
    {
      "path": "src/main/App.java",
      "contentBase64": "SGVsbG8="
    }
  ]
}
```

**Response:**

```json
{
  "written": 1
}
```
