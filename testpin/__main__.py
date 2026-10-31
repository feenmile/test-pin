"""Allow `python -m testpin` and `python path/to/testpin` (no install needed)."""

import os
import sys

if not __package__:
    # Run as a directory: replace the package folder on sys.path with its parent,
    # so `testpin` imports as a package and its modules cannot shadow anything.
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
    sys.path.insert(0, os.path.dirname(_here))
    from testpin.cli import main
else:
    from .cli import main

sys.exit(main())
