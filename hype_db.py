from __future__ import annotations

"""
hype_db.py
----------
Compatibility facade for the Hype Wave database layer.

The implementation is split by responsibility:
- hype_db_common.py: pure helpers and configuration-derived utilities
- hype_db_schema.py: database connection, schema, migrations, and views
- hype_db_store.py: track persistence, match cache, crawl-run storage, and audit records
- hype_db_reports.py: Hype reports and frontend history export
"""

import hype_db_common as _common
import hype_db_reports as _reports
import hype_db_schema as _schema
import hype_db_store as _store

from hype_db_common import *  # noqa: F401,F403
from hype_db_schema import *  # noqa: F401,F403
from hype_db_store import *  # noqa: F401,F403
from hype_db_reports import *  # noqa: F401,F403

__all__ = [
    *_common.__all__,
    *_schema.__all__,
    *_store.__all__,
    *_reports.__all__,
]
