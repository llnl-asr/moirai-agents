"""moirai-agents' selfrefresh is phronix's: phronix.engine.selfrefresh, configured for moirai-agents (core._engine)."""
import sys

from . import _engine  # noqa: F401  (configures the engine for moirai-agents first)
from phronix.engine import selfrefresh as _m

sys.modules[__name__] = _m
