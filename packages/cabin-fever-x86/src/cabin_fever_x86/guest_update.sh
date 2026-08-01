#!/usr/bin/env bash
# Periodically update the PyPI-installed core inside a prepared guest.
set -euo pipefail

CHECK_FILE=/cabin-fever-x86/.version_check
CORE_URL=__PACKAGE_LOCATOR_SENTINEL__

# The timestamp lives in the guest disk, so it survives only when the launcher
# saves the running guest after this script reports completion.
if [[ -e "$CHECK_FILE" ]] && [[ -z "$(find "$CHECK_FILE" -mmin +240 -print -quit)" ]]; then
    exit 0
fi

# uv ships in the image but is not on the PATH a non-login shell gets.
export PATH="/root/.local/bin:$PATH"

cd /cabin-fever-x86
uv pip install --python .venv/bin/python --upgrade "$CORE_URL"
touch "$CHECK_FILE"
echo "VERSION CHECK COMPLETE"
