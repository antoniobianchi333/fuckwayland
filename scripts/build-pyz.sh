#!/bin/sh
# Build dist/wdotool: a single-file, stdlib-only executable zipapp.
set -eu
cd "$(dirname "$0")/.."
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
