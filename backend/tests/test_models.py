"""Payload validation — the edge where untrusted input is rejected."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from restaurant.models import (
    MAX_FOOD_NAME_LENGTH,
    OrderPlaced,
    OrderStatus,
    TableState,
    utcnow,
)

NOW = utcnow()


def valid(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "clientOrderId": "coid",
        "clientId": "cid",
        "tableId": 1,
        "foodName": "Ramen",
        "sentAt": "2026-01-01T00:00:00Z",
    }
    body.update(overrides)
    return body


class TestOrderPlaced:
    def test_accepts_a_valid_order(self) -> None:
        order = OrderPlaced.model_validate(valid())
        assert order.food_name == "Ramen"
        assert order.table_id == 1

    def test_trims_whitespace_from_the_food_name(self) -> None:
        assert OrderPlaced.model_validate(valid(foodName="  Pad Thai  ")).food_name == (
            "Pad Thai"
        )

    def test_keeps_internal_spaces(self) -> None:
        name = "Green curry with rice"
        assert OrderPlaced.model_validate(valid(foodName=name)).food_name == name

    def test_rejects_a_blank_food_name(self) -> None:
        with pytest.raises(ValidationError):
            OrderPlaced.model_validate(valid(foodName="   "))

    def test_rejects_an_overlong_food_name(self) -> None:
        with pytest.raises(ValidationError):
            OrderPlaced.model_validate(valid(foodName="x" * (MAX_FOOD_NAME_LENGTH + 1)))

    def test_accepts_a_food_name_at_the_limit(self) -> None:
        name = "x" * MAX_FOOD_NAME_LENGTH
        assert OrderPlaced.model_validate(valid(foodName=name)).food_name == name

    @pytest.mark.parametrize("code", [0, 10, 13, 27, 31, 127])
    def test_rejects_interior_control_characters(self, code: int) -> None:
        # Interior, because that is the dangerous position: an embedded newline is
        # what could forge a whole extra line in the structured logs.
        with pytest.raises(ValidationError):
            OrderPlaced.model_validate(valid(foodName=f"Ra{chr(code)}men"))

    @pytest.mark.parametrize("code", [10, 13, 31])
    def test_strips_trailing_whitespace_control_characters(self, code: int) -> None:
        # Documents real behaviour: `strip()` runs before the control-character
        # check, and Python treats \n, \r and \x1f as whitespace. So a trailing
        # newline is removed rather than rejected — harmless, and friendlier than
        # refusing an order because of a stray character the customer cannot see.
        assert (
            OrderPlaced.model_validate(valid(foodName=f"Ramen{chr(code)}")).food_name
            == "Ramen"
        )

    def test_rejects_unknown_fields(self) -> None:
        # extra="forbid" makes the parser fail closed rather than half-apply a
        # payload carrying fields we do not recognise.
        with pytest.raises(ValidationError):
            OrderPlaced.model_validate(valid(surprise="value"))

    def test_rejects_a_negative_table_id(self) -> None:
        with pytest.raises(ValidationError):
            OrderPlaced.model_validate(valid(tableId=-1))

    def test_rejects_a_missing_field(self) -> None:
        body = valid()
        del body["clientId"]
        with pytest.raises(ValidationError):
            OrderPlaced.model_validate(body)

    def test_rejects_an_overlong_identifier(self) -> None:
        with pytest.raises(ValidationError):
            OrderPlaced.model_validate(valid(clientOrderId="x" * 65))


class TestSerialisation:
    def test_publishes_camel_case_on_the_wire(self) -> None:
        # Python stays snake_case, JSON stays camelCase; neither side compromises.
        state = TableState(table_id=2, epoch="e1", version=3, updated_at=NOW, orders=[])
        payload = state.to_json().decode()
        assert '"tableId":2' in payload
        assert '"updatedAt"' in payload
        assert "table_id" not in payload

    def test_status_serialises_as_its_value(self) -> None:
        assert OrderStatus.COOKING.value == "COOKING"
