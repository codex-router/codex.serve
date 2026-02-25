#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SESSION_ID="demo-$(date +%s)"
REPO_PATH="${REPO_PATH:-$(pwd)}"
DRY_RUN="${DRY_RUN:-false}"
LITELLM_SSL_VERIFY="${LITELLM_SSL_VERIFY:-false}"
LITELLM_CA_BUNDLE="${LITELLM_CA_BUNDLE:-}"
RUN_DATE="$(date +%Y%m%d-%H%M%S)"
OUT_PATH="${OUT_PATH:-/tmp/codex-serve-example-out-${RUN_DATE}}"

INSIGHT_PAYLOAD="/tmp/codex-serve-insight-payload-${RUN_DATE}.json"
INSIGHT_RESPONSE="/tmp/codex-serve-insight-response-${RUN_DATE}.json"

cleanup() {
  rm -f "${INSIGHT_PAYLOAD}"
  rm -f "${INSIGHT_RESPONSE}"
}
trap cleanup EXIT

mkdir -p "${OUT_PATH}"

echo "Testing POST ${BASE_URL}/agent/run with sessionId=${SESSION_ID}"
echo "Expect NDJSON stream with: session/stdout|stderr/exit"

curl -N -sS -X POST "${BASE_URL}/agent/run" \
  -H "Content-Type: application/json" \
  --data-binary @- <<EOF
{
  "agent": "codex",
  "args": ["--model", "ollama-kimi-k2.5"],
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

curl -sS -X POST "${BASE_URL}/insight/run" \
  -H "Content-Type: application/json" \
  --data-binary "@${INSIGHT_PAYLOAD}" \
  -o "${INSIGHT_RESPONSE}"

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
echo "Done. If successful, generated files are written to: ${OUT_PATH}"
