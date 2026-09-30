"""Canonical input summary and idempotency hash.

The hash covers the *semantic* input summary (nodes, quantities, units,
factor references, allocation method and factor version), not request
metadata like ``client_request_id`` or display names.  Lists are sorted so
re-ordered submissions still hit the same snapshot.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from .decimals import canonical
from .models import AccountingRequest


def _d(value) -> str:
    return canonical(value)


def build_canonical_payload(req: AccountingRequest) -> Dict[str, Any]:
    externals = sorted(
        (
            {
                "ref": e.ref,
                "quantity": _d(e.quantity),
                "unit": e.unit,
                "factor_ref": e.factor_ref,
            }
            for e in req.external_materials
        ),
        key=lambda e: e["ref"],
    )
    processes = []
    for p in sorted(req.processes, key=lambda p: p.process_id):
        emissions = sorted(
            (
                {
                    "source": e.source,
                    "quantity": _d(e.quantity),
                    "unit": e.unit,
                    "factor_ref": e.factor_ref,
                }
                for e in p.direct_emissions
            ),
            key=lambda e: (e["source"], e["factor_ref"], e["unit"], e["quantity"]),
        )
        inputs = sorted(
            (
                {"ref": i.ref, "quantity": _d(i.quantity), "unit": i.unit}
                for i in p.inputs
            ),
            key=lambda i: (i["ref"], i["unit"], i["quantity"]),
        )
        outputs = sorted(
            (
                {
                    "product_id": o.product_id,
                    "quantity": _d(o.quantity),
                    "unit": o.unit,
                    "mass_weight": _d(o.mass_weight),
                    "energy_weight": _d(o.energy_weight),
                    "economic_weight": _d(o.economic_weight),
                }
                for o in p.outputs
            ),
            key=lambda o: o["product_id"],
        )
        processes.append(
            {
                "process_id": p.process_id,
                "batch_quantity": _d(p.batch_quantity),
                "unit": p.unit,
                "direct_emissions": emissions,
                "inputs": inputs,
                "outputs": outputs,
            }
        )
    return {
        "factor_version": req.factor_version,
        "allocation_method": req.allocation_method,
        "external_materials": externals,
        "processes": processes,
    }


def input_hash(req: AccountingRequest) -> str:
    payload = build_canonical_payload(req)
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
