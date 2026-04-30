"""
Advisory routes.

  POST /advisory                  → all 6 engines (E1-E6) in dependency tiers
  POST /advisory/eng1             → just E1 (stage)
  POST /advisory/eng2             → E2 + transparently E1 (upstream)
  POST /advisory/eng3             → E3 + E1
  POST /advisory/eng4             → E4 + E1
  POST /advisory/eng5             → E5 + E1
  POST /advisory/eng6             → E6 + E1 + E5

WHY one combined endpoint AND six per-engine endpoints (mirrors agri-integrated):
  Production callers want the full advisory in a single round-trip
  (`/advisory`). Developers and QA want to exercise individual engines in
  isolation (`/advisory/engN`). Per-engine endpoints transparently run any
  upstream engines they depend on so callers don't have to know the
  dependency graph — the response includes an `upstream` map for visibility.
"""

from fastapi import APIRouter, HTTPException

from app.advisory import generate_advisories
from app.advisory.context import AdvisoryContext
from app.advisory.orchestrator import generate_single

router = APIRouter()


def _validate_k(k: int) -> None:
    if k < 1 or k > 10:
        raise HTTPException(
            status_code=400,
            detail="k must be between 1 and 10",
        )


@router.post("/advisory", tags=["advisory"])
async def advisory(context: AdvisoryContext, k: int = 1):
    _validate_k(k)
    return await generate_advisories(context, k=k)


# Per-engine endpoints — same body shape, single engine output. The path
# segment names match the agri-integrated convention (eng1..eng6) so a
# tester moving between the two systems can keep the same mental model.
_ENGINE_ROUTE_MAP = {
    "eng1": "e1_stage",
    "eng2": "e2_irrigation",
    "eng3": "e3_nutrition",
    "eng4": "e4_crop_health",
    "eng5": "e5_yield",
    "eng6": "e6_financial",
}


def _make_engine_route(path_name: str, engine_id: str):
    @router.post(f"/advisory/{path_name}", tags=["advisory"])
    async def _endpoint(context: AdvisoryContext, k: int = 1):
        _validate_k(k)
        return await generate_single(context, engine_id=engine_id, k=k)
    _endpoint.__name__ = f"advisory_{path_name}"
    return _endpoint


for _path, _engine in _ENGINE_ROUTE_MAP.items():
    _make_engine_route(_path, _engine)
