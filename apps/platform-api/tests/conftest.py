"""Test-wide compatibility shims.

The project source uses ``datetime.UTC`` (PEP 495, Python 3.11+). When the
local execution runtime is older, this patch makes ``from datetime import UTC``
work for both test modules and the source modules they import, without touching
production code.
"""

import datetime

if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc
