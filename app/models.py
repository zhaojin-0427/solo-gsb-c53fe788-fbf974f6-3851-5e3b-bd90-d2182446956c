"""Pydantic request models.

Decimal arrives as a *string* (or int/float); parsing goes through
``Decimal(str(...))`` so float literals never silently gain binary artefacts,
and non-finite values are rejected.  Sign/range validation happens in the
accounting engine, which aggregates *all* problems into one stable error
report instead of failing on the first one.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.functional_validators import BeforeValidator
from typing_extensions import Annotated

from .decimals import to_decimal


def _coerce_decimal(value):
    if isinstance(value, Decimal):
        dec = value
    else:
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("not a valid decimal number")
    if not dec.is_finite():
        raise ValueError("decimal value must be finite")
    # Decimal happily stores exponents like 1E+99999 as "finite"; arithmetic
    # would then overflow to Infinity. Reject absurd magnitudes up front.
    if dec != 0 and abs(dec.adjusted()) > 30:
        raise ValueError(
            "decimal magnitude out of range (exponent must be within 1e±30)")
    return dec


# Stored as Decimal, rendered back as string on the wire.
Dec = Annotated[
    Decimal,
    BeforeValidator(_coerce_decimal),
    Field(json_schema_extra={"type": "string"}),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectEmission(StrictModel):
    source: str = Field(..., description="排放源标识，如 natural_gas")
    quantity: Dec = Field(..., description="活动数据数量")
    unit: str = Field(..., description="活动数据单位，如 MJ")
    factor_ref: str = Field(..., description="排放因子标识，如 ef.natural_gas")


class MaterialInput(StrictModel):
    ref: str = Field(..., description="外部物料标识或上游产品标识")
    quantity: Dec
    unit: str


class ProductOutput(StrictModel):
    product_id: str
    name: Optional[str] = None
    quantity: Dec
    unit: str
    mass_weight: Dec = Field(default=Decimal("0"))
    energy_weight: Dec = Field(default=Decimal("0"))
    economic_weight: Dec = Field(default=Decimal("0"))


class Process(StrictModel):
    process_id: str
    name: Optional[str] = None
    batch_quantity: Dec = Field(..., description="批次产量")
    unit: str = Field(..., description="批次产量单位")
    direct_emissions: List[DirectEmission] = Field(default_factory=list)
    inputs: List[MaterialInput] = Field(default_factory=list)
    outputs: List[ProductOutput]


class ExternalMaterial(StrictModel):
    ref: str
    name: Optional[str] = None
    quantity: Dec
    unit: str
    factor_ref: str


class AccountingRequest(StrictModel):
    client_request_id: Optional[str] = Field(
        default=None, description="客户端幂等键（可选）"
    )
    factor_version: str = Field(..., description="排放因子版本，如 v2023-01")
    allocation_method: Literal["mass", "energy", "economic"]
    external_materials: List[ExternalMaterial] = Field(default_factory=list)
    processes: List[Process]


class RecalculateRequest(StrictModel):
    factor_version: str = Field(..., description="用于重算的新排放因子版本")
    client_request_id: Optional[str] = None


class FactorInput(StrictModel):
    ref: str
    name: Optional[str] = None
    unit: str = Field(..., description="因子适用的活动数据单位，如 kgCO2e/MJ")
    kg_co2e_per_unit: Dec = Field(..., description="单位活动数据 kgCO2e")


class FactorVersionRequest(StrictModel):
    version: str
    factors: List[FactorInput]
