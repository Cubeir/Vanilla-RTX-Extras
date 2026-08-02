#!/bin/bash
# batch-commit.sh
# Stages, commits, and pushes new files in small batches so progress
# is saved incrementally — safer for unstable connections.
#
# Run this from Git Bash, from the repo root:
# bash batch-commit.sh
#
# If a push fails partway through (timeout, dropped connection, etc.),
# just re-run the script. Already-committed batches stay on disk and
# will resume pushing; already-tracked files are skipped automatically.

set -e  # stop immediately on unexpected errors (not on push failure, handled below)

BATCH_SIZE=100   # files per commit — lower this (e.g. 40-50) if commits are still too heavy

# --- Make large single pushes more tolerant of slow connections ---
git config http.postBuffer 524288000     # 500MB buffer, avoids "RPC failed" on big pushes
git config http.lowSpeedLimit 0          # disable "too slow" abort
git config http.lowSpeedTime 999999      # disable timeout based on speed

# Get every new/untracked file that survives .gitignore, one per line
mapfile -t FILES < <(git ls-files --others --exclude-standard)

TOTAL=${#FILES[@]}
if [ "$TOTAL" -eq 0 ]; then
    echo "No new untracked files found. Nothing to do."
    exit 0
fi

echo "Found $TOTAL new files. Committing in batches of $BATCH_SIZE..."
echo ""

BATCH_NUM=1
for ((i=0; i<TOTAL; i+=BATCH_SIZE)); do
    BATCH=("${FILES[@]:i:BATCH_SIZE}")
    START=$((i+1))
    END=$((i+${#BATCH[@]}))

    echo "== Batch $BATCH_NUM: files $START-$END of $TOTAL =="

    git add -- "${BATCH[@]}"
    git commit -m "Add batch $BATCH_NUM ($START-$END of $TOTAL)" --quiet

    echo "   Pushing batch $BATCH_NUM..."
    if git push; then
        echo "   Batch $BATCH_NUM pushed successfully."
    else
        echo ""
        echo "   Push FAILED on batch $BATCH_NUM."
        echo "   No worries — this commit is safely saved on your local disk."
        echo "   Just re-run this script (bash batch-commit.sh) once your"
        echo "   connection is stable again; it will resume pushing from here."
        exit 1
    fi

    ((BATCH_NUM++))
    echo ""
done

echo "All done! $TOTAL files committed and pushed across $((BATCH_NUM-1)) batches."
