from models.order import OrderItem, OrderStatus
from repository.order_repository import OrderRepository
from services.order_service import OrderService, OrderNotFoundError, InvalidStatusTransitionError


def print_order(order):
    print(f"\nOrder ID : {order.id}")
    print(f"Customer : {order.customer_id}")
    print(f"Status   : {order.status.value}")
    print(f"Items    :")
    for item in order.items:
        print(f"  - {item.product_name} x{item.quantity} @ ${item.unit_price:.2f} = ${item.total_price:.2f}")
    print(f"Total    : ${order.total_amount:.2f}")
    print(f"Created  : {order.created_at.strftime('%Y-%m-%d %H:%M:%S %Z')}")


def main():
    repo = OrderRepository()
    service = OrderService(repo)

    print("=== Order Management Demo ===")

    # Create orders
    items1 = [
        OrderItem("P001", "Laptop", 1, 999.99),
        OrderItem("P002", "Mouse", 2, 29.99),
    ]
    order1 = service.create_order("CUST-001", items1)
    print("\n[1] Created order:")
    print_order(order1)

    items2 = [
        OrderItem("P003", "Keyboard", 1, 79.99),
    ]
    order2 = service.create_order("CUST-002", items2)
    print("\n[2] Created second order:")
    print_order(order2)

    # Update status
    service.update_status(order1.id, OrderStatus.CONFIRMED)
    service.update_status(order1.id, OrderStatus.SHIPPED)
    print("\n[3] Order 1 after shipping:")
    print_order(service.get_order(order1.id))

    # Add item to pending order
    service.add_item(order2.id, OrderItem("P004", "USB Hub", 1, 19.99))
    print("\n[4] Order 2 after adding item:")
    print_order(service.get_order(order2.id))

    # Cancel order
    service.cancel_order(order2.id)
    print("\n[5] Order 2 after cancellation:")
    print_order(service.get_order(order2.id))

    # List all orders for customer
    print("\n[6] All orders for CUST-001:")
    for o in service.list_orders(customer_id="CUST-001"):
        print(f"  Order {o.id[:8]}... | {o.status.value} | ${o.total_amount:.2f}")

    # Error handling demo
    print("\n[7] Error handling:")
    try:
        service.update_status(order2.id, OrderStatus.CONFIRMED)
    except InvalidStatusTransitionError as e:
        print(f"  Caught expected error: {e}")

    try:
        service.get_order("bad-id")
    except OrderNotFoundError as e:
        print(f"  Caught expected error: {e}")


if __name__ == "__main__":
    main()
