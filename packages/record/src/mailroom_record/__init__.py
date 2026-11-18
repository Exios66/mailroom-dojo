"""mailroom_record: the append-only, hash-chained run record and its store.

The public surface is ``__all__`` (19 names, checked by ``tests/test_public_surface.py``);
everything else is reached through its module.
"""

from mailroom_record.blobs import BlobStore
from mailroom_record.importer.audit import audit_sandbox_run
from mailroom_record.importer.sandbox_run import import_sandbox_run
from mailroom_record.index import Index
from mailroom_record.log import EventLog
from mailroom_record.models import GENESIS, BlobRef, Event, Run, Seal
from mailroom_record.pack import pack_run, prune_run, unpack_run
from mailroom_record.replay import replay_timeline
from mailroom_record.seal import seal_run
from mailroom_record.trace import trace_doc, trace_run
from mailroom_record.verify import verify_run
from mailroom_record.versions import version_chain

__version__ = "0.1.0"

__all__ = [
    "GENESIS",
    "BlobRef",
    "BlobStore",
    "Event",
    "EventLog",
    "Index",
    "Run",
    "Seal",
    "audit_sandbox_run",
    "import_sandbox_run",
    "pack_run",
    "prune_run",
    "replay_timeline",
    "seal_run",
    "trace_doc",
    "trace_run",
    "unpack_run",
    "verify_run",
    "version_chain",
]
