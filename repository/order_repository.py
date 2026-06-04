from typing import Dict, List, Optional
from models.order import Order, OrderStatus


class OrderRepository:
    def __init__(self):
        self._orders: Dict[str, Order] = {}

    def save(self, order: Order) -> Order:
        self._orders[order.id] = order
        return order

    def get_by_id(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_by_customer(self, customer_id: str) -> List[Order]:
        return [o for o in self._orders.values() if o.customer_id == customer_id]

    def get_by_status(self, status: OrderStatus) -> List[Order]:
        return [o for o in self._orders.values() if o.status == status]

    def get_all(self) -> List[Order]:
        return list(self._orders.values())

    def delete(self, order_id: str) -> bool:
        if order_id in self._orders:
            del self._orders[order_id]
            return True
        return False
