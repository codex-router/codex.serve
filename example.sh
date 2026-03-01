#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8000}"
SESSION_ID="demo-$(date +%s)"
REPO_PATH="${REPO_PATH:-$(pwd)}"
DRY_RUN="${DRY_RUN:-false}"
LITELLM_SSL_VERIFY="${LITELLM_SSL_VERIFY:-false}"
LITELLM_CA_BUNDLE="${LITELLM_CA_BUNDLE:-}"
GRAPH_MODEL="${GRAPH_MODEL:-}"
GRAPH_MAX_RETRIES="${GRAPH_MAX_RETRIES:-3}"
GRAPH_RETRY_DELAY_SECONDS="${GRAPH_RETRY_DELAY_SECONDS:-10}"
GRAPH_STARTUP_WAIT_SECONDS="${GRAPH_STARTUP_WAIT_SECONDS:-240}"
GRAPH_HEALTH_URL="${GRAPH_HEALTH_URL:-http://localhost:52104/health}"
GRAPH_CONTAINER_NAME="${GRAPH_CONTAINER_NAME:-codex-graph}"
QUEUE_MAX_RETRIES="${QUEUE_MAX_RETRIES:-3}"
QUEUE_RETRY_DELAY_SECONDS="${QUEUE_RETRY_DELAY_SECONDS:-3}"
DEMO_CONTEXT_OVERFLOW="${DEMO_CONTEXT_OVERFLOW:-false}"
RUN_DATE="$(date +%Y%m%d-%H%M%S)"
OUT_PATH="${OUT_PATH:-/tmp/codex-serve-example-out-${RUN_DATE}}"

INSIGHT_PAYLOAD="/tmp/codex-serve-insight-payload-${RUN_DATE}.json"
INSIGHT_RESPONSE="/tmp/codex-serve-insight-response-${RUN_DATE}.json"
GRAPH_PAYLOAD="/tmp/codex-serve-graph-payload-${RUN_DATE}.json"
GRAPH_RESPONSE="/tmp/codex-serve-graph-response-${RUN_DATE}.json"

resolve_graph_model_from_compose() {
  local compose_file="${COMPOSE_FILE:-${SCRIPT_DIR}/docker-compose.yml}"
  local line raw

  [[ -f "${compose_file}" ]] || return 0

  line="$(grep -E '^[[:space:]]*-[[:space:]]*GRAPH_MODEL=' "${compose_file}" | head -n 1 || true)"
  [[ -n "${line}" ]] || return 0

  raw="${line#*=}"
  raw="$(printf '%s' "${raw}" | sed -E 's/[[:space:]]+$//')"

  if [[ "${raw}" =~ ^\$\{GRAPH_MODEL:-([^}]*)\}$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return 0
  fi

  if [[ "${raw}" =~ ^\$\{GRAPH_MODEL\}$ ]]; then
    return 0
  fi

  printf '%s' "${raw}"
}

wait_for_graph_health() {
  local started_at elapsed

  started_at="$(date +%s)"
  while true; do
    if curl -sS -f "${GRAPH_HEALTH_URL}" >/dev/null 2>&1; then
      return 0
    fi

    elapsed=$(( $(date +%s) - started_at ))
    if [[ "${elapsed}" -ge "${GRAPH_STARTUP_WAIT_SECONDS}" ]]; then
      return 1
    fi

    sleep 2
  done
}

if [[ -z "${GRAPH_MODEL}" ]]; then
  GRAPH_MODEL="$(resolve_graph_model_from_compose)"
fi

GRAPH_MODEL_DISPLAY="${GRAPH_MODEL:-<server-default>}"

cleanup() {
  rm -f "${INSIGHT_PAYLOAD}"
  rm -f "${INSIGHT_RESPONSE}"
  rm -f "${GRAPH_PAYLOAD}"
  rm -f "${GRAPH_RESPONSE}"
}
trap cleanup EXIT

mkdir -p "${OUT_PATH}"

echo "Testing POST ${BASE_URL}/agent/run with sessionId=${SESSION_ID}"
echo "Expect NDJSON stream with: session/stdout|stderr/exit"
echo "demoContextOverflow=${DEMO_CONTEXT_OVERFLOW}"

curl -N -sS -X POST "${BASE_URL}/agent/run" \
  -H "Content-Type: application/json" \
  --data-binary @- <<EOF
{
  "agent": "codex",
  "args": ["--model", "auto"],
  "stdin": "Summarize attached files in one sentence.",
  "sessionId": "${SESSION_ID}",
  "contextFiles": [
    {
      "path": "hello.c",
      "content": "#include <stdio.h>\\nint main(){printf(\"Hello\\\\n\");return 0;}"
    },
    {
      "path": "notes.txt",
      "base64Content": "SGVsbG8gZnJvbSBiYXNlNjQu"
    }
  ]
}
EOF

if [[ "${DEMO_CONTEXT_OVERFLOW}" =~ ^(1|true|yes|on)$ ]]; then
  echo
  echo "Testing optional /agent/run context-overflow auto-compress demo"

  OVERFLOW_PAYLOAD="/tmp/codex-serve-overflow-payload-${RUN_DATE}.json"
  OVERFLOW_RESPONSE="/tmp/codex-serve-overflow-response-${RUN_DATE}.ndjson"

  python3 - "${OVERFLOW_PAYLOAD}" "${RUN_DATE}" <<'PY'
import json
import sys

payload_path = sys.argv[1]
run_date = sys.argv[2]
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
  "sessionId": f"demo-overflow-{run_date}"
}

with open(payload_path, "w", encoding="utf-8") as f:
  json.dump(payload, f)
PY

  curl -N -sS -X POST "${BASE_URL}/agent/run" \
    -H "Content-Type: application/json" \
    --data-binary "@${OVERFLOW_PAYLOAD}" \
    -o "${OVERFLOW_RESPONSE}"

  cat "${OVERFLOW_RESPONSE}"
  rm -f "${OVERFLOW_PAYLOAD}" "${OVERFLOW_RESPONSE}"
fi

echo
echo "Testing POST ${BASE_URL}/insight/run"
echo "repoDirectory=${REPO_PATH}"
echo "dryRun=${DRY_RUN}"
echo "litellmSslVerify=${LITELLM_SSL_VERIFY}"
echo "litellmCaBundle=${LITELLM_CA_BUNDLE}"
echo "outPath=${OUT_PATH}"
echo "payloadFile=${INSIGHT_PAYLOAD}"

python3 - "${REPO_PATH}" "${DRY_RUN}" "${OUT_PATH}" "${LITELLM_SSL_VERIFY}" "${LITELLM_CA_BUNDLE}" > "${INSIGHT_PAYLOAD}" <<'PY'
import base64
import json
import os
import sys

repo_path = os.path.abspath(sys.argv[1])
dry_run = sys.argv[2].strip().lower() in {"1", "true", "yes", "y", "on"}
out_path = os.path.abspath(sys.argv[3])
litellm_ssl_verify = sys.argv[4].strip().lower() in {"1", "true", "yes", "y", "on"}
litellm_ca_bundle = sys.argv[5].strip()

files = []
for root, _, names in os.walk(repo_path):
    for name in names:
        abs_path = os.path.join(root, name)
        rel_path = os.path.relpath(abs_path, repo_path).replace("\\", "/")
        with open(abs_path, "rb") as f:
            payload = base64.b64encode(f.read()).decode("ascii")
        files.append({"path": rel_path, "base64Content": payload})

body = {
    "files": files,
    "maxFilesPerModule": 40,
    "maxCharsPerFile": 10000,
    "dryRun": dry_run,
    "outPath": out_path,
    "env": {
      "LITELLM_SSL_VERIFY": "true" if litellm_ssl_verify else "false",
      "LITELLM_CA_BUNDLE": litellm_ca_bundle,
    },
}
print(json.dumps(body))
PY

insight_attempt=1
while true; do
  insight_status_code="$(curl -sS -X POST "${BASE_URL}/insight/run" \
    -H "Content-Type: application/json" \
    --data-binary "@${INSIGHT_PAYLOAD}" \
    -o "${INSIGHT_RESPONSE}" \
    -w "%{http_code}")"

  if [[ "${insight_status_code}" -ge 200 && "${insight_status_code}" -lt 300 ]]; then
    break
  fi

  if [[ "${insight_status_code}" == "503" ]] && [[ "${insight_attempt}" -lt "${QUEUE_MAX_RETRIES}" ]]; then
    echo "insight/run attempt ${insight_attempt}/${QUEUE_MAX_RETRIES} returned 503 (queue backpressure), retrying in ${QUEUE_RETRY_DELAY_SECONDS}s..."
    sleep "${QUEUE_RETRY_DELAY_SECONDS}"
    insight_attempt=$((insight_attempt + 1))
    continue
  fi

  echo "insight/run failed with HTTP ${insight_status_code}."
  cat "${INSIGHT_RESPONSE}"
  exit 1
done

cat "${INSIGHT_RESPONSE}"

python3 - "${INSIGHT_RESPONSE}" "${OUT_PATH}" <<'PY'
import json
import os
import sys

response_path = sys.argv[1]
out_path = sys.argv[2]

with open(response_path, "r", encoding="utf-8") as f:
    data = json.load(f)

files = data.get("files") or []
for item in files:
    name = item.get("path")
    content = item.get("content", "")
    if not name:
        continue
    target = os.path.join(out_path, name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as out_file:
        out_file.write(content)

print(f"materializedFiles={len(files)}")
print(f"materializedOutPath={out_path}")
print(f"serverOutputDir={data.get('outputDir', '')}")
PY

echo
echo "Testing POST ${BASE_URL}/graph/run"
echo "graphModel=${GRAPH_MODEL_DISPLAY}"
echo "graphMaxRetries=${GRAPH_MAX_RETRIES}"
echo "graphRetryDelaySeconds=${GRAPH_RETRY_DELAY_SECONDS}"
echo "graphStartupWaitSeconds=${GRAPH_STARTUP_WAIT_SECONDS}"
echo "graphHealthUrl=${GRAPH_HEALTH_URL}"
echo "graphContainerName=${GRAPH_CONTAINER_NAME}"
echo "queueMaxRetries=${QUEUE_MAX_RETRIES}"
echo "queueRetryDelaySeconds=${QUEUE_RETRY_DELAY_SECONDS}"
echo "payloadFile=${GRAPH_PAYLOAD}"

python3 - "${GRAPH_MODEL}" > "${GRAPH_PAYLOAD}" <<'PY'
import json
import os
import sys

graph_model = sys.argv[1].strip()

body = {
  "code": "from openai import OpenAI\n\nclient = OpenAI()\n\ndef summarize(text):\n    if len(text) > 200:\n        prompt = 'Summarize this long text'\n    else:\n        prompt = 'Answer this short question'\n\n    response = client.chat.completions.create(\n        model='gpt-4o-mini',\n        messages=[\n            {'role': 'system', 'content': prompt},\n            {'role': 'user', 'content': text}\n        ]\n    )\n    return response.choices[0].message.content\n",
  "file_paths": ["example/sample_workflow.py"],
  "framework_hint": "openai",
}

env = {}
for key in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "LITELLM_SSL_VERIFY", "LITELLM_CA_BUNDLE"):
  value = os.environ.get(key)
  if value:
    env[key] = value

if graph_model:
  env["GRAPH_MODEL"] = graph_model

if env:
  body["env"] = env

print(json.dumps(body))
PY

graph_attempt=1
while true; do
  graph_status_code="$(curl -sS -X POST "${BASE_URL}/graph/run" \
    -H "Content-Type: application/json" \
    --data-binary "@${GRAPH_PAYLOAD}" \
    -o "${GRAPH_RESPONSE}" \
    -w "%{http_code}")"

  if [[ "${graph_status_code}" -ge 200 && "${graph_status_code}" -lt 300 ]]; then
    break
  fi

  if [[ "${graph_status_code}" == "503" ]] && [[ "${graph_attempt}" -lt "${GRAPH_MAX_RETRIES}" ]]; then
    echo "graph/run attempt ${graph_attempt}/${GRAPH_MAX_RETRIES} returned 503 (queue backpressure), retrying in ${QUEUE_RETRY_DELAY_SECONDS}s..."
    sleep "${QUEUE_RETRY_DELAY_SECONDS}"
    graph_attempt=$((graph_attempt + 1))
    continue
  fi

  if [[ "${graph_status_code}" == "504" ]] && \
     grep -q "Timed out waiting for codex.graph health endpoint after startup" "${GRAPH_RESPONSE}" && \
     [[ "${graph_attempt}" -lt "${GRAPH_MAX_RETRIES}" ]]; then
    echo "graph/run attempt ${graph_attempt}/${GRAPH_MAX_RETRIES} timed out during codex.graph startup."
    echo "Waiting for codex.graph health at ${GRAPH_HEALTH_URL} (up to ${GRAPH_STARTUP_WAIT_SECONDS}s) before next retry..."
    if wait_for_graph_health; then
      echo "codex.graph health endpoint is now ready; retrying graph/run immediately."
    else
      echo "codex.graph health endpoint is still not ready; continuing with retry delay ${GRAPH_RETRY_DELAY_SECONDS}s."
      sleep "${GRAPH_RETRY_DELAY_SECONDS}"
    fi
    graph_attempt=$((graph_attempt + 1))
    continue
  fi

  echo "graph/run failed with HTTP ${graph_status_code}."
  cat "${GRAPH_RESPONSE}"
  if command -v docker >/dev/null 2>&1; then
    echo
    echo "Last 80 lines from docker logs ${GRAPH_CONTAINER_NAME} (if container exists):"
    docker logs --tail 80 "${GRAPH_CONTAINER_NAME}" 2>/dev/null || true
  fi
  exit 1
done

cat "${GRAPH_RESPONSE}"

echo
echo "Done. If successful, generated files are written to: ${OUT_PATH}"
