"""Allows `python3 -m publisher <command>`, the form the CI wrappers use."""

import sys

from .cli import main

sys.exit(main())
