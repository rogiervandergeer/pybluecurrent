"""The MQTT relay: publish what your charge points report to an MQTT broker.

Install it with the ``mqtt`` extra (``pip install pybluecurrent[mqtt]``) and run it as
``pybluecurrent relay``. The client library itself does not depend on any of this.
"""

from pybluecurrent.relay.relay import run
from pybluecurrent.relay.settings import Settings

__all__ = ["Settings", "run"]
