"""Compatibility entry point for the glove flashing commands in tools.flash."""
import sys

from tools import flash as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

# Preserve the module's full API, including private helpers and patched globals.
sys.modules[__name__] = _implementation
