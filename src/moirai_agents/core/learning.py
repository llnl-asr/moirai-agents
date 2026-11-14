"""moirai-agents' learning is phronix's: phronix.engine.learning, configured for moirai-agents (core._engine)."""
import sys

from . import _engine  # noqa: F401  (configures the engine for moirai-agents first)
from phronix.engine import learning as _m

sys.modules[__name__] = _m
