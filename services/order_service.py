from datetime import datetime, timezone
from typing import List, Optional

from models.order import Order, OrderItem, OrderStatus
from repository.order_repository import OrderRepository


class OrderNotFoundError(Exception):
    pass


class InvalidStatusTransitionError(Exception):
    pass


VALID_TRANSITIONS = {
    OrderStatus.PENDING: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


class OrderService:
    def __init__(self, repository: OrderRepository):
        self._repo = repository

    def create_order(self, customer_id: str, items: List[OrderItem]) -> Order:
        order = Order(customer_id=customer_id, items=items)
        return self._repo.save(order)

    def get_order(self, order_id: str) -> Order:
        order = self._repo.get_by_id(order_id)
        if not order:
            raise OrderNotFoundError(f"Order {order_id} not found")
        return order

    def list_orders(self, customer_id: Optional[str] = None, status: Optional[OrderStatus] = None) -> List[Order]:
        if customer_id:
            orders = self._repo.get_by_customer(customer_id)
        elif status:
            orders = self._repo.get_by_status(status)
        else:
            orders = self._repo.get_all()
        return orders

    def update_status(self, order_id: str, new_status: OrderStatus) -> Order:
        order = self.get_order(order_id)
        allowed = VALID_TRANSITIONS[order.status]
        if new_status not in allowed:
            raise InvalidStatusTransitionError(
                f"Cannot transition from {order.status.value} to {new_status.value}"
            )
        order.status = new_status
        order.updated_at = datetime.now(timezone.utc)
        return self._repo.save(order)

    def add_item(self, order_id: str, item: OrderItem) -> Order:
        order = self.get_order(order_id)
        if order.status != OrderStatus.PENDING:
            raise InvalidStatusTransitionError("Items can only be added to pending orders")
        order.add_item(item)
        return self._repo.save(order)

    def remove_item(self, order_id: str, product_id: str) -> Order:
        order = self.get_order(order_id)
        if order.status != OrderStatus.PENDING:
            raise InvalidStatusTransitionError("Items can only be removed from pending orders")
        if not order.remove_item(product_id):
            raise ValueError(f"Product {product_id} not found in order")
        return self._repo.save(order)

    def cancel_order(self, order_id: str) -> Order:
        return self.update_status(order_id, OrderStatus.CANCELLED)