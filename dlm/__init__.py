"""Package entry point for the resilient download engine."""
from .core import Downloader, Progress, Result, human, human_time
from . import errors, registry, adapters

__all__ = ["Downloader", "Progress", "Result", "human", "human_time",
           "errors", "registry", "adapters"]
__version__ = "1.0.0"
