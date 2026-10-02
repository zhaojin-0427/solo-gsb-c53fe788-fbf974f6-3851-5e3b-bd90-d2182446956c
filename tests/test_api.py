from decimal import Decimal

import pytest

from tests.conftest import REFINERY_REQUEST


def _products(body):
    return {p["product_id"]: p for p in body["result"]["products"]}


def test_health_and_factor_versions(client):
    assert client.get("/health").json() == {"status": "ok"}
    versions = client.get("/api/factor-versions").json()["factor_versions"]
    ids = {v["version"] for v in versions}
    assert ids == {"v2023-01", "v2024-01"}
    refs = {f["ref"] for f in versions[0]["factors"]}
    assert "ef.natural_gas" in refs


def test_full_assessment_mass_allocation(client):
    r = client.post("/api/assessments", json=REFINERY_REQUEST)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["factor_version"] == "v2023-01"
    assert body["result"]["topological_order"] == ["refining", "distribution"]
    assert body["idempotent_replay"] is False

    products = _products(body)
    # refining total: 1000*0.08 (crude) + 1000*0.0561 + 100*0.7035
    #               = 80 + 56.1 + 70.35 = 206.45
    refining = body["result"]["processes"][0]
    assert Decimal(refining["total_kg_co2e"]) == Decimal("206.45")

    # mass allocation: per-kg footprint is identical for every co-product
    unit = Decimal(products["gasoline"]["unit_kg_co2e"])
    assert unit == Decimal(products["diesel"]["unit_kg_co2e"])
    assert unit == Decimal(products["residue"]["unit_kg_co2e"])
    assert unit == Decimal("0.217315789474")  # 206.45/950

    # downstream diesel: upstream 108.6579 + freight 100*0.09 = 9 -> 117.6579
    delivered = products["diesel_delivered"]
    assert Decimal(delivered["unit_kg_co2e"]) == Decimal("0.235315789474")
    # contribution chains reach every primitive source
    chains = {tuple(c["chain"]) for c in delivered["contributions"]}
    assert any("external_material" in c and "crude" in c for c in chains)
    assert any("natural_gas" in c for c in chains)
    assert any("electricity" in c for c in chains)
    assert any("freight" in c for c in chains)
    # shares sum to 1 (quantised to 12 places)
    shares = sum(Decimal(c["share_of_product"])
                 for c in delivered["contributions"])
    assert abs(shares - Decimal("1")) <= Decimal("0.000000000001")


def test_economic_allocation_differs_from_mass(client):
    payload = {**REFINERY_REQUEST, "client_request_id": "req-econ",
               "allocation_method": "economic"}
    r = client.post("/api/assessments", json=payload)
    assert r.status_code == 201, r.text
    products = _products(r.json())
    g = Decimal(products["gasoline"]["unit_kg_co2e"])
    d = Decimal(products["diesel"]["unit_kg_co2e"])
    assert g != d
    # gasoline carries 600/910 of 206.45
    assert g == (Decimal("206.45") * Decimal("600")
                 / Decimal("910") / Decimal("400")).quantize(Decimal("1e-12"))


def test_idempotent_replay_and_reordering(client):
    r1 = client.post("/api/assessments", json=REFINERY_REQUEST)
    assert r1.status_code == 201
    id1 = r1.json()["snapshot_id"]

    r2 = client.post("/api/assessments", json=REFINERY_REQUEST)
    assert r2.status_code == 200
    assert r2.json()["snapshot_id"] == id1
    assert r2.json()["idempotent_replay"] is True

    # Same content, processes given in reverse order -> same hash.
    reordered = {**REFINERY_REQUEST,
                 "processes": list(reversed(REFINERY_REQUEST["processes"]))}
    r3 = client.post("/api/assessments", json=reordered)
    assert r3.status_code == 200
    assert r3.json()["snapshot_id"] == id1
    # Topological order is still computed, not input order
    assert r3.json()["result"]["topological_order"] == [
        "refining", "distribution"]


def test_client_request_id_conflict(client):
    r1 = client.post("/api/assessments", json=REFINERY_REQUEST)
    assert r1.status_code == 201

    other = {**REFINERY_REQUEST,
             "external_materials": [
                 {"ref": "crude", "quantity": "999", "unit": "kg",
                  "factor_ref": "ef.crude_oil"}]}
    r2 = client.post("/api/assessments", json=other)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "CLIENT_REQUEST_ID_CONFLICT"


def test_factor_version_is_immutable_conflict_keeps_history(client):
    created = client.post("/api/assessments",
                          json=REFINERY_REQUEST).json()
    sid = created["snapshot_id"]
    assert Decimal(_products(created)["diesel"]["unit_kg_co2e"]) == Decimal(
        "0.217315789474")

    # Re-registering v2023-01 with different factors is rejected: 409,
    # nothing is overwritten.
    tampered = {
        "version": "v2023-01",
        "factors": [
            {"ref": "ef.crude_oil", "name": "原油开采与运输", "unit": "kg",
             "kg_co2e_per_unit": "9.999"}
        ]}
    r = client.post("/api/factor-versions", json=tampered)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "FACTOR_VERSION_CONFLICT"
    codes = {i["code"] for i in err["details"]["issues"]}
    assert "FACTOR_VALUE_CONFLICT" in codes
    # The whole sealed version is still there: missing refs are reported too.
    assert "FACTOR_REMOVED" in codes

    # Live factors table untouched.
    versions = {v["version"]: v
                for v in client.get("/api/factor-versions").json()
                ["factor_versions"]}
    sealed = {f["ref"]: f for f in versions["v2023-01"]["factors"]}
    assert sealed["ef.crude_oil"]["kg_co2e_per_unit"] == "0.0800"
    assert versions["v2023-01"]["factor_count"] == 7

    # Snapshot evidence stays byte-identical.
    again = client.get(f"/api/snapshots/{sid}").json()
    assert Decimal(_products(again)["diesel"]["unit_kg_co2e"]) == Decimal(
        "0.217315789474")
    used = {f["ref"]: f for f in again["result"]["factors_used"]}
    assert used["ef.crude_oil"]["kg_co2e_per_unit"] == "0.080000000000"


def test_factor_version_identical_resubmit_is_idempotent(client):
    payload = {
        "version": "v2025-01",
        "factors": [{"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
                     "kg_co2e_per_unit": "1.200"}]}
    r1 = client.post("/api/factor-versions", json=payload)
    assert r1.status_code == 201, r1.text
    assert r1.json() == {"version": "v2025-01", "created": True,
                         "idempotent_replay": False, "factor_count": 1}

    # Same ref set / name / unit, numerically equal Decimal, even when the
    # text differs (1.200 == 1.2) and list order is unchanged here.
    again = {
        "version": "v2025-01",
        "factors": [{"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
                     "kg_co2e_per_unit": "1.2"}]}
    r2 = client.post("/api/factor-versions", json=again)
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"version": "v2025-01", "created": False,
                         "idempotent_replay": True, "factor_count": 1}


def test_factor_version_any_change_conflicts(client):
    base_factors = [{"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
                     "kg_co2e_per_unit": "1.2"}]
    assert client.post("/api/factor-versions",
                       json={"version": "v2025-01",
                             "factors": base_factors}).status_code == 201

    def conflict_codes(factors):
        r = client.post("/api/factor-versions",
                        json={"version": "v2025-01", "factors": factors})
        assert r.status_code == 409, r.text
        return {i["code"] for i in r.json()["error"]["details"]["issues"]}

    # changed Decimal value
    assert "FACTOR_VALUE_CONFLICT" in conflict_codes(
        [{"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
          "kg_co2e_per_unit": "1.3"}])
    # changed unit
    assert "FACTOR_UNIT_CONFLICT" in conflict_codes(
        [{"ref": "ef.hydrogen", "name": "绿氢", "unit": "g",
          "kg_co2e_per_unit": "1.2"}])
    # changed name
    assert "FACTOR_NAME_CONFLICT" in conflict_codes(
        [{"ref": "ef.hydrogen", "name": "蓝氢", "unit": "kg",
          "kg_co2e_per_unit": "1.2"}])
    # added factor
    assert "FACTOR_ADDED" in conflict_codes(
        [{"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
          "kg_co2e_per_unit": "1.2"},
         {"ref": "ef.ammonia", "name": "氨", "unit": "kg",
          "kg_co2e_per_unit": "2"}])

    # The changed content must be accepted under a new version name.
    r = client.post("/api/factor-versions", json={
        "version": "v2025-02",
        "factors": [{"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
                     "kg_co2e_per_unit": "1.3"}]})
    assert r.status_code == 201, r.text


def test_duplicate_factor_ref_in_one_request_rejected(client):
    r = client.post("/api/factor-versions", json={
        "version": "v2025-03",
        "factors": [
            {"ref": "ef.x", "unit": "kg", "kg_co2e_per_unit": "1"},
            {"ref": "ef.x", "unit": "kg", "kg_co2e_per_unit": "2"}]})
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["error"]["issues"]}
    assert "DUPLICATE_FACTOR" in codes
    # nothing was written
    versions = {v["version"]
                for v in client.get("/api/factor-versions").json()
                ["factor_versions"]}
    assert "v2025-03" not in versions


def test_concurrent_same_name_submissions(client):
    import threading

    barrier = threading.Barrier(6)
    outcomes = []

    def post(value):
        barrier.wait()
        status = client.post("/api/factor-versions", json={
            "version": "v2026-01",
            "factors": [{"ref": "ef.a", "name": "A", "unit": "kg",
                         "kg_co2e_per_unit": value}]}).status_code
        outcomes.append((value, status))

    threads = []
    for _ in range(3):
        threads.append(threading.Thread(target=post, args=("1",)))
    for _ in range(3):
        threads.append(threading.Thread(target=post, args=("2",)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one writer wins (201); same-content followers replay (200);
    # the other content's writers all conflict (409).  Nothing is partially
    # written and the loser never overwrites the winner.
    winners = [v for v, s in outcomes if s == 201]
    assert len(winners) == 1
    winner = winners[0]
    for value, status in outcomes:
        if value == winner:
            assert status in (200, 201)
        else:
            assert status == 409

    versions = {v["version"]: v
                for v in client.get("/api/factor-versions").json()
                ["factor_versions"]}
    v = versions["v2026-01"]
    assert v["factor_count"] == 1
    assert v["factors"][0]["kg_co2e_per_unit"] == winner


def test_cross_connection_race_is_replay_or_conflict(tmp_path):
    from app import db, seed
    from app.errors import ConflictError
    from app.models import FactorVersionRequest
    from app.service import register_factor_version

    path = str(tmp_path / "race.db")
    conn1 = db.connect(path)
    db.init_db(conn1)
    seed.seed(conn1)
    conn2 = db.connect(path)  # separate connection, no shared process lock

    req = FactorVersionRequest.model_validate({
        "version": "v2030-01",
        "factors": [{"ref": "ef.a", "name": "A", "unit": "kg",
                     "kg_co2e_per_unit": "1"}]})
    assert register_factor_version(conn1, req)["created"] is True

    # Identical content arriving on a second connection: the INSERT loses with
    # IntegrityError but is recognised as an idempotent replay.
    assert register_factor_version(conn2, req)["idempotent_replay"] is True

    # Different content: conflict, and conn2's failed insert rolled back.
    bad = FactorVersionRequest.model_validate({
        "version": "v2030-01",
        "factors": [{"ref": "ef.a", "name": "A", "unit": "kg",
                     "kg_co2e_per_unit": "9"}]})
    with pytest.raises(ConflictError) as excinfo:
        register_factor_version(conn2, bad)
    assert excinfo.value.code == "FACTOR_VERSION_CONFLICT"
    assert db.get_factors(conn1, "v2030-01")["ef.a"][
        "kg_co2e_per_unit"] == "1"
    conn1.close()
    conn2.close()


def test_recalculate_uses_sealed_factors_even_if_version_overwritten(client):
    sid = client.post("/api/assessments",
                      json=REFINERY_REQUEST).json()["snapshot_id"]

    # Simulate historical corruption (e.g. a pre-fix deployment that allowed
    # the overwrite): tamper with the live factors table directly.
    conn = client.app.state.db
    with conn:
        conn.execute(
            "UPDATE factors SET kg_co2e_per_unit = '0.0500' "
            "WHERE version = 'v2023-01' AND ref = 'ef.crude_oil'")

    r = client.post(f"/api/snapshots/{sid}/recalculate",
                    json={"factor_version": "v2024-01"})
    assert r.status_code == 201, r.text
    body = r.json()

    # product_diffs are computed from the two sealed snapshots; old value is
    # still the original 0.235315..., not anything derived from 0.05.
    diffs = {d["product_id"]: d for d in body["product_diffs"]}
    d = diffs["diesel_delivered"]
    assert d["old_unit_kg_co2e"] == "0.235315789474"
    assert d["new_unit_kg_co2e"] == "0.214431578947"

    # factor_changes old side must come from the old snapshot's sealed
    # factors_used (0.08), consistent with product_diffs — never 0.05.
    changes = {c["factor_ref"]: c for c in body["factor_changes"]}
    crude = changes["ef.crude_oil"]
    assert crude["old_kg_co2e_per_unit"] == "0.080000000000"
    assert crude["new_kg_co2e_per_unit"] == "0.075000000000"
    assert crude["delta_kg_co2e_per_unit"] == "-0.005000000000"

    # Old snapshot evidence untouched and recalculation stays idempotent.
    old = client.get(f"/api/snapshots/{sid}").json()
    sealed = {f["ref"]: f for f in old["result"]["factors_used"]}
    assert sealed["ef.crude_oil"]["kg_co2e_per_unit"] == "0.080000000000"
    r2 = client.post(f"/api/snapshots/{sid}/recalculate",
                     json={"factor_version": "v2024-01"})
    assert r2.status_code == 200
    assert (r2.json()["new_snapshot"]["snapshot_id"]
            == body["new_snapshot"]["snapshot_id"])
    crude2 = {c["factor_ref"]: c
              for c in r2.json()["factor_changes"]}["ef.crude_oil"]
    assert crude2["old_kg_co2e_per_unit"] == "0.080000000000"


def test_recalculate_with_new_factor_version(client):
    sid = client.post("/api/assessments",
                      json=REFINERY_REQUEST).json()["snapshot_id"]
    r = client.post(f"/api/snapshots/{sid}/recalculate",
                    json={"factor_version": "v2024-01"})
    assert r.status_code == 201, r.text
    body = r.json()
    new = body["new_snapshot"]
    assert new["source_snapshot"] == sid
    assert new["factor_version"] == "v2024-01"
    assert new["snapshot_id"] != sid

    diffs = {d["product_id"]: d for d in body["product_diffs"]}
    d = diffs["diesel_delivered"]
    assert d["old_unit_kg_co2e"] == "0.235315789474"
    # crude 0.075, gas 0.0561, elec 0.5703 -> refining 75+56.1+57.03=188.13
    # diesel 500/950 share = 99.015789..., + freight 8.2 => per kg
    assert d["new_unit_kg_co2e"] == "0.214431578947"
    assert Decimal(d["delta_unit_kg_co2e"]) < 0
    assert Decimal(d["delta_percent"]) == Decimal("-8.874972")
    assert body["changed_product_count"] == 4

    refs = {c["factor_ref"] for c in body["factor_changes"]}
    assert "ef.grid_electricity" in refs
    # unchanged factor (natural_gas) absent
    assert "ef.natural_gas" not in refs

    # recalculation is idempotent too
    r2 = client.post(f"/api/snapshots/{sid}/recalculate",
                     json={"factor_version": "v2024-01"})
    assert r2.status_code == 200
    assert r2.json()["new_snapshot"]["snapshot_id"] == new["snapshot_id"]


def test_recalculate_missing_factor_version(client):
    sid = client.post("/api/assessments",
                      json=REFINERY_REQUEST).json()["snapshot_id"]
    r = client.post(f"/api/snapshots/{sid}/recalculate",
                    json={"factor_version": "v2099-01"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "FACTOR_VERSION_NOT_FOUND"


def test_cycle_rejected(client):
    cyclic = {
        "factor_version": "v2023-01",
        "allocation_method": "mass",
        "processes": [
            {"process_id": "A", "batch_quantity": "10", "unit": "kg",
             "inputs": [{"ref": "b", "quantity": "1", "unit": "kg"}],
             "outputs": [{"product_id": "a", "quantity": "10", "unit": "kg",
                          "mass_weight": "10"}]},
            {"process_id": "B", "batch_quantity": "10", "unit": "kg",
             "inputs": [{"ref": "a", "quantity": "1", "unit": "kg"}],
             "outputs": [{"product_id": "b", "quantity": "10", "unit": "kg",
                          "mass_weight": "10"}]}
        ]}
    r = client.post("/api/assessments", json=cyclic)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["error"]["issues"]}
    assert "CYCLE_DETECTED" in codes
    msg = next(i["message"] for i in r.json()["error"]["issues"]
               if i["code"] == "CYCLE_DETECTED")
    assert "A -> B -> A" in msg or "B -> A -> B" in msg


def test_negative_amount_rejected(client):
    bad = {
        "factor_version": "v2023-01",
        "allocation_method": "mass",
        "processes": [{
            "process_id": "P", "batch_quantity": "-10", "unit": "kg",
            "outputs": [{"product_id": "x", "quantity": "0", "unit": "kg",
                         "mass_weight": "-1"}]}]}
    r = client.post("/api/assessments", json=bad)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["error"]["issues"]}
    assert "NEGATIVE_AMOUNT" in codes
    assert "ZERO_AMOUNT" in codes


def test_missing_factor_and_unit_mismatch(client):
    bad = {
        "factor_version": "v2023-01",
        "allocation_method": "mass",
        "processes": [{
            "process_id": "P", "batch_quantity": "10", "unit": "kg",
            "direct_emissions": [
                {"source": "x", "quantity": "5", "unit": "L",
                 "factor_ref": "ef.water_supply"},
                {"source": "y", "quantity": "5", "unit": "MJ",
                 "factor_ref": "ef.unknown"}],
            "outputs": [{"product_id": "x", "quantity": "10", "unit": "kg",
                         "mass_weight": "10"}]}]}
    r = client.post("/api/assessments", json=bad)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["error"]["issues"]}
    assert "MISSING_FACTOR" in codes
    assert "FACTOR_UNIT_MISMATCH" in codes


def test_allocation_denominator_zero(client):
    bad = {
        "factor_version": "v2023-01",
        "allocation_method": "mass",
        "processes": [{
            "process_id": "P", "batch_quantity": "10", "unit": "kg",
            "outputs": [{"product_id": "x", "quantity": "10", "unit": "kg",
                         "mass_weight": "0"}]}]}
    r = client.post("/api/assessments", json=bad)
    assert r.status_code == 422
    codes = [i["code"] for i in r.json()["error"]["issues"]]
    assert "ALLOCATION_DENOMINATOR_ZERO" in codes


def test_unknown_reference_and_flow_exceed(client):
    bad = {
        "factor_version": "v2023-01",
        "allocation_method": "mass",
        "processes": [
            {"process_id": "A", "batch_quantity": "10", "unit": "kg",
             "outputs": [{"product_id": "a", "quantity": "10", "unit": "kg",
                          "mass_weight": "10"}]},
            {"process_id": "B", "batch_quantity": "5", "unit": "kg",
             "inputs": [
                 {"ref": "a", "quantity": "11", "unit": "kg"},
                 {"ref": "ghost", "quantity": "1", "unit": "kg"}],
             "outputs": [{"product_id": "b", "quantity": "5", "unit": "kg",
                          "mass_weight": "5"}]}]}
    r = client.post("/api/assessments", json=bad)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["error"]["issues"]}
    assert "UNKNOWN_REFERENCE" in codes
    assert "FLOW_EXCEEDS_OUTPUT" in codes


def test_error_details_are_sorted_and_stable(client):
    bad = {"factor_version": "v2023-01", "allocation_method": "mass",
           "processes": [
               {"process_id": "Z", "batch_quantity": "-1", "unit": "kg",
                "outputs": [{"product_id": "z", "quantity": "10",
                             "unit": "kg", "mass_weight": "10"}]},
               {"process_id": "A", "batch_quantity": "-1", "unit": "kg",
                "outputs": [{"product_id": "a", "quantity": "10",
                             "unit": "kg", "mass_weight": "10"}]}]}
    r1 = client.post("/api/assessments", json=bad)
    paths = [i["path"] for i in r1.json()["error"]["issues"]]
    assert paths == sorted(paths)


def test_product_endpoint_and_404(client):
    sid = client.post("/api/assessments",
                      json=REFINERY_REQUEST).json()["snapshot_id"]
    r = client.get(f"/api/snapshots/{sid}/products/diesel")
    assert r.status_code == 200
    assert r.json()["product"]["product_id"] == "diesel"

    assert client.get(f"/api/snapshots/{sid}/products/nope").status_code == 404
    assert client.get("/api/snapshots/snap_deadbeef").status_code == 404


def test_register_new_factor_version_and_use_it(client):
    r = client.post("/api/factor-versions", json={
        "version": "v2025-01",
        "factors": [{"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
                     "kg_co2e_per_unit": "1.20"}]})
    assert r.status_code == 201
    payload = {
        "factor_version": "v2025-01",
        "allocation_method": "mass",
        "processes": [{
            "process_id": "P", "batch_quantity": "10", "unit": "kg",
            "direct_emissions": [
                {"source": "h2", "quantity": "10", "unit": "kg",
                 "factor_ref": "ef.hydrogen"}],
            "outputs": [{"product_id": "x", "quantity": "10", "unit": "kg",
                         "mass_weight": "10"}]}]}
    created = client.post("/api/assessments", json=payload)
    assert created.status_code == 201, created.text
    assert Decimal(_products(created.json())["x"]["unit_kg_co2e"]) == Decimal(
        "1.2")


def test_string_decimal_outputs_are_strings(client):
    body = client.post("/api/assessments", json=REFINERY_REQUEST).json()
    p = _products(body)["gasoline"]
    assert isinstance(p["unit_kg_co2e"], str)
    assert isinstance(p["contributions"][0]["kg_co2e"], str)


def test_product_without_emissions_or_inputs_has_zero_footprint(client):
    payload = {
        "factor_version": "v2023-01",
        "allocation_method": "mass",
        "processes": [{
            "process_id": "P", "batch_quantity": "10", "unit": "kg",
            "outputs": [{"product_id": "x", "quantity": "10", "unit": "kg",
                         "mass_weight": "10"}]}]}
    r = client.post("/api/assessments", json=payload)
    assert r.status_code == 201, r.text
    p = _products(r.json())["x"]
    assert p["total_kg_co2e"] == "0"
    assert p["unit_kg_co2e"] == "0"
    assert p["contributions"] == []


def test_non_finite_number_rejected(client):
    r = client.post("/api/assessments", json={
        "factor_version": "v2023-01",
        "allocation_method": "mass",
        "processes": [{
            "process_id": "P", "batch_quantity": "1e99999", "unit": "kg",
            "outputs": [{"product_id": "x", "quantity": "10", "unit": "kg",
                         "mass_weight": "10"}]}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
