"""Service layer: factor lookup, idempotent snapshot creation and diffing."""

from __future__ import annotations

import json
import sqlite3
import threading
from decimal import Decimal
from typing import Any, Dict, Optional

from . import db
from .accounting import Factor, calculate
from .decimals import percent_str, to_decimal, to_jsonable
from .errors import AccountingError, ConflictError, NotFoundError
from .idempotency import input_hash
from .models import AccountingRequest, FactorVersionRequest, RecalculateRequest

ZERO = Decimal("0")

# One shared SQLite connection per process: serialise check-then-write
# sequences so concurrent submissions can't interleave.  Factor-version
# registration additionally opens an IMMEDIATE transaction so that
# multi-process deployments get the same all-or-nothing guarantee.
_write_lock = threading.RLock()


def _serial(fn):
    def wrapper(*args, **kwargs):
        with _write_lock:
            return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


def _load_factors(conn: sqlite3.Connection, version: str
                  ) -> Dict[str, Factor]:
    raw = db.get_factors(conn, version)
    if not raw and not db.factor_version_exists(conn, version):
        raise NotFoundError(
            "FACTOR_VERSION_NOT_FOUND",
            f"排放因子版本不存在: {version}（可通过 GET /api/factor-versions 查询，"
            "或 POST /api/factor-versions 登记）")
    return {ref: (f["ref"], f["unit"], to_decimal(f["kg_co2e_per_unit"]),
                  f["name"])
            for ref, f in raw.items()}


def _factor_payloads(req: FactorVersionRequest) -> list:
    return [{"ref": f.ref, "name": f.name, "unit": f.unit,
             "kg_co2e_per_unit": f.kg_co2e_per_unit} for f in req.factors]


def _factor_version_diff(existing: Dict[str, Dict[str, Any]],
                         submitted: Dict[str, Any]) -> list:
    """Explain why a resubmitted version is not identical.

    Decimal values are compared numerically (``0.10`` equals ``0.1``);
    names, units and the set of factor refs are compared literally.
    """
    diffs = []
    for ref in sorted(set(existing) | set(submitted)):
        old = existing.get(ref)
        new = submitted.get(ref)
        if old is None:
            diffs.append({
                "factor_ref": ref, "change": "added_in_request",
                "name": new["name"], "unit": new["unit"],
                "kg_co2e_per_unit": new["kg_co2e_per_unit"]})
            continue
        if new is None:
            diffs.append({
                "factor_ref": ref, "change": "missing_in_request",
                "name": old["name"], "unit": old["unit"],
                "kg_co2e_per_unit": to_decimal(old["kg_co2e_per_unit"])})
            continue
        if (old["name"] or None) != (new["name"] or None):
            diffs.append({"factor_ref": ref, "change": "name_changed",
                          "old_name": old["name"], "new_name": new["name"]})
        if old["unit"] != new["unit"]:
            diffs.append({"factor_ref": ref, "change": "unit_changed",
                          "old_unit": old["unit"],
                          "new_unit": new["unit"]})
        old_value = to_decimal(old["kg_co2e_per_unit"])
        new_value = new["kg_co2e_per_unit"]
        if old_value != new_value:
            diffs.append({
                "factor_ref": ref, "change": "value_changed",
                "old_kg_co2e_per_unit": old_value,
                "new_kg_co2e_per_unit": new_value,
                "delta_kg_co2e_per_unit": new_value - old_value})
    return to_jsonable(diffs)


@_serial
def register_factor_version(conn: sqlite3.Connection,
                            req: FactorVersionRequest) -> Dict[str, Any]:
    """Register a factor version with append-only, conflict-aware semantics.

    * Unknown version  -> insert atomically, report ``created=True`` (201).
    * Existing version identical in factor-ref set, names, units and Decimal
      values -> idempotent no-op, report ``created=False`` (200).
    * Existing version with any difference -> 409, no row is inserted,
      updated or deleted; callers must publish changes under a new version
      name.

    The check-and-insert runs in one ``BEGIN IMMEDIATE`` transaction (the
    SQLite write lock is taken before the read), so two concurrent same-named
    submissions can never interleave into a partial write or silently
    overwrite one another.  The process-wide write lock serialises
    same-process callers as well.
    """
    seen = set()
    issues = []
    for f in req.factors:
        if f.ref in seen:
            issues.append({
                "path": f"factors[ref={f.ref}]",
                "code": "DUPLICATE_FACTOR",
                "message": f"因子标识在同一版本中重复: {f.ref}"})
        seen.add(f.ref)
    if issues:
        raise AccountingError(
            issues, code="FACTOR_VERSION_VALIDATION_FAILED",
            message="因子版本登记请求校验失败")

    submitted = {f["ref"]: f for f in _factor_payloads(req)}

    conn.execute("BEGIN IMMEDIATE")
    try:
        if not db.factor_version_exists(conn, req.version):
            db.insert_factor_version(
                conn, req.version, _factor_payloads(req))
            conn.commit()
            return {"version": req.version, "created": True,
                    "factor_count": len(req.factors)}

        differences = _factor_version_diff(
            db.get_factors(conn, req.version), submitted)
        conn.rollback()
        if not differences:
            return {"version": req.version, "created": False,
                    "factor_count": len(req.factors)}
        raise ConflictError(
            "FACTOR_VERSION_CONFLICT",
            f"因子版本 {req.version} 已存在且内容不同；版本一经登记即不可变，"
            "请使用新的版本名提交变更",
            {"version": req.version, "differences": differences})
    except ConflictError:
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _new_snapshot_id(inhash: str) -> str:
    return "snap_" + inhash[:16]


def _row_to_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "snapshot_id": row["id"],
        "input_hash": row["input_hash"],
        "client_request_id": row["client_request_id"],
        "factor_version": row["factor_version"],
        "allocation_method": row["allocation_method"],
        "source_snapshot": row["source_snapshot"],
        "created_at": row["created_at"],
        "result": db.parse_result(row),
    }


@_serial
def submit_assessment(conn: sqlite3.Connection,
                      req: AccountingRequest) -> Dict[str, Any]:
    """Idempotent assessment submission.

    The snapshot key is the canonical input summary plus factor version.
    Replaying the same payload returns the *same* snapshot.  A different
    payload reusing a ``client_request_id`` is rejected as a conflict.
    """
    inhash = input_hash(req)

    existing = db.get_snapshot_by_hash(conn, inhash)
    if existing is None and req.client_request_id:
        by_client = db.get_snapshot_by_client_id(
            conn, req.client_request_id)
        if by_client is not None:
            raise ConflictError(
                "CLIENT_REQUEST_ID_CONFLICT",
                f"client_request_id={req.client_request_id} 已用于另一核算请求",
                {"existing_snapshot_id": by_client["id"]})

    if existing is not None:
        snap = _row_to_snapshot(existing)
        snap["idempotent_replay"] = True
        return snap

    factors = _load_factors(conn, req.factor_version)
    result = to_jsonable(calculate(req, factors))

    # Store the original request; the hash itself is the evidence anchor.
    request_json = req.model_dump_json()
    snapshot_id = _new_snapshot_id(inhash)
    db.insert_snapshot(
        conn,
        snapshot_id=snapshot_id,
        input_hash=inhash,
        client_request_id=req.client_request_id,
        factor_version=req.factor_version,
        allocation_method=req.allocation_method,
        source_snapshot=None,
        request_json=request_json,
        result_json=json.dumps(result, ensure_ascii=False),
    )
    created = db.get_snapshot(conn, snapshot_id)
    snap = _row_to_snapshot(created)
    snap["idempotent_replay"] = False
    return snap


def list_snapshots(conn, limit: int, offset: int) -> Dict[str, Any]:
    rows = db.list_snapshots(conn, limit=limit, offset=offset)
    return {
        "snapshots": [{
            "snapshot_id": r["id"],
            "input_hash": r["input_hash"],
            "client_request_id": r["client_request_id"],
            "factor_version": r["factor_version"],
            "allocation_method": r["allocation_method"],
            "source_snapshot": r["source_snapshot"],
            "created_at": r["created_at"],
        } for r in rows],
        "limit": limit,
        "offset": offset,
    }


def get_snapshot(conn, snapshot_id: str) -> Dict[str, Any]:
    row = db.get_snapshot(conn, snapshot_id)
    if row is None:
        raise NotFoundError("SNAPSHOT_NOT_FOUND",
                            f"快照不存在: {snapshot_id}")
    return _row_to_snapshot(row)


def get_product(conn, snapshot_id: str, product_id: str) -> Dict[str, Any]:
    snap = get_snapshot(conn, snapshot_id)
    for product in snap["result"]["products"]:
        if product["product_id"] == product_id:
            return {
                "snapshot_id": snapshot_id,
                "factor_version": snap["factor_version"],
                "created_at": snap["created_at"],
                "product": product,
            }
    raise NotFoundError(
        "PRODUCT_NOT_FOUND",
        f"快照 {snapshot_id} 中不存在产品: {product_id}")


# -------- recalculation / diff --------------------------------------------

def _diff_products(old: Dict[str, Any], new: Dict[str, Any]) -> list:
    old_by_id = {p["product_id"]: p for p in old["products"]}
    new_by_id = {p["product_id"]: p for p in new["products"]}
    diffs = []
    for pid in sorted(set(old_by_id) | set(new_by_id)):
        o = old_by_id.get(pid)
        n = new_by_id.get(pid)
        if o is None or n is None:
            diffs.append({
                "product_id": pid,
                "status": "added" if o is None else "removed",
            })
            continue
        old_unit = to_decimal(o["unit_kg_co2e"])
        new_unit = to_decimal(n["unit_kg_co2e"])
        old_total = to_decimal(o["total_kg_co2e"])
        new_total = to_decimal(n["total_kg_co2e"])
        d_unit = new_unit - old_unit
        d_total = new_total - old_total
        pct = (d_unit / old_unit * Decimal("100")) if old_unit != 0 else ZERO
        diffs.append({
            "product_id": pid,
            "name": n.get("name"),
            "unit": n["unit"],
            "status": "unchanged" if d_unit == 0 else "changed",
            "old_factor_version": old["factor_version"],
            "new_factor_version": new["factor_version"],
            "old_unit_kg_co2e": old_unit,
            "new_unit_kg_co2e": new_unit,
            "delta_unit_kg_co2e": d_unit,
            "delta_percent": percent_str(pct),
            "old_total_kg_co2e": old_total,
            "new_total_kg_co2e": new_total,
            "delta_total_kg_co2e": d_total,
        })
    return to_jsonable(diffs)


def _factor_changes(old_result: Dict[str, Any],
                    new_result: Dict[str, Any]) -> list:
    """Factor-value differences evidenced *by the two snapshots*.

    Both sides come from the snapshots' sealed ``factors_used`` copies, never
    from the live factor table: even if the old version's catalogue rows were
    rewritten before immutability was enforced, the "old" values reported here
    are exactly the values the old snapshot was computed with (and therefore
    match ``product_diffs``).  Only factors referenced by this assessment are
    evidence-relevant.
    """
    old = {f["ref"]: f for f in old_result.get("factors_used", [])}
    new = {f["ref"]: f for f in new_result.get("factors_used", [])}
    refs = sorted(set(old) | set(new))
    changes = []
    for ref in refs:
        if ref not in old:
            changes.append({"factor_ref": ref, "status": "added",
                            "new_unit": new[ref]["unit"],
                            "new_kg_co2e_per_unit":
                                to_decimal(new[ref]["kg_co2e_per_unit"])})
        elif ref not in new:
            changes.append({"factor_ref": ref, "status": "removed",
                            "old_unit": old[ref]["unit"],
                            "old_kg_co2e_per_unit":
                                to_decimal(old[ref]["kg_co2e_per_unit"])})
        else:
            ov = to_decimal(old[ref]["kg_co2e_per_unit"])
            nv = to_decimal(new[ref]["kg_co2e_per_unit"])
            if ov != nv or old[ref]["unit"] != new[ref]["unit"]:
                changes.append({
                    "factor_ref": ref, "status": "changed",
                    "old_unit": old[ref]["unit"], "new_unit": new[ref]["unit"],
                    "old_kg_co2e_per_unit": ov,
                    "new_kg_co2e_per_unit": nv,
                    "delta_kg_co2e_per_unit": nv - ov,
                })
    return to_jsonable(changes)


@_serial
def recalculate(conn, snapshot_id: str, body: RecalculateRequest
                ) -> Dict[str, Any]:
    """Replay an archived input summary with a new factor version.

    The old snapshot is never modified: a brand-new immutable snapshot is
    stored and returned together with per-product and per-factor diffs.
    Replaying the same (snapshot, new version) pair is idempotent.
    """
    row = db.get_snapshot(conn, snapshot_id)
    if row is None:
        raise NotFoundError("SNAPSHOT_NOT_FOUND",
                            f"快照不存在: {snapshot_id}")
    if row["factor_version"] == body.factor_version:
        raise ConflictError(
            "SAME_FACTOR_VERSION",
            f"新因子版本与原快照相同 ({body.factor_version})，无差异可计算")

    old_result = db.parse_result(row)

    # Rebuild the canonical request from the stored original request, but
    # swap the factor version.
    req = AccountingRequest.model_validate_json(row["request_json"])
    req = req.model_copy(update={"factor_version": body.factor_version,
                                 "client_request_id": body.client_request_id})

    inhash = input_hash(req)
    existing = db.get_snapshot_by_hash(conn, inhash)
    if existing is not None:
        new_snap = _row_to_snapshot(existing)
        new_snap["idempotent_replay"] = True
    else:
        if body.client_request_id:
            by_client = db.get_snapshot_by_client_id(
                conn, body.client_request_id)
            if by_client is not None and by_client["id"] != snapshot_id:
                raise ConflictError(
                    "CLIENT_REQUEST_ID_CONFLICT",
                    f"client_request_id={body.client_request_id} "
                    "已用于另一核算请求",
                    {"existing_snapshot_id": by_client["id"]})
        factors = _load_factors(conn, body.factor_version)
        new_result = to_jsonable(calculate(req, factors))
        new_id = _new_snapshot_id(inhash)
        db.insert_snapshot(
            conn,
            snapshot_id=new_id,
            input_hash=inhash,
            client_request_id=body.client_request_id,
            factor_version=body.factor_version,
            allocation_method=row["allocation_method"],
            source_snapshot=snapshot_id,
            request_json=req.model_dump_json(),
            result_json=json.dumps(new_result, ensure_ascii=False),
        )
        new_snap = _row_to_snapshot(db.get_snapshot(conn, new_id))
        new_snap["idempotent_replay"] = False

    diffs = _diff_products(old_result, new_snap["result"])
    changed = [d for d in diffs if d["status"] != "unchanged"]
    return {
        "source_snapshot_id": snapshot_id,
        "source_factor_version": row["factor_version"],
        "new_snapshot": new_snap,
        "factor_changes": _factor_changes(old_result, new_snap["result"]),
        "product_diffs": diffs,
        "changed_product_count": len(changed),
    }
