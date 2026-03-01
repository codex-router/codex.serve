# codex.serve

HTTP server implementation for the Codex Gerrit plugin. This service exposes a REST API to execute supported AI agents remotely, decoupling the execution environment from the Gerrit server.

## Features

- Exposes a `POST /agent/run` endpoint to execute agent commands.
- Exposes a `POST /insight/run` endpoint to execute `codex-insight` Docker jobs and return generated insight pages.
- Exposes a `POST /graph/run` endpoint to execute `codex.graph` CLI image using `analyze --request-json -` (stdin payload).
- Supports in-memory request queueing with bounded pending requests and per-endpoint concurrency limits for `/agent/run`, `/insight/run`, and `/graph/run`.
- Supports on-demand `docker run --rm -i` execution of `codex.graph` CLI image for each `/graph/run` request.
- Exposes a `POST /sessions/{sessionId}/stop` endpoint to stop an active `/agent/run` session.
- Exposes a `GET /models` endpoint to return model IDs from `AGENT_MODEL`.
- Exposes a `GET /agents` endpoint to list supported agent names.
- Supports streaming output via newline-delimited JSON (NDJSON).
- Supports a configurable agent allowlist via `AGENT_LIST`.
- Handles environment variable propagation (e.g., LiteLLM config).
- Supports automatic message-history compression and one-time retry when `/agent/run` fails due to model context overflow.
- Supports optional `contextFiles` in `POST /agent/run` to prepend referenced file contents into agent stdin context.
- Each `contextFiles` item is a typed `ContextFileItem` object supporting `content` (plain text) or `base64Content` (base64-encoded bytes) for flexible file attachment, including binary and non-UTF-8 files.

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
| `AGENT_MODEL` | *(empty)* | Returned model IDs for `GET /models` (comma-separated). Include `auto` to enable server-side auto selection for `POST /agent/run` when request args contain `--model auto`. |
| `LITELLM_BASE_URL` | *(unset)* | Default LiteLLM base URL passed to execution container in Docker mode |
| `LITELLM_API_KEY` | *(unset)* | Default LiteLLM API key passed to execution container in Docker mode |
| `LITELLM_SSL_VERIFY` | `false` | Default TLS verification behavior for LiteLLM calls (`false` supports self-signed certificates) |
| `LITELLM_CA_BUNDLE` | *(unset)* | Optional CA bundle path for LiteLLM TLS verification |
| `LITELLM_MODEL` | *(unset)* | Default model for `POST /agent/run`, and for `POST /insight/run` when using a custom `CODEX_INSIGHT_IMAGE` |
| `INSIGHT_MODEL` | *(unset)* | Default model used for `POST /insight/run` when `CODEX_INSIGHT_IMAGE` is `craftslab/codex-insight:latest` (mapped to container `LITELLM_MODEL`) |
| `RUN_RESPONSE_TIMEOUT_SECONDS` | *(unset)* | Optional timeout (seconds) for `POST /agent/run`; `<= 0`, empty, or invalid disables timeout |
| `CODEX_INSIGHT_IMAGE` | `craftslab/codex-insight:latest` | Docker image used by `POST /insight/run` |
| `INSIGHT_RESPONSE_TIMEOUT_SECONDS` | *(unset)* | Optional timeout (seconds) for `POST /insight/run`; `<= 0`, empty, or invalid disables timeout |
| `GRAPH_MODEL` | *(unset)* | Default graph model for `POST /graph/run` (mapped to forwarded payload env `LITELLM_MODEL`; falls back to `LITELLM_MODEL` when unset) |
| `CODEX_GRAPH_IMAGE` | `craftslab/codex-graph-cli:latest` | Docker image used by `POST /graph/run` for direct `main.py analyze` execution |
| `GRAPH_RESPONSE_TIMEOUT_SECONDS` | *(unset)* | Optional timeout (seconds) for `POST /graph/run`; `<= 0`, empty, or invalid disables timeout |
| `REQUEST_QUEUE_MAX_PENDING` | `100` | Max pending requests allowed per queued API before returning `503` |
| `REQUEST_QUEUE_WAIT_TIMEOUT_SECONDS` | *(unset)* | Optional max wait time in queue before returning `503`; empty/invalid/`<= 0` disables queue wait timeout |
| `AGENT_MAX_CONCURRENT_REQUESTS` | `4` | Max concurrently executing requests for `POST /agent/run` |
| `INSIGHT_MAX_CONCURRENT_REQUESTS` | `2` | Max concurrently executing requests for `POST /insight/run` |
| `GRAPH_MAX_CONCURRENT_REQUESTS` | `4` | Max concurrently executing requests for `POST /graph/run` |
| `AUTO_COMPRESS_ON_CONTEXT_OVERFLOW` | `true` | Automatically compresses oversized `/agent/run` stdin/history and retries once when stderr indicates model context overflow |
| `AUTO_COMPRESS_MAX_CHARS` | `24000` | Target max character budget for compressed `/agent/run` stdin payload |
| `AUTO_COMPRESS_KEEP_HEAD_CHARS` | `6000` | Head segment character budget preserved before tail content during automatic compression |

### Docker Mode

To run the agents inside a Docker container (e.g. built from `codex.agent/Dockerfile`), set the `CODEX_AGENT_IMAGE` environment variable.

```bash
export CODEX_AGENT_IMAGE=my-codex-image:latest
python codex_serve.py
```

When enabled:
1. `codex.serve` calls `docker run --rm -i ...` for every request.
2. `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `LITELLM_SSL_VERIFY`, and `LITELLM_CA_BUNDLE` are inherited from `codex.serve` runtime env and passed via `-e` flags.
3. `AGENT_PROVIDER_NAME` is automatically set from the requested `agent`.
4. Request `env` values are optional and can override inherited defaults.
5. `LITELLM_MODEL` is inferred from `--model`/`-m` args when not explicitly provided.
6. When request args use `--model auto`, `codex.serve` selects the best model from `AGENT_MODEL` (excluding `auto`) using LiteLLM model metadata (performance and rate-limit signals) fetched from `LITELLM_BASE_URL` with `LITELLM_API_KEY`.
7. The `agent` value is used as the executable name inside the execution container.
8. If `codex.serve` itself runs in Docker, mount `/var/run/docker.sock` so it can start sibling containers.

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
- Configures `CODEX_INSIGHT_IMAGE` to `craftslab/codex-insight:latest` for insight generation requests.
- Uses `CODEX_GRAPH_IMAGE` (`craftslab/codex-graph-cli:latest`) for `POST /graph/run` with stdin payload:
  - `analyze --request-json - --pretty`
  - Request fields (`code`, `file_paths`, `framework_hint`, optional `metadata`, optional `http_connections`) are serialized to JSON and passed via stdin.
- Supports `GRAPH_MODEL` for `POST /graph/run` payload env forwarding as `LITELLM_MODEL`.
- Supports `LITELLM_SSL_VERIFY` (default `false`) and optional `LITELLM_CA_BUNDLE` for LiteLLM/self-signed cert scenarios.
- Sets `RUN_RESPONSE_TIMEOUT_SECONDS` in [docker-compose.yml](docker-compose.yml) (default `300`) to bound `POST /agent/run` response time in container deployments.

See [docker-compose.yml](docker-compose.yml) for details.

### Smoke Test (Docker Mode)

To verify Docker mode end-to-end (including `CODEX_AGENT_IMAGE`), run:

```bash
./test.sh
```

This test now validates:
- The agent image built from `codex.agent/Dockerfile` is Ubuntu-based and all supported agents are callable.
- A `codex.serve` container built from this module's `Dockerfile` can execute `POST /agent/run` requests by launching the configured `CODEX_AGENT_IMAGE`.

### Example Script (`example.sh`)

To run a local end-to-end demo (`/agent/run`, `/insight/run`, `/graph/run`), start the server first and then run:

```bash
./example.sh
```

You can override the target server with:

```bash
BASE_URL="http://localhost:8000" ./example.sh
```

The script sends `POST /agent/run` with:
- `agent: "codex"`
- `args: ["--model", "auto"]`
- one text `contextFiles` item (`content`) and one base64 item (`base64Content`)
- a generated `sessionId` in the form `demo-<timestamp>`

Then it sends:
- `POST /insight/run` using files collected from `REPO_PATH` (default: current directory)
- `POST /graph/run` with a minimal payload (`code`, `file_paths`, `framework_hint`)

Supported overrides:
- `BASE_URL` (default `http://localhost:8000`)
- `REPO_PATH` (default current working directory)
- `OUT_PATH` (default `/tmp/codex-serve-example-out-<timestamp>`)
- `DRY_RUN` for `/insight/run` (default `false`)
- `GRAPH_MODEL` (mapped to graph request `env.LITELLM_MODEL` when set)
- `LITELLM_SSL_VERIFY` and `LITELLM_CA_BUNDLE` (forwarded in request env)

Expected output is NDJSON containing `session`, streamed `stdout`/`stderr`, and a final `exit` object.
If response timeout is configured server-side and reached, the stream may end with `{"type":"exit","code":124}`.

For `POST /graph/run`, success returns JSON containing `graph`, `usage`, and `cost`.
A minimal smoke-test payload may return an empty graph (`nodes: []`, `edges: []`) while still indicating a successful end-to-end execution.

## API

### `GET /models`

Returns model IDs from `AGENT_MODEL`.

If `AGENT_MODEL` is unset, the default is `[]`.

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
  "agents": ["codex", "opencode", "qwen", "kimi"],
  "count": 4
}
```

### `POST /agent/run`

Executes a agent command.

**Request Body:**

```json
{
  "agent": "codex",
  "args": ["--model", "gpt-4"],
  "stdin": "Prompt text...",
  "sessionId": "optional-client-session-id",
  "contextFiles": [
    {
      "path": "test.c",
      "content": "#include <stdio.h>\\nint main(){printf(\"Hello World\\\\n\");}"
    },
    {
      "path": "logo.png",
      "base64Content": "iVBORw0KGgoAAAANSUhEUgAA..."
    }
  ]
}
```

`env` is optional. In Docker mode, `LITELLM_BASE_URL` and `LITELLM_API_KEY` are read from `codex.serve` process env by default.

If `args` contains `--model auto` (or `-m auto`) and `AGENT_MODEL` includes `auto`, the server resolves `auto` to a concrete model from `AGENT_MODEL` (excluding `auto`) before execution.

`contextFiles` is optional:
- Each item must include `path` and at least one of `content` or `base64Content`.
- `content` — plain UTF-8 text content of the file.
- `base64Content` — base64-encoded file bytes (decoded as UTF-8 with replacement characters). Useful for binary or non-UTF-8 files. Takes precedence over `content` when both are provided.
- When present, `codex.serve` prepends a clearly delimited file-context block to `stdin` before running the agent.
- Server-side limits cap number of files and per-file content size for safety.

`sessionId` is optional:
- If provided, it is used as the session identifier.
- If omitted, `codex.serve` generates a UUID session ID.
- If a session with the same ID is already running, `POST /agent/run` returns `409`.

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

Stops an active `/agent/run` session process.

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

If queueing is enabled and the request queue is saturated (pending limit reached) or queue wait timeout is exceeded, this endpoint returns `503`.

If `AUTO_COMPRESS_ON_CONTEXT_OVERFLOW=true` and the initial run fails with a model context-overflow style error (for example max context length / token limit exceeded), `codex.serve` automatically compresses the stdin message history and retries once. The stream includes a stderr notice before retrying.

### `POST /insight/run`

Runs `codex-insight` in Docker using the same invocation style documented in `codex.insight/README.md`.

`files` contains the selected repository files uploaded by client UI. `codex.serve` creates a temporary repository directory, writes uploaded files into it, then runs `codex-insight` in Docker.

The uploaded `path` values are treated as repository-relative paths.

Execution shape:

```text
docker create --name <container> \
  -e LITELLM_BASE_URL=... \
  -e LITELLM_API_KEY=... \
  -e LITELLM_MODEL=... \
  <CODEX_INSIGHT_IMAGE> \
  --repo /tmp/codex-repo \
  --out /tmp/codex-out [other optional flags]

docker cp <uploaded-repo-dir>/. <container>:/tmp/codex-repo
docker start -a <container>
docker cp <container>:/tmp/codex-out/. <server-temp-out-dir>
docker rm -f <container>
```

When `CODEX_INSIGHT_IMAGE` is `craftslab/codex-insight:latest` (or `codex-insight:latest`), `codex.serve` resolves that `LITELLM_MODEL` value from `INSIGHT_MODEL` instead of `LITELLM_MODEL`.

`outPath` is optional:
- If provided, generated insight files are persisted to that host path and returned in `outputDir`.
- If omitted, `codex.serve` creates a server-side temp output directory, persists generated files there, and returns that path in `outputDir`.

**Request Body:**

```json
{
  "files": [
    {
      "path": "src/main.py",
      "base64Content": "<base64-file-content>"
    },
    {
      "path": "README.md",
      "content": "# project"
    }
  ],
  "include": ["src/**"],
  "exclude": ["**/third_party/**"],
  "maxFilesPerModule": 40,
  "maxCharsPerFile": 10000,
  "dryRun": false,
  "env": {
    "LITELLM_BASE_URL": "https://litellm.com/v1",
    "LITELLM_API_KEY": "<your-api-key>",
    "INSIGHT_MODEL": "ollama-gemini-3-flash-preview"
  }
}
```

`files` is required.
`outPath` is optional.

`env` is optional and can override `LITELLM_BASE_URL`, `LITELLM_API_KEY`, and model selection inherited from `codex.serve`:
- For `craftslab/codex-insight:latest` (or `codex-insight:latest`), set `INSIGHT_MODEL`.
- For other custom `CODEX_INSIGHT_IMAGE` values, set `LITELLM_MODEL`.

**Response (success or tool failure):**

```json
{
  "stdout": "...",
  "stderr": "...",
  "exit_code": 0,
  "outputDir": "/path/to/insight",
  "files": [
    {
      "path": "System-Architecture.md",
      "content": "# System Architecture\n..."
    }
  ],
  "count": 1
}
```

- `files` contains top-level generated Markdown files from `outputDir` when `exit_code` is `0`.
- If timeout is configured via `INSIGHT_RESPONSE_TIMEOUT_SECONDS` and reached, endpoint returns `504`.
- If queueing is enabled and the request queue is saturated (pending limit reached) or queue wait timeout is exceeded, endpoint returns `503`.

### `POST /graph/run`

Proxies graph generation to `codex.graph` backend API:

```text
POST /graph/run
```


For each `/graph/run` request, codex.serve writes the code to a temp file, mounts it into the container, and runs:

```bash
docker run --rm -v <code-file>:/tmp/code.txt:ro \
  -e LITELLM_BASE_URL=... -e LITELLM_API_KEY=... -e LITELLM_MODEL=... \
  craftslab/codex-graph-cli:latest \
  analyze --code-file /tmp/code.txt --file-path <file> --framework-hint <hint> --pretty
```

All arguments are passed via CLI, not stdin. No `--request-json` is used. See [codex.graph/README_codex.graph.md](../codex.graph/README_codex.graph.md) for details.

If queueing is enabled and the request queue is saturated (pending limit reached) or queue wait timeout is exceeded, endpoint returns `503`.

