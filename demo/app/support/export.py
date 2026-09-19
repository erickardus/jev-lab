"""Nightly customer export for the support team."""

import csv
import logging
from pathlib import Path

from app.models import User

logger = logging.getLogger(__name__)
EXPORT_DIR = Path("/var/exports")


def export_customers() -> Path:
    out = EXPORT_DIR / "customers.csv"
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "name", "email", "phone", "street_address"])
        for u in User.query.all():
            writer.writerow([u.id, u.name, u.email, u.phone, u.street_address])
    logger.info("exported %d customers to %s", User.query.count(), out)
    return out
