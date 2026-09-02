#!/bin/sh
# Build dist/wdotool and dist/wwmctl: single-file, stdlib-only executable zipapps.
set -eu
cd "$(dirname "$0")/.."

# dist/wdotool
rm -rf dist/.stage
mkdir -p dist/.stage
cp -r wdotool dist/.stage/wdotool
find dist/.stage -name __pycache__ -type d -exec rm -rf {} +
cat > dist/.stage/__main__.py <<'EOF'
import sys
from wdotool.cli import main

sys.exit(main())
EOF
python3 -m zipapp dist/.stage -p "/usr/bin/env python3" -o dist/wdotool
rm -rf dist/.stage
chmod +x dist/wdotool
echo "built dist/wdotool ($(wc -c < dist/wdotool) bytes)"

# dist/wwmctl (wwmctl.core reuses the wdotool backend machinery, so both
# packages ride along; entry point is wwmctl.cli:main)
rm -rf dist/.stage
mkdir -p dist/.stage
cp -r wdotool dist/.stage/wdotool
cp -r wwmctl dist/.stage/wwmctl
find dist/.stage -name __pycache__ -type d -exec rm -rf {} +
cat > dist/.stage/__main__.py <<'EOF'
import sys
from wwmctl.cli import main

sys.exit(main())
EOF
python3 -m zipapp dist/.stage -p "/usr/bin/env python3" -o dist/wwmctl
rm -rf dist/.stage
chmod +x dist/wwmctl
echo "built dist/wwmctl ($(wc -c < dist/wwmctl) bytes)"
