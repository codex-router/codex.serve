#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOCKER_DIR="${ROOT_DIR}/codex.docker"

CLI_IMAGE_TAG="codex-cli-env:test"
SERVE_IMAGE_TAG="codex-serve:test"
SERVE_CONTAINER_NAME="codex-serve-test"
SERVE_PORT="18000"

cleanup() {
	docker rm -f "${SERVE_CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[1/4] Building CLI Docker image from codex.docker: ${CLI_IMAGE_TAG}"
docker build -t "${CLI_IMAGE_TAG}" -f "${DOCKER_DIR}/Dockerfile" "${DOCKER_DIR}"

echo "[2/4] Running CLI image smoke tests in container"
docker run --rm "${CLI_IMAGE_TAG}" bash -lc '
set -euo pipefail

echo "- Verifying Ubuntu base image"
if ! grep -qi "^ID=ubuntu" /etc/os-release; then
	echo "Expected Ubuntu base image, but /etc/os-release is:"
	cat /etc/os-release
	exit 1
fi

echo "- Verifying required CLI binaries"
for cmd in claude codex gemini opencode qwen; do
	command -v "$cmd" >/dev/null
	"$cmd" --version >/dev/null
done

echo "- Verifying configured CLI path env vars"
for path_var in CLAUDE_PATH CODEX_PATH GEMINI_PATH OPENCODE_PATH QWEN_PATH; do
	value="${!path_var}"
	[ -n "$value" ]
	[ -x "$value" ]
done

echo "CLI image smoke tests passed."
'

echo "[3/4] Building codex.serve Docker image: ${SERVE_IMAGE_TAG}"
docker build -t "${SERVE_IMAGE_TAG}" -f "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}"

echo "[4/4] Running codex.serve with CODEX_DOCKER_IMAGE=${CLI_IMAGE_TAG}"
docker run -d \
	--name "${SERVE_CONTAINER_NAME}" \
	-p "${SERVE_PORT}:8000" \
	-v /var/run/docker.sock:/var/run/docker.sock \
	-e CODEX_DOCKER_IMAGE="${CLI_IMAGE_TAG}" \
	"${SERVE_IMAGE_TAG}" >/dev/null

echo "- Waiting for codex.serve readiness..."
ready=0
for _ in $(seq 1 30); do
	if curl -fsS -o /dev/null "http://127.0.0.1:${SERVE_PORT}/docs" 2>/dev/null; then
		ready=1
		break
	fi
	sleep 1
done

if [ "${ready}" -ne 1 ]; then
	echo "codex.serve did not become ready in time."
	docker logs "${SERVE_CONTAINER_NAME}" || true
	exit 1
fi

response="$(curl -sS "http://127.0.0.1:${SERVE_PORT}/run" \
	-H "Content-Type: application/json" \
	-d '{"cli":"codex","args":["--version"],"stdin":""}')"

echo "- Verifying Docker mode command execution response"
if ! grep -Eq '"type"[[:space:]]*:[[:space:]]*"exit".*"code"[[:space:]]*:[[:space:]]*0' <<<"${response}"; then
	echo "Expected successful exit from Docker mode run, but got:"
	echo "${response}"
	docker logs "${SERVE_CONTAINER_NAME}" || true
	exit 1
fi

echo "Docker mode smoke test passed."
echo "Test completed successfully."
