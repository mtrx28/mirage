"""The rule that nothing speaks to a caller without the safety gate. One tiny module so the call session and the
speaking path can both use it without importing each other."""
from __future__ import annotations


class GateRequired(RuntimeError):
    """Raised when something tries to run the voice path without the safety gate."""


def require_gate(gate: object) -> object:
    if gate is None or not callable(gate):
        raise GateRequired("The voice path will not start without the safety gate (mirage.safety.Gate). "
                           "Nothing may be spoken to a caller that has not passed it.")
    return gate
