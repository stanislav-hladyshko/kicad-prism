"""The KiCAD Prism Python SDK and ``prism`` CLI."""

from ._version import __version__
from .client import Client, PrismError

__all__ = ["Client", "PrismError", "__version__"]
