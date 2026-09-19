"""Order processing. Nothing personal is written here."""

import logging

from app.db import db
from app.models import Order

logger = logging.getLogger(__name__)


def place_order(user_id: int, sku: str, qty: int) -> Order:
    order = Order(user_id=user_id, sku=sku, qty=qty)
    db.session.add(order)
    db.session.commit()
    logger.info("order placed order_id=%s sku=%s qty=%d", order.id, sku, qty)
    return order
