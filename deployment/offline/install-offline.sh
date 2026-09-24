#!/bin/sh
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "Usage: $0 ENVIRONMENT_DIRECTORY [PYTHON_EXECUTABLE]" >&2
    exit 2
fi

environment_directory=$1
python_executable=${2:-python3}
bundle_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
wheelhouse="$bundle_root/wheelhouse"

if [ ! -d "$wheelhouse" ]; then
    echo "Bundle wheelhouse is missing." >&2
    exit 1
fi
if [ -e "$environment_directory" ]; then
    echo "Environment directory already exists; choose a new path." >&2
    exit 1
fi

"$python_executable" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 'Python 3.12+ is required')"
"$python_executable" -m venv "$environment_directory"
"$environment_directory/bin/python" -m pip install \
    --no-index --find-links "$wheelhouse" nexus-jar-sync
echo "Installed nexus-jar-sync into $environment_directory"
