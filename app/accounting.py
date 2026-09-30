"""Carbon-footprint accounting engine.

Pipeline
--------
1. *Validate* the whole request and collect every problem (negative amounts,
   unknown references, missing factors, unit mismatches, cycles, zero
   allocation denominators ...).  Problems are returned together, sorted
   deterministically.
2. *Topologically order* process nodes via Kahn's algorithm.
3. For each process in order:

   * direct emission = quantity * factor (kg CO2e/unit);
   * external-material footprint = quantity * factor;
   * upstream-product footprint is pulled in and scaled by the consumed
     fraction of the upstream batch;
   * all input footprints + direct emissions are allocated to the co-product
     outputs by mass / energy / economic weight.

Every product keeps a list of *contributions* — an auditable chain from the
final product back to each external material or direct emission source.

All arithmetic is ``Decimal``; nothing is quantised until serialisation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .decimals import high_precision
from .graph import find_cycle, topological_order
from .models import AccountingRequest
from .errors import AccountingError

ZERO = Decimal("0")

# Factor record: (factor_ref, unit, kg_co2e_per_unit, name)
Factor = Tuple[str, str, Decimal, Optional[str]]
WEIGHT_FIELD = {"mass": "mass_weight", "energy": "energy_weight",
                "economic": "economic_weight"}


@dataclass
class Contribution:
    amount: Decimal
    path: List[str]
    kind: str          # "direct_emission" | "external_material"
    source: str        # emission source or external material ref
    factor_ref: str
    factor_version: str
    factor_unit: str
    factor_value: Decimal
    detail: str


def _issue(path: str, code: str, message: str) -> Dict[str, str]:
    return {"path": path, "code": code, "message": message}


def validate(req: AccountingRequest, factors: Dict[str, Factor]) -> None:
    """Collect every validation problem, then raise once."""
    issues: List[Dict[str, str]] = []

    # ---- collect ids -----------------------------------------------------
    ext = {e.ref: e for e in req.external_materials}
    if len(ext) != len(req.external_materials):
        seen = set()
        for e in req.external_materials:
            if e.ref in seen:
                issues.append(_issue(
                    f"external_materials[ref={e.ref}]", "DUPLICATE_ID",
                    f"外部物料标识重复: {e.ref}"))
            seen.add(e.ref)

    procs: Dict[str, Any] = {}
    for p in req.processes:
        if p.process_id in procs:
            issues.append(_issue(
                f"processes[process_id={p.process_id}]", "DUPLICATE_ID",
                f"过程标识重复: {p.process_id}"))
        procs[p.process_id] = p

    products: Dict[str, Any] = {}
    product_owner: Dict[str, str] = {}
    for p in req.processes:
        for o in p.outputs:
            if o.product_id in products:
                issues.append(_issue(
                    f"processes[process_id={p.process_id}]"
                    f".outputs[product_id={o.product_id}]",
                    "DUPLICATE_PRODUCT",
                    f"产品标识被多个产出声明: {o.product_id}"))
            products[o.product_id] = o
            product_owner[o.product_id] = p.process_id

    # Ambiguous references: a ref that is both a product and an external
    # material could be misread by downstream inputs.
    ambiguous = set(ext) & set(products)
    for ref in sorted(ambiguous):
        issues.append(_issue(f"ref:{ref}", "AMBIGUOUS_REFERENCE",
                             f"标识同时是外部物料和产品: {ref}"))

    # ---- numeric sign checks --------------------------------------------
    def check_amount(path: str, value: Decimal, label: str,
                     allow_zero: bool = False):
        if value < 0:
            issues.append(_issue(path, "NEGATIVE_AMOUNT", f"{label}不能为负"))
        elif not allow_zero and value == 0:
            issues.append(_issue(path, "ZERO_AMOUNT", f"{label}不能为零"))

    for e in req.external_materials:
        base = f"external_materials[ref={e.ref}]"
        check_amount(f"{base}.quantity", e.quantity, "外部物料数量")
        fac = factors.get(e.factor_ref)
        if fac is not None and fac[2] < 0:
            issues.append(_issue(
                f"external_materials[ref={e.ref}].factor:kg_co2e_per_unit",
                "NEGATIVE_AMOUNT", "排放因子数值不能为负"))

    for p in req.processes:
        base = f"processes[process_id={p.process_id}]"
        check_amount(f"{base}.batch_quantity", p.batch_quantity, "批次产量")
        for i, em in enumerate(p.direct_emissions):
            check_amount(f"{base}.direct_emissions[{i}].quantity",
                         em.quantity, "直接排放活动数据")
        for i, inp in enumerate(p.inputs):
            check_amount(f"{base}.inputs[{i}].quantity",
                         inp.quantity, "投入数量")
        for i, o in enumerate(p.outputs):
            op = f"{base}.outputs[product_id={o.product_id}]"
            check_amount(f"{op}.quantity", o.quantity, "产品产出数量")
            for method, field in (("mass", "mass_weight"),
                                  ("energy", "energy_weight"),
                                  ("economic", "economic_weight")):
                w = getattr(o, field)
                if w < 0:
                    issues.append(_issue(f"{op}.{field}", "NEGATIVE_AMOUNT",
                                         f"{method}分摊权重不能为负"))

    # ---- factor existence / units (direct + external) --------------------
    def check_factor(path: str, factor_ref: str, unit: str, owner: str):
        fac = factors.get(factor_ref)
        if fac is None:
            issues.append(_issue(path, "MISSING_FACTOR",
                                 f"因子版本 {req.factor_version} 中缺少排放因子: "
                                 f"{factor_ref} ({owner})"))
        elif fac[1] != unit:
            issues.append(_issue(path, "FACTOR_UNIT_MISMATCH",
                                 f"因子 {factor_ref} 单位为 {fac[1]}，"
                                 f"与活动数据单位 {unit} 不一致"))

    for e in req.external_materials:
        check_factor(f"external_materials[ref={e.ref}].factor_ref",
                     e.factor_ref, e.unit, f"外部物料 {e.ref}")
    for p in req.processes:
        for i, em in enumerate(p.direct_emissions):
            check_factor(
                f"processes[process_id={p.process_id}]"
                f".direct_emissions[{i}].factor_ref",
                em.factor_ref, em.unit,
                f"过程 {p.process_id} 排放源 {em.source}")

    # ---- graph: input references, flow feasibility, allocation -----------
    edges: Dict[str, List[str]] = {pid: [] for pid in procs}
    consumed: Dict[str, Decimal] = {}

    for p in req.processes:
        base = f"processes[process_id={p.process_id}]"
        for i, inp in enumerate(p.inputs):
            ipath = f"{base}.inputs[{i}]"
            if inp.ref in products:
                owner = product_owner[inp.ref]
                if owner == p.process_id:
                    issues.append(_issue(ipath, "SELF_REFERENCE",
                                         f"过程不能投入本过程的产品: {inp.ref}"))
                else:
                    edges.setdefault(owner, [])
                    if p.process_id not in edges[owner]:
                        edges[owner].append(p.process_id)
                out = products[inp.ref]
                if inp.unit != out.unit:
                    issues.append(_issue(f"{ipath}.unit", "UNIT_MISMATCH",
                                         f"投入 {inp.ref} 单位 {inp.unit} 与产出"
                                         f"单位 {out.unit} 不一致"))
                consumed[inp.ref] = consumed.get(inp.ref, ZERO) + inp.quantity
            elif inp.ref in ext:
                emat = ext[inp.ref]
                if inp.unit != emat.unit:
                    issues.append(_issue(f"{ipath}.unit", "UNIT_MISMATCH",
                                         f"投入 {inp.ref} 单位 {inp.unit} 与外部"
                                         f"物料单位 {emat.unit} 不一致"))
                consumed[inp.ref] = consumed.get(inp.ref, ZERO) + inp.quantity
            else:
                issues.append(_issue(ipath, "UNKNOWN_REFERENCE",
                                     f"投入引用了未登记的物料或产品: {inp.ref}"))

        weight_field = WEIGHT_FIELD[req.allocation_method]
        denom = sum((getattr(o, weight_field) for o in p.outputs), ZERO)
        if denom <= 0:
            issues.append(_issue(
                f"{base}.allocation:{req.allocation_method}",
                "ALLOCATION_DENOMINATOR_ZERO",
                f"过程 {p.process_id} 的{req.allocation_method}分摊权重之和为零，"
                "无法分摊"))

    # Flow feasibility (single-use is reported at the edge; multi-use is
    # reported once at the product/material aggregate).
    for ref, qty in consumed.items():
        available = (products[ref].quantity if ref in products
                     else ext[ref].quantity)
        if qty > available:
            path = (f"product[product_id={ref}].consumed"
                    if ref in products
                    else f"external_materials[ref={ref}].consumed")
            issues.append(_issue(
                path, "FLOW_EXCEEDS_OUTPUT",
                f"{'产品' if ref in products else '外部物料'} {ref} 累计投入 "
                f"{qty} 超过可用数量 {available}"))

    # ---- cycle -----------------------------------------------------------
    cycle = find_cycle(procs.keys(), edges)
    if cycle is not None:
        issues.append(_issue(
            "processes.graph", "CYCLE_DETECTED",
            "过程投入有向图存在环: " + " -> ".join(cycle)))

    if issues:
        raise AccountingError(issues)


def _used_factor(factors: Dict[str, Factor], ref: str, version: str
                 ) -> Dict[str, Any]:
    _fid, unit, value, name = factors[ref]
    return {"ref": ref, "name": name, "unit": unit,
            "kg_co2e_per_unit": value, "factor_version": version}


def calculate(req: AccountingRequest,
              factors: Dict[str, Factor]) -> Dict[str, Any]:
    """Validate then compute footprint, contribution chains and evidence."""
    validate(req, factors)
    version = req.factor_version
    weight_field = WEIGHT_FIELD[req.allocation_method]

    externals = {e.ref: e for e in req.external_materials}
    proc_by_id = {p.process_id: p for p in req.processes}

    # Graph of process -> consuming processes.
    edges: Dict[str, List[str]] = {pid: [] for pid in proc_by_id}
    product_owner: Dict[str, str] = {}
    for p in req.processes:
        for o in p.outputs:
            product_owner[o.product_id] = p.process_id
    for p in req.processes:
        for inp in p.inputs:
            if inp.ref in product_owner:
                owner = product_owner[inp.ref]
                if owner != p.process_id:
                    edges.setdefault(owner, [])
                    if p.process_id not in edges[owner]:
                        edges[owner].append(p.process_id)

    order, remaining = topological_order(proc_by_id.keys(), edges)
    if remaining:  # validate() should have caught this; defensive guard.
        cycle = find_cycle(proc_by_id.keys(), edges) or remaining
        raise AccountingError([_issue("processes.graph", "CYCLE_DETECTED",
                                      "过程投入有向图存在环: " + " -> ".join(cycle))])

    # Per-product accumulated state.
    total: Dict[str, Decimal] = {}
    leaves: Dict[str, List[Contribution]] = {}
    produced: Dict[str, Decimal] = {}

    process_results: List[Dict[str, Any]] = []
    used: Dict[str, Factor] = {}

    def extend(product: str, new_leaves: List[Contribution],
               factor_refs=()):
        leaves.setdefault(product, [])
        leaves[product].extend(new_leaves)
        total[product] = total.get(product, ZERO) + sum(
            (c.amount for c in new_leaves), ZERO)
        for ref in factor_refs:
            used[ref] = factors[ref]

    with high_precision():
        for pid in order:
            p = proc_by_id[pid]
            proc_direct = ZERO
            proc_inputs_fp = ZERO

            # A process with no inputs/emissions still produces products with
            # a zero footprint: make sure their accumulators exist.
            for o in p.outputs:
                leaves.setdefault(o.product_id, [])
                total.setdefault(o.product_id, ZERO)

            # Direct emissions --------------------------------------------
            for em in p.direct_emissions:
                fac = factors[em.factor_ref]
                amount = em.quantity * fac[2]
                proc_direct += amount
                for o in p.outputs:
                    extend(o.product_id, [Contribution(
                        amount=amount,
                        path=[pid, "direct_emission", em.source,
                              o.product_id],
                        kind="direct_emission",
                        source=em.source,
                        factor_ref=em.factor_ref,
                        factor_version=version,
                        factor_unit=fac[1],
                        factor_value=fac[2],
                        detail=f"{em.quantity} {em.unit} × {fac[2]} "
                               f"kgCO2e/{em.unit}（在 {pid} 直接排放）")],
                        factor_refs=[em.factor_ref])

            # Material / upstream inputs ----------------------------------
            for inp in p.inputs:
                if inp.ref in externals:
                    e = externals[inp.ref]
                    fac = factors[e.factor_ref]
                    amount = inp.quantity * fac[2]
                    proc_inputs_fp += amount
                    for o in p.outputs:
                        extend(o.product_id, [Contribution(
                            amount=amount,
                            path=["external_material", e.ref, pid,
                                  o.product_id],
                            kind="external_material",
                            source=e.ref,
                            factor_ref=e.factor_ref,
                            factor_version=version,
                            factor_unit=fac[1],
                            factor_value=fac[2],
                            detail=f"{inp.quantity} {inp.unit} × {fac[2]} "
                                   f"kgCO2e/{inp.unit}（外部物料 {e.ref}）")],
                            factor_refs=[e.factor_ref])
                else:
                    # Upstream product: pull its contributions, scaled by the
                    # fraction of the upstream batch that flows here.
                    up_total = total[inp.ref]
                    scale = inp.quantity / produced[inp.ref]
                    share = up_total * scale
                    proc_inputs_fp += share
                    for o in p.outputs:
                        scaled = []
                        for c in leaves[inp.ref]:
                            scaled.append(Contribution(
                                amount=c.amount * scale,
                                path=c.path[:-1] + [pid, o.product_id],
                                kind=c.kind,
                                source=c.source,
                                factor_ref=c.factor_ref,
                                factor_version=c.factor_version,
                                factor_unit=c.factor_unit,
                                factor_value=c.factor_value,
                                detail=c.detail + (
                                    f"；沿 {inp.ref} 传递 "
                                    f"{inp.quantity}/{produced[inp.ref]}")))
                        extend(o.product_id, scaled)

            proc_total = proc_direct + proc_inputs_fp

            # Allocate to co-product outputs -------------------------------
            denom = sum((getattr(o, weight_field) for o in p.outputs), ZERO)
            output_rows = []
            for o in p.outputs:
                w = getattr(o, weight_field)
                fraction = w / denom
                produced[o.product_id] = o.quantity
                # Every contribution entering this process was tentatively
                # attached to every output in full; multiply each by its
                # allocation fraction.
                for c in leaves[o.product_id]:
                    owner = product_owner[o.product_id]
                    if owner == pid:
                        c.amount *= fraction
                total[o.product_id] = sum((c.amount for c in leaves[o.product_id]),
                                          ZERO)
                allocated = proc_total * fraction
                output_rows.append({
                    "product_id": o.product_id,
                    "name": o.name,
                    "quantity": o.quantity,
                    "unit": o.unit,
                    "allocation_weight": w,
                    "allocation_fraction": fraction,
                    "allocated_kg_co2e": allocated,
                    "unit_kg_co2e": total[o.product_id] / o.quantity,
                })

            process_results.append({
                "process_id": pid,
                "name": p.name,
                "direct_kg_co2e": proc_direct,
                "incoming_kg_co2e": proc_inputs_fp,
                "total_kg_co2e": proc_total,
                "outputs": output_rows,
            })

    # ---- assemble product rows in deterministic order --------------------
    product_rows = []
    for pid in order:
        p = proc_by_id[pid]
        for o in p.outputs:
            prod = o.product_id
            cl = sorted(leaves.get(prod, []),
                        key=lambda c: (-c.amount, c.path, c.factor_ref))
            fp = total[prod]
            contributions = [{
                "kg_co2e": c.amount,
                "share_of_product": (c.amount / fp) if fp != 0 else ZERO,
                "chain": c.path,
                "kind": c.kind,
                "source": c.source,
                "factor_ref": c.factor_ref,
                "factor_version": c.factor_version,
                "factor_unit": c.factor_unit,
                "factor_kg_co2e_per_unit": c.factor_value,
                "detail": c.detail,
            } for c in cl]
            product_rows.append({
                "product_id": prod,
                "name": o.name,
                "producing_process": pid,
                "quantity": o.quantity,
                "unit": o.unit,
                "total_kg_co2e": fp,
                "unit_kg_co2e": fp / o.quantity,
                "contributions": contributions,
            })

    return {
        "factor_version": version,
        "allocation_method": req.allocation_method,
        "topological_order": order,
        "processes": process_results,
        "products": product_rows,
        "factors_used": [_used_factor(used, ref, version)
                         for ref in sorted(used)],
    }
