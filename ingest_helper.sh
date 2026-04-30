#!/bin/bash
# Upload a single file and auto-confirm all pending states (classify, evidence, version).
# Usage: ./ingest_helper.sh <filepath>

set -e
API="http://localhost:8000"
FILE="$1"

if [ -z "$FILE" ]; then
    echo "usage: $0 <filepath>"
    exit 1
fi

echo ">>> UPLOAD: $FILE"
RESP=$(curl -s -X POST "$API/upload" -F "file=@$FILE")
STATUS=$(echo "$RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status',''))")
UPID=$(echo "$RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('upload_id',''))")

while true; do
    case "$STATUS" in
        stored)
            echo "    STORED upload_id=$UPID"
            echo "$RESP" | python3 -m json.tool
            break
            ;;
        pending_classification)
            echo "    pending_classification → approving"
            RESP=$(curl -s -X POST "$API/confirm/classify/$UPID" \
                -H "Content-Type: application/json" \
                -d '{"decision":"approve"}')
            ;;
        pending_evidence_review)
            echo "    pending_evidence_review → confirming"
            RESP=$(curl -s -X POST "$API/confirm/evidence/$UPID" \
                -H "Content-Type: application/json" \
                -d '{"decision":"approve"}')
            ;;
        pending_version)
            echo "    pending_version → replacing"
            RESP=$(curl -s -X POST "$API/confirm/version/$UPID" \
                -H "Content-Type: application/json" \
                -d '{"decision":"replace"}')
            ;;
        stopped|rejected)
            echo "    STOPPED/REJECTED"
            echo "$RESP"
            break
            ;;
        "")
            echo "    ERROR: no status in response"
            echo "$RESP"
            exit 2
            ;;
        *)
            echo "    UNKNOWN STATUS: $STATUS"
            echo "$RESP"
            exit 3
            ;;
    esac
    STATUS=$(echo "$RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status',''))")
    UPID=$(echo "$RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('upload_id',''))")
done
