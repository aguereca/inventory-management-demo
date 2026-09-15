"""
Tests for the restocking order submission endpoint.
"""
from datetime import datetime

import pytest


def post_restock(client, **payload):
    """Submit a restock order, defaulting to a single valid line item."""
    payload.setdefault("items", [{"sku": "TMP-201", "quantity": 10}])
    return client.post("/api/orders/restock", json=payload)


class TestRestockEndpoint:
    """Test suite for POST /api/orders/restock."""

    def test_create_restock_order(self, client):
        """Test submitting a restock order returns the created order."""
        response = post_restock(client)
        assert response.status_code == 201

        order = response.json()
        assert isinstance(order, dict)

        required_fields = [
            "id", "order_number", "customer", "items", "status",
            "order_date", "expected_delivery", "total_value"
        ]
        for field in required_fields:
            assert field in order, f"Missing field: {field}"

    def test_restock_order_status_is_submitted(self, client):
        """Test that a restock order carries the Submitted status."""
        order = post_restock(client).json()
        assert order["status"] == "Submitted"

    def test_restock_order_customer(self, client):
        """Test that a restock order is attributed to an internal customer."""
        order = post_restock(client).json()
        assert order["customer"] == "Internal Restock"

    def test_restock_order_number_format(self, client):
        """Test that the order number follows the existing ORD-YYYY-NNNN shape."""
        order = post_restock(client).json()
        parts = order["order_number"].split("-")
        assert parts[0] == "ORD"
        assert len(parts) == 3
        assert len(parts[1]) == 4 and parts[1].isdigit()
        assert len(parts[2]) == 4 and parts[2].isdigit()

    def test_restock_order_line_items_structure(self, client):
        """Test that line items carry the keys the Orders view reads."""
        order = post_restock(
            client, items=[{"sku": "TMP-201", "quantity": 10}]
        ).json()

        assert isinstance(order["items"], list)
        assert len(order["items"]) == 1

        for item in order["items"]:
            for field in ["sku", "name", "quantity", "unit_price"]:
                assert field in item, f"Missing item field: {field}"
            assert isinstance(item["quantity"], int)
            assert isinstance(item["unit_price"], (int, float))
            assert item["quantity"] > 0
            assert item["unit_price"] >= 0

    def test_restock_order_resolves_name_and_price_from_inventory(self, client):
        """Test that name and unit price come from inventory, not the request."""
        inventory = client.get("/api/inventory").json()
        source = next(i for i in inventory if i["sku"] == "TMP-201")

        order = post_restock(client, items=[{"sku": "TMP-201", "quantity": 4}]).json()
        line = order["items"][0]

        assert line["name"] == source["name"]
        assert abs(line["unit_price"] - source["unit_cost"]) < 0.01

    def test_restock_order_total_value_calculation(self, client):
        """Test that total value equals the summed line items."""
        order = post_restock(client, items=[
            {"sku": "TMP-201", "quantity": 10},
            {"sku": "SRV-301", "quantity": 3},
        ]).json()

        calculated = sum(i["quantity"] * i["unit_price"] for i in order["items"])
        assert abs(order["total_value"] - calculated) < 0.01
        assert order["total_value"] > 0

    def test_restock_order_multiple_items(self, client):
        """Test submitting several SKUs in one combined order."""
        order = post_restock(client, items=[
            {"sku": "TMP-201", "quantity": 5},
            {"sku": "SRV-302", "quantity": 2},
            {"sku": "PSU-508", "quantity": 7},
        ]).json()

        assert len(order["items"]) == 3
        assert {i["sku"] for i in order["items"]} == {"TMP-201", "SRV-302", "PSU-508"}

    def test_restock_order_lead_time_within_policy(self, client):
        """Test that expected delivery is 7-14 days out, matching existing orders."""
        order = post_restock(client).json()

        ordered = datetime.fromisoformat(order["order_date"])
        expected = datetime.fromisoformat(order["expected_delivery"])
        lead_days = (expected - ordered).days

        assert 7 <= lead_days <= 14, f"Lead time {lead_days} outside the 7-14 day window"

    def test_restock_order_dates_format(self, client):
        """Test that both dates are ISO strings with a time component."""
        order = post_restock(client).json()

        for field in ["order_date", "expected_delivery"]:
            assert "T" in order[field]
            assert "-" in order[field]
            datetime.fromisoformat(order[field])  # raises if malformed

    def test_restock_order_has_no_actual_delivery(self, client):
        """Test that a newly submitted order has not been delivered."""
        order = post_restock(client).json()
        assert order["actual_delivery"] is None

    def test_restock_order_warehouse_defaults_to_largest_line(self, client):
        """Test the warehouse/category fallback when the request omits them.

        SRV-302 (Tokyo, Actuators, 725.0) outweighs TMP-201 (London, Sensors, 89.5)
        at these quantities, so the order should be attributed to Tokyo.
        """
        order = post_restock(client, items=[
            {"sku": "TMP-201", "quantity": 1},
            {"sku": "SRV-302", "quantity": 5},
        ]).json()

        assert order["warehouse"] == "Tokyo"
        assert order["category"] == "Actuators"

    def test_restock_order_respects_explicit_warehouse_category(self, client):
        """Test that request-supplied warehouse and category win over the fallback."""
        order = post_restock(
            client,
            items=[{"sku": "SRV-302", "quantity": 5}],
            warehouse="London",
            category="Sensors",
        ).json()

        assert order["warehouse"] == "London"
        assert order["category"] == "Sensors"

    def test_restock_order_appears_in_orders_list(self, client):
        """Test that a submitted order is returned by GET /api/orders."""
        created = post_restock(client).json()

        all_orders = client.get("/api/orders").json()
        match = next((o for o in all_orders if o["id"] == created["id"]), None)

        assert match is not None, "Submitted order missing from /api/orders"
        assert match["order_number"] == created["order_number"]
        assert match["status"] == "Submitted"

    def test_restock_order_retrievable_by_id(self, client):
        """Test that a submitted order can be fetched by its id."""
        created = post_restock(client).json()

        response = client.get(f"/api/orders/{created['id']}")
        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    def test_restock_order_id_does_not_collide(self, client):
        """Test that two submitted orders get distinct ids and numbers."""
        first = post_restock(client).json()
        second = post_restock(client).json()

        assert first["id"] != second["id"]
        assert first["order_number"] != second["order_number"]
        assert int(second["id"]) == int(first["id"]) + 1

    def test_restock_order_survives_warehouse_filter(self, client):
        """Test that a submitted order is still found when filtering by its warehouse.

        Guards the warehouse fallback: a None would make apply_filters drop the
        order from the Orders view whenever a location filter is active.
        """
        created = post_restock(
            client, items=[{"sku": "TMP-201", "quantity": 3}]
        ).json()

        filtered = client.get(f"/api/orders?warehouse={created['warehouse']}").json()
        assert any(o["id"] == created["id"] for o in filtered)

    def test_restock_order_counted_in_dashboard_orders_value(self, client):
        """Test that a submitted order's value flows into the dashboard total."""
        before = client.get("/api/dashboard/summary").json()["total_orders_value"]
        created = post_restock(client).json()
        after = client.get("/api/dashboard/summary").json()["total_orders_value"]

        assert abs((after - before) - created["total_value"]) < 0.01


class TestRestockValidation:
    """Test suite for restock request validation."""

    def test_empty_items_rejected(self, client):
        """Test that an order with no items is rejected."""
        response = post_restock(client, items=[])
        assert response.status_code == 400

        data = response.json()
        assert "detail" in data
        assert "at least one item" in data["detail"].lower()

    def test_unknown_sku_rejected(self, client):
        """Test that an unrecognised SKU is rejected."""
        response = post_restock(client, items=[{"sku": "NOPE-999", "quantity": 5}])
        assert response.status_code == 400

        data = response.json()
        assert "detail" in data
        assert "NOPE-999" in data["detail"]

    def test_zero_quantity_rejected(self, client):
        """Test that a zero quantity is rejected."""
        response = post_restock(client, items=[{"sku": "TMP-201", "quantity": 0}])
        assert response.status_code == 400
        assert "detail" in response.json()

    def test_negative_quantity_rejected(self, client):
        """Test that a negative quantity is rejected."""
        response = post_restock(client, items=[{"sku": "TMP-201", "quantity": -5}])
        assert response.status_code == 400
        assert "detail" in response.json()

    def test_missing_items_field_rejected(self, client):
        """Test that omitting the items field fails Pydantic validation."""
        response = client.post("/api/orders/restock", json={})
        assert response.status_code == 422

    def test_rejected_order_is_not_persisted(self, client):
        """Test that a failed submission does not add an order."""
        before = len(client.get("/api/orders").json())

        assert post_restock(client, items=[]).status_code == 400
        assert post_restock(
            client, items=[{"sku": "NOPE-999", "quantity": 1}]
        ).status_code == 400

        after = len(client.get("/api/orders").json())
        assert after == before
