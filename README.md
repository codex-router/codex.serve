# codex.serve

HTTP server implementation for the Codex Gerrit plugin. This service exposes a REST API to execute supported AI CLIs remotely, decoupling the execution environment from the Gerrit server.

## Features

- Exposes a `POST /run` endpoint to execute CLI commands.
- Exposes a `GET /models` endpoint to return model IDs from `MODEL_LIST`.
- Exposes a `GET /clis` endpoint to list supported CLI names.
- Supports streaming output via newline-delimited JSON (NDJSON).
- Supports a configurable CLI allowlist via `CLI_LIST`.
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

The server reads supported CLIs from `CLI_LIST` (comma-separated). In local mode, the selected CLI name is executed directly from `PATH`.

| Variable | Default | Description |
|----------|---------|-------------|
| `CLI_LIST` | `codex` | Supported CLI names (comma-separated) |
| `MODEL_LIST` | *(empty)* | Returned model IDs for `GET /models` (comma-separated) |
| `LITELLM_BASE_URL` | *(unset)* | Passed through to execution container when provided in request env |
| `LITELLM_API_KEY` | *(unset)* | Passed through to execution container when provided in request env |

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
4. `LITELLM_BASE_URL` is passed through to the execution container when provided.
5. `LITELLM_MODEL` is inferred from `--model`/`-m` args when not explicitly provided.
6. The `cli` value is used as the executable name inside the execution container.
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

Returns model IDs from `MODEL_LIST`.

If `MODEL_LIST` is unset, the default is an empty list.

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

### `GET /clis`

Returns the supported CLI names from `CLI_LIST`.

**Example:**

```bash
curl "http://localhost:8000/clis"
```

**Response:**

```json
{
  "clis": ["codex"],
  "count": 1
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
