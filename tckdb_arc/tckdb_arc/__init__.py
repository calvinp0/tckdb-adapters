"""ARC-side TCKDB integration: build, write, and optionally upload conformer/calculation payloads.

The chemistry/provenance mapping lives here in ARC. Transport lives in
``tckdb-client``. Server-side validation/persistence lives in TCKDB.
"""

from tckdb_arc.adapter import TCKDBAdapter, UploadOutcome
from tckdb_arc.config import TCKDBConfig
from tckdb_arc.sweep import run_upload_sweep

__all__ = ["TCKDBAdapter", "TCKDBConfig", "UploadOutcome", "run_upload_sweep"]
