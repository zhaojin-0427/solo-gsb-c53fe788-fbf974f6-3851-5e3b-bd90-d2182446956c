import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    app = create_app(":memory:")
    with TestClient(app) as c:
        yield c


REFINERY_REQUEST = {
    "client_request_id": "req-refinery-001",
    "factor_version": "v2023-01",
    "allocation_method": "mass",
    "external_materials": [
        {"ref": "crude", "name": "原油", "quantity": "1000",
         "unit": "kg", "factor_ref": "ef.crude_oil"}
    ],
    "processes": [
        {
            "process_id": "refining",
            "name": "原油炼制",
            "batch_quantity": "950",
            "unit": "kg",
            "direct_emissions": [
                {"source": "natural_gas", "quantity": "1000",
                 "unit": "MJ", "factor_ref": "ef.natural_gas"},
                {"source": "electricity", "quantity": "100",
                 "unit": "kWh", "factor_ref": "ef.grid_electricity"}
            ],
            "inputs": [
                {"ref": "crude", "quantity": "1000", "unit": "kg"}
            ],
            "outputs": [
                {"product_id": "gasoline", "name": "汽油",
                 "quantity": "400", "unit": "kg",
                 "mass_weight": "400", "energy_weight": "450",
                 "economic_weight": "600"},
                {"product_id": "diesel", "name": "柴油",
                 "quantity": "500", "unit": "kg",
                 "mass_weight": "500", "energy_weight": "400",
                 "economic_weight": "300"},
                {"product_id": "residue", "name": "渣油",
                 "quantity": "50", "unit": "kg",
                 "mass_weight": "50", "energy_weight": "50",
                 "economic_weight": "10"}
            ]
        },
        {
            "process_id": "distribution",
            "name": "配送",
            "batch_quantity": "500",
            "unit": "kg",
            "direct_emissions": [
                {"source": "freight", "quantity": "100",
                 "unit": "tkm", "factor_ref": "ef.truck_freight"}
            ],
            "inputs": [
                {"ref": "diesel", "quantity": "500", "unit": "kg"}
            ],
            "outputs": [
                {"product_id": "diesel_delivered", "name": "交付柴油",
                 "quantity": "500", "unit": "kg",
                 "mass_weight": "500", "energy_weight": "400",
                 "economic_weight": "300"}
            ]
        }
    ]
}
