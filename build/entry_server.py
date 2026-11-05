"""PyInstaller entry point. A plain script, because the bundler cannot start from ``-m``."""

import sys

from server.main import main

sys.exit(main())
