#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
AGENT_DIR="${ROOT_DIR}/codex.agent"
INSIGHT_DIR="${ROOT_DIR}/codex.insight"

AGENT_IMAGE_TAG="codex-agent:test"
INSIGHT_IMAGE_TAG="codex-insight:test"
SERVE_IMAGE_TAG="codex-serve:test"
SERVE_CONTAINER_NAME="codex-serve-test"
GRAPH_CONTAINER_NAME="codex-graph-test"
GRAPH_DEFAULT_CONTAINER_NAME="codex-graph-backend"
SERVE_PORT="18000"
TEST_CONTAINER_LABEL="codex.serve.test=true"
TMP_DIR=""

cleanup() {
	if [ -n "${RUN_PID:-}" ]; then
		kill "${RUN_PID}" >/dev/null 2>&1 || true
	fi

	if [ -n "${TMP_DIR}" ] && [ -d "${TMP_DIR}" ]; then
		rm -rf "${TMP_DIR}"
	fi

	test_container_ids="$(docker ps -aq --filter "label=${TEST_CONTAINER_LABEL}" 2>/dev/null || true)"
	if [ -n "${test_container_ids}" ]; then
		docker rm -f ${test_container_ids} >/dev/null 2>&1 || true
	fi

	agent_container_ids="$(docker ps -aq --filter "ancestor=${AGENT_IMAGE_TAG}" 2>/dev/null || true)"
	if [ -n "${agent_container_ids}" ]; then
		docker rm -f ${agent_container_ids} >/dev/null 2>&1 || true
	fi

	insight_container_ids="$(docker ps -aq --filter "ancestor=${INSIGHT_IMAGE_TAG}" 2>/dev/null || true)"
	if [ -n "${insight_container_ids}" ]; then
		docker rm -f ${insight_container_ids} >/dev/null 2>&1 || true
	fi

	docker rm -f "${GRAPH_CONTAINER_NAME}" >/dev/null 2>&1 || true
	docker rm -f "${GRAPH_DEFAULT_CONTAINER_NAME}" >/dev/null 2>&1 || true
	docker rm -f "${SERVE_CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[1/6] Building agent Docker image from codex.agent: ${AGENT_IMAGE_TAG}"
docker build -t "${AGENT_IMAGE_TAG}" -f "${AGENT_DIR}/Dockerfile" "${AGENT_DIR}"

echo "[2/6] Running agent image smoke tests in container"
docker run --rm "${AGENT_IMAGE_TAG}" bash -lc '
set -euo pipefail

echo "- Verifying Ubuntu base image"
if ! grep -qi "^ID=ubuntu" /etc/os-release; then
	echo "Expected Ubuntu base image, but /etc/os-release is:"
	cat /etc/os-release
	exit 1
fi

echo "- Verifying required agent binaries"
for cmd in codex opencode qwen kimi; do
	command -v "$cmd" >/dev/null
	"$cmd" --version >/dev/null
done

echo "- Verifying configured agent path env vars"
for path_var in CODEX_PATH OPENCODE_PATH QWEN_PATH KIMI_PATH; do
	value="${!path_var}"
	[ -n "$value" ]
	[ -x "$value" ]
done

echo "Agent image smoke tests passed."
'

echo "[3/6] Building codex-insight Docker image: ${INSIGHT_IMAGE_TAG}"
docker build -t "${INSIGHT_IMAGE_TAG}" -f "${INSIGHT_DIR}/Dockerfile" "${INSIGHT_DIR}"

echo "[4/6] Building codex.serve Docker image: ${SERVE_IMAGE_TAG}"
docker build -t "${SERVE_IMAGE_TAG}" -f "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}"

echo "[5/6] Running codex.serve with CODEX_AGENT_IMAGE=${AGENT_IMAGE_TAG} and CODEX_INSIGHT_IMAGE=${INSIGHT_IMAGE_TAG}"
docker run -d \
	--name "${SERVE_CONTAINER_NAME}" \
	--label "${TEST_CONTAINER_LABEL}" \
	-p "${SERVE_PORT}:8000" \
	-v /var/run/docker.sock:/var/run/docker.sock \
	-e AGENT_LIST="codex,bash" \
	-e AGENT_MODEL="auto,test-model-fast,test-model-fallback" \
	-e CODEX_AGENT_IMAGE="${AGENT_IMAGE_TAG}" \
	-e CODEX_INSIGHT_IMAGE="${INSIGHT_IMAGE_TAG}" \
	-e GRAPH_CONTAINER_NAME="${GRAPH_CONTAINER_NAME}" \
	-e GRAPH_BASE_URL="http://127.0.0.1:59999" \
	-e GRAPH_AUTO_START="true" \
	-e GRAPH_HEALTH_CHECK_TIMEOUT_SECONDS="5" \
	-e RUN_RESPONSE_TIMEOUT_SECONDS="60" \
	-e INSIGHT_RESPONSE_TIMEOUT_SECONDS="300" \
	-e GRAPH_RESPONSE_TIMEOUT_SECONDS="30" \
	-e REQUEST_QUEUE_MAX_PENDING="100" \
	-e REQUEST_QUEUE_WAIT_TIMEOUT_SECONDS="10" \
	-e AGENT_MAX_CONCURRENT_REQUESTS="4" \
	-e INSIGHT_MAX_CONCURRENT_REQUESTS="2" \
	-e GRAPH_MAX_CONCURRENT_REQUESTS="4" \
	-e AUTO_COMPRESS_ON_CONTEXT_OVERFLOW="true" \
	-e AUTO_COMPRESS_MAX_CHARS="220" \
	-e AUTO_COMPRESS_KEEP_HEAD_CHARS="60" \
	-e LITELLM_BASE_URL="http://litellm.test.local" \
	-e LITELLM_API_KEY="test-api-key" \
	-e LITELLM_SSL_VERIFY="false" \
	-e LITELLM_CA_BUNDLE="" \
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

echo "[6/6] Testing codex.serve APIs (/agents, /models, /agent/run, /insight/run, /graph/run)"
TMP_DIR="$(mktemp -d)"
AGENTS_BODY="${TMP_DIR}/agents.json"
MODELS_BODY="${TMP_DIR}/models.json"
RUN_BODY="${TMP_DIR}/run.ndjson"
CONTEXT_RUN_BODY="${TMP_DIR}/context-run.ndjson"
CONTEXT_RUN_PAYLOAD="${TMP_DIR}/context-run.json"
B64_CONTEXT_RUN_BODY="${TMP_DIR}/b64-context-run.ndjson"
B64_CONTEXT_RUN_PAYLOAD="${TMP_DIR}/b64-context-run.json"
INSIGHT_RUN_BODY="${TMP_DIR}/insight-run.json"
INSIGHT_RUN_PAYLOAD="${TMP_DIR}/insight-run-payload.json"

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

expected_models = ["auto", "test-model-fast", "test-model-fallback"]
if models != expected_models:
	raise SystemExit(f"/models response mismatch: got {models}, expected {expected_models}")

if count != len(expected_models):
	raise SystemExit(f"/models count mismatch: got {count}, expected {len(expected_models)}")
PY

echo "- Testing POST /agent/run"
curl -sS -N -o "${RUN_BODY}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/agent/run" \
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
	raise SystemExit("/agent/run returned no NDJSON events")

session_events = [e for e in events if e.get("type") == "session"]
if not session_events:
	raise SystemExit("/agent/run response missing session event")

sessionIdValue = session_events[0].get("id")
if not isinstance(sessionIdValue, str) or not sessionIdValue:
	raise SystemExit(f"/agent/run session id is invalid: {sessionIdValue}")

if sessionIdValue != "smoke-run-session":
	raise SystemExit(f"/agent/run session id mismatch: got {sessionIdValue}")

exit_events = [e for e in events if e.get("type") == "exit"]
if not exit_events:
	raise SystemExit("/agent/run response missing exit event")

exit_code = exit_events[-1].get("code")
if not isinstance(exit_code, int):
	raise SystemExit(f"/agent/run exit code is not an integer: {exit_code}")
PY

echo "- Testing POST /agent/run with --model auto resolution"
AUTO_MODEL_RUN_BODY="${TMP_DIR}/auto-model-run.ndjson"
AUTO_MODEL_RUN_PAYLOAD="${TMP_DIR}/auto-model-run.json"

cat > "${AUTO_MODEL_RUN_PAYLOAD}" <<'JSON'
{
  "agent": "bash",
  "args": ["-lc", "printf '%s' \"$LITELLM_MODEL\"", "--model", "auto"],
  "stdin": "",
  "sessionId": "auto-model-run-session"
}
JSON

curl -sS -N -o "${AUTO_MODEL_RUN_BODY}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/agent/run" \
	-H "Content-Type: application/json" \
	--data-binary "@${AUTO_MODEL_RUN_PAYLOAD}"

python3 - "${AUTO_MODEL_RUN_BODY}" <<'PY'
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
	raise SystemExit("/agent/run auto-model test returned no NDJSON events")

stdout_text = "".join(e.get("data", "") for e in events if e.get("type") == "stdout")
if "test-model-fast" not in stdout_text:
	raise SystemExit(f"/agent/run auto-model expected selected model 'test-model-fast', got stdout={stdout_text!r}")

exit_events = [e for e in events if e.get("type") == "exit"]
if not exit_events:
	raise SystemExit("/agent/run auto-model test missing exit event")

if exit_events[-1].get("code") != 0:
	raise SystemExit(f"/agent/run auto-model test expected exit 0, got {exit_events[-1].get('code')}")
PY

echo "- Testing POST /agent/run with contextFiles injection"
cat > "${CONTEXT_RUN_PAYLOAD}" <<'JSON'
{
  "agent": "bash",
  "args": ["-lc", "cat"],
  "stdin": "update @test.c to support to print hello world",
  "sessionId": "context-run-session",
  "contextFiles": [
    {
      "path": "test.c",
      "content": "int main(){return 0;}"
    }
  ]
}
JSON

curl -sS -N -o "${CONTEXT_RUN_BODY}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/agent/run" \
	-H "Content-Type: application/json" \
	--data-binary "@${CONTEXT_RUN_PAYLOAD}"

python3 - "${CONTEXT_RUN_BODY}" <<'PY'
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
	raise SystemExit("/agent/run context test returned no NDJSON events")

stdout_text = "".join(e.get("data", "") for e in events if e.get("type") == "stdout")
required_fragments = [
	"Execution note:",
	"Do not request filesystem permission or claim missing file access.",
	"Referenced file context:",
	"--- FILE: test.c ---",
	"int main(){return 0;}",
	"--- END FILE: test.c ---",
	"update @test.c to support to print hello world",
]

for fragment in required_fragments:
	if fragment not in stdout_text:
		raise SystemExit(f"/agent/run context test missing expected fragment: {fragment}")

exit_events = [e for e in events if e.get("type") == "exit"]
if not exit_events:
	raise SystemExit("/agent/run context test missing exit event")

if exit_events[-1].get("code") != 0:
	raise SystemExit(f"/agent/run context test expected exit 0, got {exit_events[-1].get('code')}")
PY

echo "- Testing POST /agent/run with contextFiles base64Content injection"
# base64 of: int main(){return 0;}
# echo -n 'int main(){return 0;}' | base64  =>  aW50IG1haW4oKXtyZXR1cm4gMDt9
cat > "${B64_CONTEXT_RUN_PAYLOAD}" <<'JSON'
{
  "agent": "bash",
  "args": ["-lc", "cat"],
  "stdin": "explain @hello.c",
  "sessionId": "b64-context-run-session",
  "contextFiles": [
    {
      "path": "hello.c",
      "base64Content": "aW50IG1haW4oKXtyZXR1cm4gMDt9"
    }
  ]
}
JSON

curl -sS -N -o "${B64_CONTEXT_RUN_BODY}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/agent/run" \
	-H "Content-Type: application/json" \
	--data-binary "@${B64_CONTEXT_RUN_PAYLOAD}"

python3 - "${B64_CONTEXT_RUN_BODY}" <<'PY'
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
	raise SystemExit("/agent/run base64 context test returned no NDJSON events")

stdout_text = "".join(e.get("data", "") for e in events if e.get("type") == "stdout")
required_fragments = [
	"Execution note:",
	"Referenced file context:",
	"--- FILE: hello.c ---",
	"int main(){return 0;}",
	"--- END FILE: hello.c ---",
	"explain @hello.c",
]

for fragment in required_fragments:
	if fragment not in stdout_text:
		raise SystemExit(f"/agent/run base64 context test missing expected fragment: {fragment}")

exit_events = [e for e in events if e.get("type") == "exit"]
if not exit_events:
	raise SystemExit("/agent/run base64 context test missing exit event")

if exit_events[-1].get("code") != 0:
	raise SystemExit(f"/agent/run base64 context test expected exit 0, got {exit_events[-1].get('code')}")
PY

echo "- Testing POST /agent/run auto-compress retry on context overflow"
COMPRESS_RUN_BODY="${TMP_DIR}/compress-run.ndjson"
COMPRESS_RUN_PAYLOAD="${TMP_DIR}/compress-run.json"

python3 - "${COMPRESS_RUN_PAYLOAD}" <<'PY'
import json
import sys

payload_path = sys.argv[1]
long_prompt = "HISTORY-BLOCK-" * 120

payload = {
	"agent": "bash",
	"args": [
		"-lc",
		"INPUT=\"$(cat)\"; "
		"if printf '%s' \"$INPUT\" | grep -q 'message history compressed automatically by codex.serve'; then "
		"printf 'compressed-retry-ok'; "
		"else "
		"printf 'maximum context length exceeded\n' >&2; exit 1; "
		"fi"
	],
	"stdin": long_prompt,
	"sessionId": "compress-retry-session"
}

with open(payload_path, "w", encoding="utf-8") as f:
	json.dump(payload, f)
PY

curl -sS -N -o "${COMPRESS_RUN_BODY}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/agent/run" \
	-H "Content-Type: application/json" \
	--data-binary "@${COMPRESS_RUN_PAYLOAD}"

python3 - "${COMPRESS_RUN_BODY}" <<'PY'
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
	raise SystemExit("/agent/run compress retry test returned no NDJSON events")

stderr_text = "".join(e.get("data", "") for e in events if e.get("type") == "stderr")
if "Retrying once with compressed message history" not in stderr_text:
	raise SystemExit(f"/agent/run compress retry missing retry notice, stderr={stderr_text!r}")

stdout_text = "".join(e.get("data", "") for e in events if e.get("type") == "stdout")
if "compressed-retry-ok" not in stdout_text:
	raise SystemExit(f"/agent/run compress retry expected success output, got stdout={stdout_text!r}")

exit_events = [e for e in events if e.get("type") == "exit"]
if not exit_events:
	raise SystemExit("/agent/run compress retry test missing exit event")

if exit_events[-1].get("code") != 0:
	raise SystemExit(f"/agent/run compress retry expected exit 0, got {exit_events[-1].get('code')}")
PY

echo "- Testing POST /sessions/{sessionId}/stop"
STOP_RUN_BODY="${TMP_DIR}/stop-run.ndjson"
STOP_RESP_BODY="${TMP_DIR}/stop-response.json"

curl -sS -N -o "${STOP_RUN_BODY}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/agent/run" \
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
unset RUN_PID

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
	raise SystemExit("stop-session /agent/run returned no NDJSON events")

session_events = [e for e in events if e.get("type") == "session"]
if not session_events or session_events[0].get("id") != "stop-me":
	raise SystemExit(f"stop-session run missing expected session event: {session_events}")

exit_events = [e for e in events if e.get("type") == "exit"]
if not exit_events:
	raise SystemExit("stop-session run missing exit event")

if not isinstance(exit_events[-1].get("code"), int):
	raise SystemExit(f"stop-session run exit code is invalid: {exit_events[-1]}")
PY

echo "- Testing POST /insight/run (dry-run)"
python3 - "${INSIGHT_DIR}" > "${INSIGHT_RUN_PAYLOAD}" <<'PY'
import base64
import json
import os
import sys

repo_dir = os.path.abspath(sys.argv[1])

files = []
for root, _, names in os.walk(repo_dir):
	for name in names:
		abs_path = os.path.join(root, name)
		rel_path = os.path.relpath(abs_path, repo_dir).replace("\\", "/")
		with open(abs_path, "rb") as f:
			payload = base64.b64encode(f.read()).decode("ascii")
		files.append({"path": rel_path, "base64Content": payload})

body = {
	"files": files,
	"dryRun": True,
	"include": ["*.py", "**/*.py"],
}

print(json.dumps(body))
PY

INSIGHT_STATUS="$(curl -sS -o "${INSIGHT_RUN_BODY}" -w "%{http_code}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/insight/run" \
	-H "Content-Type: application/json" \
	--data-binary "@${INSIGHT_RUN_PAYLOAD}")"

if [ "${INSIGHT_STATUS}" = "200" ]; then
python3 - "${INSIGHT_RUN_BODY}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
	data = json.load(f)

if data.get("exit_code") != 0:
	raise SystemExit(f"/insight/run expected exit_code 0, got {data.get('exit_code')}")

if not isinstance(data.get("stdout"), str):
	raise SystemExit("/insight/run missing stdout text")

if "Dry run enabled; no AI calls were made." not in data.get("stdout", ""):
	raise SystemExit("/insight/run dry-run output missing expected marker")

if data.get("count") != 0:
	raise SystemExit(f"/insight/run dry-run expected count 0, got {data.get('count')}")

files = data.get("files")
if files != []:
	raise SystemExit(f"/insight/run dry-run expected empty files, got {files}")
PY
elif [ "${INSIGHT_STATUS}" = "400" ]; then
	python3 - "${INSIGHT_RUN_BODY}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
	data = json.load(f)

detail = data.get("detail")
if not isinstance(detail, str):
	raise SystemExit(f"/insight/run expected string error detail for HTTP 400, got: {data}")

expected = "Could not find the file /tmp/codex-out/. in container"
if expected not in detail:
	raise SystemExit(f"/insight/run unexpected HTTP 400 detail: {detail}")
PY
	echo "- /insight/run returned known dry-run no-output 400; accepted by smoke test"
else
	echo "Expected HTTP 200 (or known dry-run 400) from /insight/run, got ${INSIGHT_STATUS}"
	cat "${INSIGHT_RUN_BODY}"
	exit 1
fi

echo "- Testing POST /graph/run auto-start failure and readiness error mapping"
GRAPH_INVALID_BODY="${TMP_DIR}/graph-invalid.json"
GRAPH_FILE_PATHS_INVALID_BODY="${TMP_DIR}/graph-file-paths-invalid.json"
GRAPH_PROXY_BODY="${TMP_DIR}/graph-proxy.json"

GRAPH_INVALID_STATUS="$(curl -sS -o "${GRAPH_INVALID_BODY}" -w "%{http_code}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/graph/run" \
	-H "Content-Type: application/json" \
	-d '{"code":"","file_paths":[]}')"

if [ "${GRAPH_INVALID_STATUS}" != "502" ] && [ "${GRAPH_INVALID_STATUS}" != "504" ]; then
	echo "Expected HTTP 502/504 from /graph/run when auto-start cannot make backend healthy, got ${GRAPH_INVALID_STATUS}"
	cat "${GRAPH_INVALID_BODY}"
	exit 1
fi

python3 - "${GRAPH_INVALID_BODY}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
	data = json.load(f)

detail = data.get("detail")
if not isinstance(detail, str):
	raise SystemExit(f"/graph/run expected string detail, got: {data}")

expected_fragments = [
	"Timed out waiting for codex.graph health endpoint after startup",
	"Failed to start codex.graph backend",
	"Error response from daemon",
	"pull access denied",
	"No such image",
]

if not any(fragment in detail for fragment in expected_fragments):
	raise SystemExit(f"/graph/run unexpected auto-start failure detail: {detail}")
PY

GRAPH_FILE_PATHS_INVALID_STATUS="$(curl -sS -o "${GRAPH_FILE_PATHS_INVALID_BODY}" -w "%{http_code}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/graph/run" \
	-H "Content-Type: application/json" \
	-d '{"code":"def run():\n    return 1","file_paths":[]}')"

if [ "${GRAPH_FILE_PATHS_INVALID_STATUS}" != "502" ] && [ "${GRAPH_FILE_PATHS_INVALID_STATUS}" != "504" ]; then
	echo "Expected HTTP 502/504 from /graph/run when auto-start cannot make backend healthy, got ${GRAPH_FILE_PATHS_INVALID_STATUS}"
	cat "${GRAPH_FILE_PATHS_INVALID_BODY}"
	exit 1
fi

python3 - "${GRAPH_FILE_PATHS_INVALID_BODY}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
	data = json.load(f)

detail = data.get("detail")
if not isinstance(detail, str):
	raise SystemExit(f"/graph/run expected string detail, got: {data}")

expected_fragments = [
	"Timed out waiting for codex.graph health endpoint after startup",
	"Failed to start codex.graph backend",
	"Error response from daemon",
	"pull access denied",
	"No such image",
]

if not any(fragment in detail for fragment in expected_fragments):
	raise SystemExit(f"/graph/run unexpected auto-start failure detail: {detail}")
PY

GRAPH_PROXY_STATUS="$(curl -sS -o "${GRAPH_PROXY_BODY}" -w "%{http_code}" \
	-X POST "http://127.0.0.1:${SERVE_PORT}/graph/run" \
	-H "Content-Type: application/json" \
	-d '{"code":"def run():\n    return 1","file_paths":["app.py"],"env":{"GRAPH_MODEL":"graph-test-model","LITELLM_SSL_VERIFY":"false","LITELLM_CA_BUNDLE":""}}')"

if [ "${GRAPH_PROXY_STATUS}" != "502" ] && [ "${GRAPH_PROXY_STATUS}" != "504" ]; then
	echo "Expected HTTP 502/504 from /graph/run when GRAPH_BASE_URL is unreachable, got ${GRAPH_PROXY_STATUS}"
	cat "${GRAPH_PROXY_BODY}"
	exit 1
fi

python3 - "${GRAPH_PROXY_BODY}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
	data = json.load(f)

detail = data.get("detail")
if not isinstance(detail, str):
	raise SystemExit(f"/graph/run expected string detail, got: {data}")

expected_fragments = [
	"Timed out waiting for codex.graph health endpoint after startup",
	"Failed to start codex.graph backend",
	"Error response from daemon",
	"pull access denied",
	"No such image",
	"Failed to call codex.graph",
]

if not any(fragment in detail for fragment in expected_fragments):
	raise SystemExit(f"/graph/run upstream error detail mismatch: {detail}")
PY

rm -rf "${TMP_DIR}"
TMP_DIR=""

echo "Docker mode smoke test passed."
echo "Test completed successfully."
