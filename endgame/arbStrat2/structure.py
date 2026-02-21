# structure.py
from dataclasses import dataclass

# Default position limit used by check_breach_position_limit
DEFAULT_POSITION_LIMIT = 100


@dataclass
class OrderDetail:
    """
    Our internal representation of an order to be sent to the exchange.
    """
    instrument_id: str
    price: float
    volume: int
    side: str            # 'bid' or 'ask'
    order_type: str = "limit"


def _net_outstanding_for_instrument(outstanding_orders_for_instr: dict) -> int:
    """
    Turn current outstanding orders into a net position effect.

    bids increase exposure, asks decrease exposure.
    """
    net = 0
    for order_id, o in outstanding_orders_for_instr.items():
        if o.side == "bid":
            net += o.volume
        else:
            net -= o.volume
    return net


def check_breach_position_limit(
    order: OrderDetail,
    positions: dict,
    outstanding_orders_for_instr: dict,
    position_limit: int = DEFAULT_POSITION_LIMIT,
) -> bool:
    """
    Return True if sending THIS order would breach the position limit.

    We take:
    - current inventory (positions)
    - plus what’s already working in the book (outstanding orders)
    - plus this new order
    and check if the absolute value goes over the limit.
    """
    current_pos = positions.get(order.instrument_id, 0)

    # what’s already in the book for this instrument
    working_net = _net_outstanding_for_instrument(outstanding_orders_for_instr)

    # effect of this new order
    if order.side == "bid":
        projected = current_pos + working_net + order.volume
    else:
        projected = current_pos + working_net - order.volume

    return abs(projected) > position_limit
