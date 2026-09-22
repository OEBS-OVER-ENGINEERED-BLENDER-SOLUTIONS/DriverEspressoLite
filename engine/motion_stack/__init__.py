"""The Motion Stack: how one recipe's channels are compiled and layered.

This is not part of the generated-systems layer. It is the model the
multi-channel recipes run on -- a stack of layers, a blend mode per layer, and
a compiler that turns them into one expression per channel.
"""

from .contracts import *  # noqa: F401,F403
from .stack import *  # noqa: F401,F403
