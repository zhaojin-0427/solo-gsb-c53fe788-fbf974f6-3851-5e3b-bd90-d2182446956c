"""FastAPI application: product carbon-footprint accounting & evidence API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db, seed, service
from .errors import AccountingError, ConflictError, NotFoundError
from .models import (AccountingRequest, FactorVersionRequest,
                     RecalculateRequest)

DEFAULT_DB = os.environ.get("CARBON_DB_PATH", "data/carbon.db")


def create_app(db_path: Optional[str] = None) -> FastAPI:
    path = db_path if db_path is not None else DEFAULT_DB

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        conn = db.connect(path)
        db.init_db(conn)
        seed.seed(conn)
        app.state.db = conn
        try:
            yield
        finally:
            conn.close()

    app = FastAPI(
        title="产品碳足迹核算与证据 API",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "登记过程批次、物料投入与多产品产出有向图，按质量/能量/经济价值"
            "分摊，输出每个产品的单位 CO₂e、贡献链与不可变证据快照；"
            "支持按新因子版本重算并返回逐产品差异。"
        ),
    )

    def get_db() -> db.sqlite3.Connection:
        return app.state.db

    # ---------------- exception envelopes -------------------------------

    @app.exception_handler(AccountingError)
    async def accounting_error_handler(request: Request, exc: AccountingError):
        return JSONResponse(status_code=422, content={
            "error": {"code": exc.code, "message": exc.message,
                      "issues": exc.issues}})

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={
            "error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={
            "error": {"code": exc.code, "message": exc.message,
                      "details": exc.details}})

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request,
                                         exc: RequestValidationError):
        issues = sorted(
            ({"path": ".".join(str(x) for x in err["loc"] if x != "body"),
              "code": err["type"],
              "message": err["msg"]}
             for err in exc.errors()),
            key=lambda it: (it["path"], it["code"]))
        return JSONResponse(status_code=422, content={
            "error": {"code": "REQUEST_VALIDATION_FAILED",
                      "message": "请求体格式校验失败", "issues": issues}})

    # ---------------- meta -----------------------------------------------

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    @app.get("/api/factor-versions", tags=["factors"])
    def list_factor_versions(c: db.sqlite3.Connection = Depends(get_db)):
        versions = []
        for version in db.list_factor_versions(c):
            factors = db.get_factors(c, version)
            versions.append({
                "version": version,
                "factor_count": len(factors),
                "factors": [
                    {"ref": f["ref"], "name": f["name"], "unit": f["unit"],
                     "kg_co2e_per_unit": f["kg_co2e_per_unit"]}
                    for f in factors.values()],
            })
        return {"factor_versions": versions}

    @app.post("/api/factor-versions", status_code=201, tags=["factors"])
    def create_factor_version(body: FactorVersionRequest,
                              c: db.sqlite3.Connection = Depends(get_db)):
        existing = set(db.list_factor_versions(c))
        created = body.version not in existing
        # Idempotent upsert: re-POST identical data is 200, new version 201.
        db.upsert_factor_version(c, body.version, [
            {"ref": f.ref, "name": f.name, "unit": f.unit,
             "kg_co2e_per_unit": f.kg_co2e_per_unit}
            for f in body.factors])
        return JSONResponse(
            status_code=201 if created else 200,
            content={"version": body.version, "created": created,
                     "factor_count": len(body.factors)})

    # ---------------- assessment / snapshots ----------------------------

    @app.post("/api/assessments", status_code=201, tags=["accounting"])
    def submit_assessment(body: AccountingRequest,
                          c: db.sqlite3.Connection = Depends(get_db)):
        snap = service.submit_assessment(c, body)
        return JSONResponse(
            status_code=200 if snap["idempotent_replay"] else 201,
            content=snap)

    @app.get("/api/snapshots", tags=["evidence"])
    def list_snapshots(limit: int = Query(100, ge=1, le=500),
                       offset: int = Query(0, ge=0),
                       c: db.sqlite3.Connection = Depends(get_db)):
        return service.list_snapshots(c, limit, offset)

    @app.get("/api/snapshots/{snapshot_id}", tags=["evidence"])
    def get_snapshot(snapshot_id: str,
                     c: db.sqlite3.Connection = Depends(get_db)):
        return service.get_snapshot(c, snapshot_id)

    @app.get("/api/snapshots/{snapshot_id}/products/{product_id}",
             tags=["evidence"])
    def get_product(snapshot_id: str, product_id: str,
                    c: db.sqlite3.Connection = Depends(get_db)):
        return service.get_product(c, snapshot_id, product_id)

    @app.post("/api/snapshots/{snapshot_id}/recalculate",
              status_code=201, tags=["recalculation"])
    def recalculate(snapshot_id: str, body: RecalculateRequest,
                    c: db.sqlite3.Connection = Depends(get_db)):
        result = service.recalculate(c, snapshot_id, body)
        replay = result["new_snapshot"]["idempotent_replay"]
        return JSONResponse(status_code=200 if replay else 201,
                            content=result)

    return app


app = create_app()
