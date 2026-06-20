"""Production trade-reconciliation layer.

Broker executions → reconciled closed trades → trustworthy statistics.

Public surface:
    from reconciliation import reconcile, compute_metrics, ReconciledTrade
    from reconciliation import OPEN, PARTIAL, CLOSED, CANCELLED, REJECTED
"""
from reconciliation.model import (
    CANCELLED, CLOSED, OPEN, PARTIAL, REJECTED, VALID_STATUSES, ReconciledTrade,
)
from reconciliation.engine import reconcile
from reconciliation.metrics import compute_metrics

__all__ = [
    "reconcile", "compute_metrics", "ReconciledTrade",
    "OPEN", "PARTIAL", "CLOSED", "CANCELLED", "REJECTED", "VALID_STATUSES",
]
