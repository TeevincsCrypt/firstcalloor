#!/usr/bin/env python3
"""FIRSTCALLOOR command-line entrypoint.

The engine lives in api/_engine.py so that Vercel's Python runtime bundles it
alongside the serverless handler (files in api/ are bundled; an underscore
prefix keeps it from becoming a route). The CLI and the website therefore run
byte-for-byte the same logic.

    python firstcalloor.py <CONTRACT_ADDRESS>
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))

from _engine import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
