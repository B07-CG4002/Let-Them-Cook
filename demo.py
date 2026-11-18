"""Compatibility entry point for the Communications commands in tools.demo."""
import sys

from tools import demo as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

# Preserve the module's full API, including private helpers and patched globals.
sys.modules[__name__] = _implementation
