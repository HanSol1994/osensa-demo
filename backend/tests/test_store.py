"""State store behaviour, including the bounds that make it DoS-resistant."""

from __future__ import annotations

from restaurant.config import Settings
from restaurant.models import Order, OrderStatus, utcnow
from restaurant.store import InMemoryStore, create_store

TABLES = (1, 2, 3, 4)


def cooking_order(order_id: str, food: str = "Ramen") -> Order:
    now = utcnow()
    return Order(
        order_id=order_id,
        food_name=food,
        status=OrderStatus.COOKING,
        placed_at=now,
        cook_seconds=10,
        expected_ready_at=now,
        ready_at=None,
    )


class TestVersioning:
    def test_version_increments_on_every_change(self) -> None:
        store = InMemoryStore(TABLES)
        assert store.snapshot(1).version == 0
        store.add_order(1, cooking_order("a"))
        assert store.snapshot(1).version == 1
        store.finish_order(1, "a", OrderStatus.SERVED, utcnow())
        assert store.snapshot(1).version == 2

    def test_versions_are_per_table(self) -> None:
        store = InMemoryStore(TABLES)
        store.add_order(1, cooking_order("a"))
        assert store.snapshot(1).version == 1
        assert store.snapshot(2).version == 0

    def test_epoch_is_stable_within_an_instance(self) -> None:
        store = InMemoryStore(TABLES)
        assert store.snapshot(1).epoch == store.snapshot(2).epoch

    def test_a_new_instance_gets_a_new_epoch(self) -> None:
        # This is what tells a client that versions restarted rather than went
        # backwards after a kitchen restart.
        assert InMemoryStore(TABLES).snapshot(1).epoch != (
            InMemoryStore(TABLES).snapshot(1).epoch
        )


class TestSnapshotIsolation:
    def test_a_snapshot_is_not_mutated_by_later_changes(self) -> None:
        store = InMemoryStore(TABLES)
        store.add_order(1, cooking_order("a"))
        taken = store.snapshot(1)
        store.finish_order(1, "a", OrderStatus.SERVED, utcnow())
        # The snapshot already handed out for publishing must not change under us.
        assert taken.orders[0].status is OrderStatus.COOKING


class TestFinishOrder:
    def test_marks_served(self) -> None:
        store = InMemoryStore(TABLES)
        store.add_order(1, cooking_order("a"))
        state = store.finish_order(1, "a", OrderStatus.SERVED, utcnow())
        assert state is not None
        assert state.orders[0].status is OrderStatus.SERVED
        assert state.orders[0].ready_at is not None

    def test_returns_none_for_an_unknown_order(self) -> None:
        # A benign race, not an error worth crashing a cooking task over.
        store = InMemoryStore(TABLES)
        assert store.finish_order(1, "missing", OrderStatus.SERVED, utcnow()) is None

    def test_returns_none_for_an_unknown_table(self) -> None:
        store = InMemoryStore(TABLES)
        assert store.finish_order(99, "a", OrderStatus.SERVED, utcnow()) is None

    def test_will_not_finish_an_order_twice(self) -> None:
        store = InMemoryStore(TABLES)
        store.add_order(1, cooking_order("a"))
        assert store.finish_order(1, "a", OrderStatus.SERVED, utcnow()) is not None
        assert store.finish_order(1, "a", OrderStatus.SERVED, utcnow()) is None


class TestCounts:
    def test_counts_only_cooking_orders(self) -> None:
        store = InMemoryStore(TABLES)
        store.add_order(1, cooking_order("a"))
        store.add_order(1, cooking_order("b"))
        assert store.cooking_count(1) == 2
        store.finish_order(1, "a", OrderStatus.SERVED, utcnow())
        assert store.cooking_count(1) == 1

    def test_global_count_spans_tables(self) -> None:
        store = InMemoryStore(TABLES)
        store.add_order(1, cooking_order("a"))
        store.add_order(2, cooking_order("b"))
        assert store.total_cooking_count() == 2

    def test_unknown_table_counts_zero(self) -> None:
        assert InMemoryStore(TABLES).cooking_count(99) == 0


class TestBounds:
    def test_dedupe_cache_is_bounded(self) -> None:
        # Unbounded growth here would be a memory DoS via unique order ids.
        store = InMemoryStore(TABLES, dedupe_cache_size=3)
        for i in range(5):
            store.remember_client_order(f"k{i}")
        assert not store.is_duplicate("k0")
        assert not store.is_duplicate("k1")
        assert store.is_duplicate("k4")

    def test_finished_orders_are_capped_but_cooking_is_never_dropped(self) -> None:
        store = InMemoryStore(TABLES, max_served_history=2)
        for i in range(5):
            store.add_order(1, cooking_order(f"s{i}"))
            store.finish_order(1, f"s{i}", OrderStatus.SERVED, utcnow())
        store.add_order(1, cooking_order("live"))

        orders = store.snapshot(1).orders
        served = [o for o in orders if o.status is OrderStatus.SERVED]
        cooking = [o for o in orders if o.status is OrderStatus.COOKING]
        assert len(served) == 2
        assert [o.order_id for o in cooking] == ["live"]


class TestReset:
    def test_clears_orders_and_issues_a_new_epoch(self) -> None:
        store = InMemoryStore(TABLES)
        store.add_order(1, cooking_order("a"))
        store.remember_client_order("coid")
        before = store.snapshot(1).epoch

        store.reset()

        assert store.snapshot(1).orders == []
        assert store.snapshot(1).version == 0
        assert store.snapshot(1).epoch != before
        # Cleared too, so replaying an old order after a reset is a real order
        # rather than being silently swallowed as a duplicate.
        assert not store.is_duplicate("coid")

    def test_keeps_the_configured_tables(self) -> None:
        store = InMemoryStore(TABLES)
        store.reset()
        assert [s.table_id for s in store.all_snapshots()] == list(TABLES)


class TestSnapshots:
    def test_all_snapshots_is_sorted_by_table(self) -> None:
        store = InMemoryStore((3, 1, 2))
        assert [s.table_id for s in store.all_snapshots()] == [1, 2, 3]


class TestFactory:
    def test_builds_an_in_memory_store_from_settings(self) -> None:
        store = create_store(Settings(table_count=2, store_backend="memory"))
        assert [s.table_id for s in store.all_snapshots()] == [1, 2]
