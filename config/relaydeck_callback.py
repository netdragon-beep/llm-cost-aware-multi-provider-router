from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from relaydeck_litellm_callback import proxy_handler_instance  # noqa: E402,F401


__all__ = ["proxy_handler_instance"]
