#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SESSION_ID="demo-$(date +%s)"
REPO_PATH="${REPO_PATH:-$(pwd)}"
OUT_PATH="${OUT_PATH:-/tmp/codex-insight-$(date +%s)}"
DRY_RUN="${DRY_RUN:-true}"

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
echo "repoPath=${REPO_PATH}"
echo "outPath=${OUT_PATH}"
echo "dryRun=${DRY_RUN}"

curl -sS -X POST "${BASE_URL}/insight/run" \
  -H "Content-Type: application/json" \
  --data-binary @- <<EOF
{
  "repoPath": "${REPO_PATH}",
  "outPath": "${OUT_PATH}",
  "maxFilesPerModule": 40,
  "maxCharsPerFile": 10000,
  "dryRun": ${DRY_RUN}
}
EOF

echo
echo "Done. If successful, response includes stdout/stderr/exit_code and generated files from outPath."
