import pytest
from models.order import Order, OrderItem, OrderStatus
from repository.order_repository import OrderRepository
from services.order_service import OrderService, OrderNotFoundError, InvalidStatusTransitionError


@pytest.fixture
def service():
    return OrderService(OrderRepository())


@pytest.fixture
def sample_items():
    return [
        OrderItem(product_id="P001", product_name="Widget A", quantity=2, unit_price=10.0),
        OrderItem(product_id="P002", product_name="Widget B", quantity=1, unit_price=25.0),
    ]


def test_create_order(service, sample_items):
    order = service.create_order("C001", sample_items)
    assert order.customer_id == "C001"
    assert len(order.items) == 2
    assert order.status == OrderStatus.PENDING
    assert order.total_amount == 45.0


def test_get_order(service, sample_items):
    created = service.create_order("C001", sample_items)
    fetched = service.get_order(created.id)
    assert fetched.id == created.id


def test_get_order_not_found(service):
    with pytest.raises(OrderNotFoundError):
        service.get_order("nonexistent-id")


def test_update_status_valid_transition(service, sample_items):
    order = service.create_order("C001", sample_items)
    updated = service.update_status(order.id, OrderStatus.CONFIRMED)
    assert updated.status == OrderStatus.CONFIRMED


def test_update_status_invalid_transition(service, sample_items):
    order = service.create_order("C001", sample_items)
    with pytest.raises(InvalidStatusTransitionError):
        service.update_status(order.id, OrderStatus.DELIVERED)


def test_cancel_order(service, sample_items):
    order = service.create_order("C001", sample_items)
    cancelled = service.cancel_order(order.id)
    assert cancelled.status == OrderStatus.CANCELLED


def test_add_item_to_pending_order(service, sample_items):
    order = service.create_order("C001", sample_items)
    new_item = OrderItem(product_id="P003", product_name="Widget C", quantity=3, unit_price=5.0)
    updated = service.add_item(order.id, new_item)
    assert len(updated.items) == 3


def test_add_item_to_confirmed_order_fails(service, sample_items):
    order = service.create_order("C001", sample_items)
    service.update_status(order.id, OrderStatus.CONFIRMED)
    new_item = OrderItem(product_id="P003", product_name="Widget C", quantity=1, unit_price=5.0)
    with pytest.raises(InvalidStatusTransitionError):
        service.add_item(order.id, new_item)


def test_remove_item(service, sample_items):
    order = service.create_order("C001", sample_items)
    updated = service.remove_item(order.id, "P001")
    assert len(updated.items) == 1
    assert updated.items[0].product_id == "P002"


def test_list_orders_by_customer(service, sample_items):
    service.create_order("C001", sample_items)
    service.create_order("C001", sample_items)
    service.create_order("C002", sample_items)
    orders = service.list_orders(customer_id="C001")
    assert len(orders) == 2


def test_list_orders_by_status(service, sample_items):
    o1 = service.create_order("C001", sample_items)
    o2 = service.create_order("C002", sample_items)
    service.update_status(o1.id, OrderStatus.CONFIRMED)
    pending = service.list_orders(status=OrderStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].id == o2.id
