from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

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


def test_factor_version_resubmit_identical_is_idempotent(client):
    payload = {
        "version": "v2025-01",
        "factors": [
            {"ref": "ef.a", "name": "甲", "unit": "kg",
             "kg_co2e_per_unit": "1.20"},
            {"ref": "ef.b", "name": "乙", "unit": "MJ",
             "kg_co2e_per_unit": "0.30"},
        ]}
    r1 = client.post("/api/factor-versions", json=payload)
    assert r1.status_code == 201, r1.text
    assert r1.json() == {"version": "v2025-01", "created": True,
                         "factor_count": 2}

    # Identical resubmission (reordered, numerically equivalent decimals,
    # null vs absent name) -> 200, no rewrite.
    again = {
        "version": "v2025-01",
        "factors": [
            {"ref": "ef.b", "name": "乙", "unit": "MJ",
             "kg_co2e_per_unit": "0.3"},
            {"ref": "ef.a", "name": "甲", "unit": "kg",
             "kg_co2e_per_unit": "1.200"},
        ]}
    r2 = client.post("/api/factor-versions", json=again)
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"version": "v2025-01", "created": False,
                         "factor_count": 2}

    # Stored rows keep the original values.
    versions = {v["version"]: v
                for v in client.get("/api/factor-versions").json()[
                    "factor_versions"]}
    stored = {f["ref"]: f for f in versions["v2025-01"]["factors"]}
    assert stored["ef.a"]["kg_co2e_per_unit"] == "1.20"
    assert stored["ef.b"]["kg_co2e_per_unit"] == "0.30"


def test_existing_factor_version_conflict_never_overwrites(client):
    seed_payload = {
        "version": "v2025-01",
        "factors": [
            {"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
             "kg_co2e_per_unit": "1.20"},
            {"ref": "ef.ammonia", "name": "氨", "unit": "kg",
             "kg_co2e_per_unit": "2.40"},
        ]}
    assert client.post("/api/factor-versions",
                       json=seed_payload).status_code == 201

    def assert_conflicted(tampered):
        r = client.post("/api/factor-versions", json=tampered)
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["code"] == "FACTOR_VERSION_CONFLICT"
        assert err["details"]["version"] == "v2025-01"
        return err["details"]["differences"]

    # Different Decimal value -> 409 with the exact mismatch.
    diffs = assert_conflicted({
        "version": "v2025-01",
        "factors": [
            {"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
             "kg_co2e_per_unit": "9.999"},
            {"ref": "ef.ammonia", "name": "氨", "unit": "kg",
             "kg_co2e_per_unit": "2.40"},
        ]})
    assert diffs == [{
        "factor_ref": "ef.hydrogen", "change": "value_changed",
        "old_kg_co2e_per_unit": "1.200000000000",
        "new_kg_co2e_per_unit": "9.999000000000",
        "delta_kg_co2e_per_unit": "8.799000000000"}]

    # Different unit -> 409.
    diffs = assert_conflicted({
        "version": "v2025-01",
        "factors": [
            {"ref": "ef.hydrogen", "name": "绿氢", "unit": "g",
             "kg_co2e_per_unit": "1.20"},
            {"ref": "ef.ammonia", "name": "氨", "unit": "kg",
             "kg_co2e_per_unit": "2.40"},
        ]})
    assert diffs == [{"factor_ref": "ef.hydrogen", "change": "unit_changed",
                      "old_unit": "kg", "new_unit": "g"}]

    # Different name -> 409.
    diffs = assert_conflicted({
        "version": "v2025-01",
        "factors": [
            {"ref": "ef.hydrogen", "name": "灰氢", "unit": "kg",
             "kg_co2e_per_unit": "1.20"},
            {"ref": "ef.ammonia", "name": "氨", "unit": "kg",
             "kg_co2e_per_unit": "2.40"},
        ]})
    assert diffs == [{"factor_ref": "ef.hydrogen", "change": "name_changed",
                      "old_name": "绿氢", "new_name": "灰氢"}]

    # Dropping a factor -> 409, the old row must survive.
    diffs = assert_conflicted({
        "version": "v2025-01",
        "factors": [{"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
                     "kg_co2e_per_unit": "1.20"}]})
    assert diffs == [{
        "factor_ref": "ef.ammonia", "change": "missing_in_request",
        "name": "氨", "unit": "kg",
        "kg_co2e_per_unit": "2.400000000000"}]

    # Adding a factor -> 409.
    diffs = assert_conflicted({
        "version": "v2025-01",
        "factors": [
            {"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
             "kg_co2e_per_unit": "1.20"},
            {"ref": "ef.ammonia", "name": "氨", "unit": "kg",
             "kg_co2e_per_unit": "2.40"},
            {"ref": "ef.methane", "name": "甲烷", "unit": "kg",
             "kg_co2e_per_unit": "3.10"},
        ]})
    assert diffs == [{
        "factor_ref": "ef.methane", "change": "added_in_request",
        "name": "甲烷", "unit": "kg",
        "kg_co2e_per_unit": "3.100000000000"}]

    # Nothing was overwritten and nothing was partially inserted: the stored
    # version still has exactly the original two factors with original data.
    versions = {v["version"]: v
                for v in client.get("/api/factor-versions").json()[
                    "factor_versions"]}
    stored = {f["ref"]: f for f in versions["v2025-01"]["factors"]}
    assert set(stored) == {"ef.hydrogen", "ef.ammonia"}
    assert stored["ef.hydrogen"] == {
        "ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
        "kg_co2e_per_unit": "1.20"}
    assert stored["ef.ammonia"] == {
        "ref": "ef.ammonia", "name": "氨", "unit": "kg",
        "kg_co2e_per_unit": "2.40"}

    # Changed content is accepted under a NEW version name.
    assert client.post("/api/factor-versions", json={
        "version": "v2025-02",
        "factors": [
            {"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
             "kg_co2e_per_unit": "9.999"}]}).status_code == 201


def test_duplicate_factor_ref_in_registration_rejected(client):
    r = client.post("/api/factor-versions", json={
        "version": "v2025-09",
        "factors": [
            {"ref": "ef.dup", "unit": "kg", "kg_co2e_per_unit": "1"},
            {"ref": "ef.dup", "unit": "kg", "kg_co2e_per_unit": "2"}]})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "FACTOR_VERSION_VALIDATION_FAILED"
    assert err["issues"][0]["code"] == "DUPLICATE_FACTOR"
    assert client.get("/api/factor-versions").json()["factor_versions"] == [] \
        or "v2025-09" not in {
            v["version"]
            for v in client.get("/api/factor-versions").json()[
                "factor_versions"]}


def test_concurrent_same_named_factor_versions_serialise(client):
    # N different contents racing for one new version name: exactly one
    # winner (201); every loser gets 409, and the stored version equals one
    # winning payload wholesale — never a partial mix.
    def payload(tag):
        return {"version": "vrace-01",
                "factors": [
                    {"ref": f"ef.f{i}", "name": f"因子{i}-{tag}",
                     "unit": "kg", "kg_co2e_per_unit": str(i + 1)}
                    for i in range(5)]}

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda tag: client.post("/api/factor-versions",
                                    json=payload(tag)),
            "abcdefgh"))

    statuses = [r.status_code for r in responses]
    assert 500 not in statuses
    assert statuses.count(201) == 1
    assert set(statuses) <= {201, 409}
    winner = next(r for r in responses if r.status_code == 201).json()
    assert winner["created"] is True

    stored = {v["version"]: v
              for v in client.get("/api/factor-versions").json()[
                  "factor_versions"]}
    factors = stored["vrace-01"]["factors"]
    assert len(factors) == 5
    names = {f["name"] for f in factors}
    tags = {n.split("-")[1] for n in names}
    assert len(tags) == 1  # one payload won entirely, no interleaved rows

    # The winner's identical resubmission now idempotently returns 200.
    winning_tag = next(iter(tags))
    r = client.post("/api/factor-versions", json=payload(winning_tag))
    assert r.status_code == 200
    assert r.json()["created"] is False


def test_factor_version_conflict_preserves_snapshot_history(client):
    created = client.post("/api/assessments",
                          json=REFINERY_REQUEST).json()
    sid = created["snapshot_id"]
    assert Decimal(_products(created)["diesel"]["unit_kg_co2e"]) == Decimal(
        "0.217315789474")

    # Re-registering v2023-01 with different numbers is rejected with 409...
    tampered = {
        "version": "v2023-01",
        "factors": [
            {"ref": "ef.crude_oil", "unit": "kg",
             "kg_co2e_per_unit": "9.999"}
        ]}
    r = client.post("/api/factor-versions", json=tampered)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "FACTOR_VERSION_CONFLICT"

    # ...and the catalogue itself is untouched: all original factors remain.
    versions = {v["version"]: v
                for v in client.get("/api/factor-versions").json()[
                    "factor_versions"]}
    stored = {f["ref"]: f for f in versions["v2023-01"]["factors"]}
    assert len(stored) == 7
    assert stored["ef.crude_oil"]["kg_co2e_per_unit"] == "0.0800"

    # The stored snapshot stays byte-identical, evidence included.
    again = client.get(f"/api/snapshots/{sid}").json()
    assert Decimal(_products(again)["diesel"]["unit_kg_co2e"]) == Decimal(
        "0.217315789474")
    used = {f["ref"]: f for f in again["result"]["factors_used"]}
    assert used["ef.crude_oil"]["kg_co2e_per_unit"] == "0.080000000000"


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


def test_recalculate_factor_changes_use_sealed_factors(client):
    """Even if the old version's catalogue was overwritten (as pre-fix
    deployments allowed), factor_changes must compare the factor values
    sealed in the two snapshots — matching the numbers product_diffs uses.
    """
    sid = client.post("/api/assessments",
                      json=REFINERY_REQUEST).json()["snapshot_id"]

    # Simulate the legacy corruption: raw overwrite of v2023-01 rows, which
    # the HTTP API now refuses to do.
    conn = client.app.state.db
    with conn:
        conn.execute(
            "UPDATE factors SET kg_co2e_per_unit = '9.999' "
            "WHERE version = 'v2023-01' AND ref = 'ef.crude_oil'")
        conn.execute(
            "UPDATE factors SET kg_co2e_per_unit = '8.888' "
            "WHERE version = 'v2023-01' AND ref = 'ef.natural_gas'")

    body = client.post(f"/api/snapshots/{sid}/recalculate",
                       json={"factor_version": "v2024-01"}).json()

    # Old snapshot evidence is still the sealed original value.
    old = client.get(f"/api/snapshots/{sid}").json()
    old_used = {f["ref"]: f for f in old["result"]["factors_used"]}
    assert old_used["ef.crude_oil"]["kg_co2e_per_unit"] == "0.080000000000"

    changes = {c["factor_ref"]: c for c in body["factor_changes"]}
    # crude: sealed 0.08 (not the overwritten 9.999) -> v2024 0.075
    crude = changes["ef.crude_oil"]
    assert crude["old_kg_co2e_per_unit"] == "0.080000000000"
    assert crude["new_kg_co2e_per_unit"] == "0.075000000000"
    assert crude["delta_kg_co2e_per_unit"] == "-0.005000000000"
    # natural_gas is unchanged in the sealed evidence, even though the live
    # catalogue row now reads 8.888: it must not appear as a change.
    assert "ef.natural_gas" not in changes

    # Consistency: product diffs are computed from the same sealed results.
    diffs = {d["product_id"]: d for d in body["product_diffs"]}
    d = diffs["diesel_delivered"]
    assert d["old_unit_kg_co2e"] == "0.235315789474"
    assert d["new_unit_kg_co2e"] == "0.214431578947"

    # Recalculation stays idempotent after the corruption as well.
    again = client.post(f"/api/snapshots/{sid}/recalculate",
                        json={"factor_version": "v2024-01"})
    assert again.status_code == 200
    assert again.json()["new_snapshot"]["snapshot_id"] == \
        body["new_snapshot"]["snapshot_id"]
    # Old snapshot remains readable and unchanged.
    assert client.get(f"/api/snapshots/{sid}").status_code == 200


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
