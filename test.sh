#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
AGENT_DIR="${ROOT_DIR}/codex.agent"

AGENT_IMAGE_TAG="codex-agent:test"
SERVE_IMAGE_TAG="codex-serve:test"
SERVE_CONTAINER_NAME="codex-serve-test"
SERVE_PORT="18000"
SYNC_TMP_DIR=""

cleanup() {
	docker rm -f "${SERVE_CONTAINER_NAME}" >/dev/null 2>&1 || true
	if [ -n "${SYNC_TMP_DIR}" ]; then
		rm -rf "${SYNC_TMP_DIR}" >/dev/null 2>&1 || true
	fi
}
trap cleanup EXIT

SYNC_TMP_DIR="$(mktemp -d)"

echo "[1/5] Building agent Docker image from codex.agent: ${AGENT_IMAGE_TAG}"
docker build -t "${AGENT_IMAGE_TAG}" -f "${AGENT_DIR}/Dockerfile" "${AGENT_DIR}"

echo "[2/5] Running agent image smoke tests in container"
docker run --rm "${AGENT_IMAGE_TAG}" bash -lc '
set -euo pipefail

echo "- Verifying Ubuntu base image"
if ! grep -qi "^ID=ubuntu" /etc/os-release; then
	echo "Expected Ubuntu base image, but /etc/os-release is:"
	cat /etc/os-release
	exit 1
fi

echo "- Verifying required agent binaries"
for cmd in claude codex gemini opencode qwen; do
	command -v "$cmd" >/dev/null
	"$cmd" --version >/dev/null
done

echo "- Verifying configured agent path env vars"
for path_var in CLAUDE_PATH CODEX_PATH GEMINI_PATH OPENCODE_PATH QWEN_PATH; do
	value="${!path_var}"
	[ -n "$value" ]
	[ -x "$value" ]
done

echo "Agent image smoke tests passed."
'

echo "[3/5] Building codex.serve Docker image: ${SERVE_IMAGE_TAG}"
docker build -t "${SERVE_IMAGE_TAG}" -f "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}"

echo "[4/5] Running codex.serve with CODEX_AGENT_IMAGE=${AGENT_IMAGE_TAG}"
docker run -d \
	--name "${SERVE_CONTAINER_NAME}" \
	-p "${SERVE_PORT}:8000" \
	-v /var/run/docker.sock:/var/run/docker.sock \
	-v "${SYNC_TMP_DIR}:/tmp/codex-sync-host" \
	-e AGENT_LIST="codex,bash" \
	-e CODEX_AGENT_IMAGE="${AGENT_IMAGE_TAG}" \
	-e RUN_RESPONSE_TIMEOUT_SECONDS="60" \
	-e LITELLM_BASE_URL="http://litellm.test.local" \
	-e LITELLM_API_KEY="test-api-key" \
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

echo "[5/5] Testing codex.serve APIs (/agents, /models, /run)"
TMP_DIR="$(mktemp -d)"
AGENTS_BODY="${TMP_DIR}/agents.json"
MODELS_BODY="${TMP_DIR}/models.json"
RUN_BODY="${TMP_DIR}/run.ndjson"

echo "- Testing GET /agents"
AGENTS_STATUS="$(curl -sS -o "${AGENTS_BODY}" -w "%{http_code}" "http://127.0.0.1:${SERVE_PORT}/agents")"
if [ "${AGENTS_STATUS}" != "200" ]; then
	echo "Expected HTTP 200 from /agents, got ${AGENTS_STATUS}"
	cat "${AGENTS_BODY}"
	exit 1
fi

python3 - "${AGENTS_BODY}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
	data = json.load(f)

expected = {"bash", "codex"}
agents = data.get("agents")
count = data.get("count")

if not isinstance(agents, list):
	raise SystemExit("/agents response missing list field 'agents'")

if set(agents) != expected:
	raise SystemExit(f"/agents response mismatch: got {agents}")

if count != len(expected):
	raise SystemExit(f"/agents count mismatch: got {count}, expected {len(expected)}")
PY

echo "- Testing GET /models"
MODELS_STATUS="$(curl -sS -o "${MODELS_BODY}" -w "%{http_code}" "http://127.0.0.1:${SERVE_PORT}/models")"
if [ "${MODELS_STATUS}" != "200" ]; then
	echo "Expected HTTP 200 from /models, got ${MODELS_STATUS}"
	cat "${MODELS_BODY}"
	exit 1
fi

python3 - "${MODELS_BODY}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
	data = json.load(f)

models = data.get("models")
count = data.get("count")

if not isinstance(models, list):
	raise SystemExit("/models response missing list field 'models'")

if models != []:
	raise SystemExit(f"/models response mismatch: got {models}, expected []")

if count != 0:
	raise SystemExit(f"/models count mismatch: got {count}, expected 0")
PY

echo "- Testing POST /run"
curl -sS -N -o "${RUN_BODY}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/run" \
	-H "Content-Type: application/json" \
	-d '{"agent":"codex","args":["--version"],"stdin":"","sessionId":"smoke-run-session"}'

python3 - "${RUN_BODY}" <<'PY'
import json
import sys

path = sys.argv[1]
events = []

with open(path, "r", encoding="utf-8") as f:
	for line in f:
		line = line.strip()
		if not line:
			continue
		events.append(json.loads(line))

if not events:
	raise SystemExit("/run returned no NDJSON events")

session_events = [e for e in events if e.get("type") == "session"]
if not session_events:
	raise SystemExit("/run response missing session event")

sessionIdValue = session_events[0].get("id")
if not isinstance(sessionIdValue, str) or not sessionIdValue:
	raise SystemExit(f"/run session id is invalid: {sessionIdValue}")

if sessionIdValue != "smoke-run-session":
	raise SystemExit(f"/run session id mismatch: got {sessionIdValue}")

exit_events = [e for e in events if e.get("type") == "exit"]
if not exit_events:
	raise SystemExit("/run response missing exit event")

exit_code = exit_events[-1].get("code")
if not isinstance(exit_code, int):
	raise SystemExit(f"/run exit code is not an integer: {exit_code}")
PY

echo "- Testing POST /sessions/{sessionId}/stop"
STOP_RUN_BODY="${TMP_DIR}/stop-run.ndjson"
STOP_RESP_BODY="${TMP_DIR}/stop-response.json"

curl -sS -N -o "${STOP_RUN_BODY}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/run" \
	-H "Content-Type: application/json" \
	-d '{"agent":"bash","args":["-lc","sleep 30"],"stdin":"","sessionId":"stop-me"}' &
RUN_PID=$!

sleep 1

STOP_STATUS="$(curl -sS -o "${STOP_RESP_BODY}" -w "%{http_code}" -X POST "http://127.0.0.1:${SERVE_PORT}/sessions/stop-me/stop")"
if [ "${STOP_STATUS}" != "200" ]; then
	echo "Expected HTTP 200 from /sessions/stop-me/stop, got ${STOP_STATUS}"
	cat "${STOP_RESP_BODY}"
	kill "${RUN_PID}" >/dev/null 2>&1 || true
	exit 1
fi

wait "${RUN_PID}"

python3 - "${STOP_RESP_BODY}" "${STOP_RUN_BODY}" <<'PY'
import json
import sys

stop_response_path = sys.argv[1]
stop_run_path = sys.argv[2]

with open(stop_response_path, "r", encoding="utf-8") as f:
	stop_data = json.load(f)

if stop_data.get("sessionId") != "stop-me":
	raise SystemExit(f"/sessions stop sessionId mismatch: {stop_data}")

if stop_data.get("status") != "stopped":
	raise SystemExit(f"/sessions stop status mismatch: {stop_data}")

events = []
with open(stop_run_path, "r", encoding="utf-8") as f:
	for line in f:
		line = line.strip()
		if not line:
			continue
		events.append(json.loads(line))

if not events:
	raise SystemExit("stop-session /run returned no NDJSON events")

session_events = [e for e in events if e.get("type") == "session"]
if not session_events or session_events[0].get("id") != "stop-me":
	raise SystemExit(f"stop-session run missing expected session event: {session_events}")

exit_events = [e for e in events if e.get("type") == "exit"]
if not exit_events:
	raise SystemExit("stop-session run missing exit event")

if not isinstance(exit_events[-1].get("code"), int):
	raise SystemExit(f"stop-session run exit code is invalid: {exit_events[-1]}")
PY

echo "- Testing POST /workspace/sync"
SYNC_RESP_BODY="${TMP_DIR}/workspace-sync.json"

SYNC_STATUS="$(curl -sS -o "${SYNC_RESP_BODY}" -w "%{http_code}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/workspace/sync" \
	-H "Content-Type: application/json" \
	-d '{"workspaceRoot":"/tmp/codex-sync-host/workspace","files":[{"path":"src/test-sync.txt","contentBase64":"aGVsbG8gd29ya2JlbmNoCg=="},{"path":"nested/a/b.txt","contentBase64":"bXVsdGktbGV2ZWwK"}]}')"

if [ "${SYNC_STATUS}" != "200" ]; then
	echo "Expected HTTP 200 from /workspace/sync, got ${SYNC_STATUS}"
	cat "${SYNC_RESP_BODY}"
	exit 1
fi

python3 - "${SYNC_RESP_BODY}" "${SYNC_TMP_DIR}" <<'PY'
import json
import os
import sys

response_path = sys.argv[1]
sync_root = sys.argv[2]

with open(response_path, "r", encoding="utf-8") as f:
	data = json.load(f)

written = data.get("written")
if written != 2:
	raise SystemExit(f"/workspace/sync written mismatch: got {written}, expected 2")

expected = {
	os.path.join(sync_root, "workspace", "src", "test-sync.txt"): "hello workbench\n",
	os.path.join(sync_root, "workspace", "nested", "a", "b.txt"): "multi-level\n",
}

for path, expected_content in expected.items():
	if not os.path.exists(path):
		raise SystemExit(f"Synced file missing: {path}")
	with open(path, "r", encoding="utf-8") as f:
		actual = f.read()
	if actual != expected_content:
		raise SystemExit(f"Synced file content mismatch for {path}: {actual!r}")
PY

rm -rf "${TMP_DIR}"

echo "Docker mode smoke test passed."
echo "Test completed successfully."
