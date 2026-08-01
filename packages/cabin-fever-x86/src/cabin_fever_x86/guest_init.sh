#!/usr/bin/env bash
# Runs inside the guest, once, on every boot.
set -euo pipefail

CORE_URL=__PACKAGE_LOCATOR_SENTINEL__

# Application directory. 
mkdir -p /cabin-fever-x86/data
cd /cabin-fever-x86

# uv ships in the image but is not on the PATH a non-login shell gets.
export PATH="/root/.local/bin:$PATH"

# Create a virtual environment and install the core package.
uv venv .venv
echo "Installing core package from: $CORE_URL"
uv pip install --python .venv/bin/python "$CORE_URL"
source .venv/bin/activate

# Proof the install is usable, not merely present.
cf86-server --help >/dev/null
cf86-web --help >/dev/null
echo "GUEST INIT COMPLETE"
