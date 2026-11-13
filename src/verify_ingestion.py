"""Check the loaded Olist subset against the documented expectations.

Usage, from the repository root:
    python src/verify_ingestion.py

Reads DATABASE_URL from the environment. Every check is one SQL query that
returns one value, printed beside its expected value. The exit status is
non-zero if any value differs. The output contains no timestamps, so the
output of two runs can be compared with diff.
"""

import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


def count_duplicates(table, key):
    return (f"SELECT count(*) FROM (SELECT {key} FROM {table} "
            f"GROUP BY {key} HAVING count(*) > 1) AS repeated")


def count_without_match(table, other, column, other_column=None):
    """Rows of `table` whose `column` value has no matching row in `other`."""
    other_column = other_column or column
    return (f"SELECT count(*) FROM {table} AS t WHERE NOT EXISTS "
            f"(SELECT 1 FROM {other} AS o WHERE o.{other_column} = t.{column})")


def fingerprint(table, key_order):
    # Every column of every row goes into the hash, so a change to any stored
    # value changes it; row counts and sums alone would miss most edits.
    # COLLATE "C" and the UTF8 conversion keep the hash the same on any server.
    return (f"SELECT md5(convert_to(string_agg(t::text, E'\\n' ORDER BY {key_order}), 'UTF8')) "
            f"FROM {table} AS t")


ORDERS_WITHOUT_ITEMS = ("FROM orders AS o WHERE NOT EXISTS "
                        "(SELECT 1 FROM order_items AS i WHERE i.order_id = o.order_id)")
UNCATEGORISED = "SELECT product_id FROM products WHERE product_category_name IS NULL"

# (label, SQL returning one value, expected value)
CHECKS = {
    "Row counts": [
        ("customers", "SELECT count(*) FROM customers", 10000),
        ("sellers", "SELECT count(*) FROM sellers", 1641),
        ("products", "SELECT count(*) FROM products", 6686),
        ("orders", "SELECT count(*) FROM orders", 10000),
        ("order_items", "SELECT count(*) FROM order_items", 11310),
    ],
    "Duplicate keys": [
        ("customers.customer_id values occurring more than once",
         count_duplicates("customers", "customer_id"), 0),
        ("sellers.seller_id values occurring more than once",
         count_duplicates("sellers", "seller_id"), 0),
        ("products.product_id values occurring more than once",
         count_duplicates("products", "product_id"), 0),
        ("orders.order_id values occurring more than once",
         count_duplicates("orders", "order_id"), 0),
        ("order_items (order_id, order_item_id) pairs occurring more than once",
         count_duplicates("order_items", "order_id, order_item_id"), 0),
        ("orders.customer_id values occurring more than once",
         count_duplicates("orders", "customer_id"), 0),
    ],
    "Referential checks": [
        ("orders with no matching customer",
         count_without_match("orders", "customers", "customer_id"), 0),
        ("order_items with no matching order",
         count_without_match("order_items", "orders", "order_id"), 0),
        ("order_items with no matching product",
         count_without_match("order_items", "products", "product_id"), 0),
        ("order_items with no matching seller",
         count_without_match("order_items", "sellers", "seller_id"), 0),
        ("customers with no order",
         count_without_match("customers", "orders", "customer_id"), 0),
        ("products no order item references",
         count_without_match("products", "order_items", "product_id"), 0),
        ("sellers no order item references",
         count_without_match("sellers", "order_items", "seller_id"), 0),
        ("orders with no items", f"SELECT count(*) {ORDERS_WITHOUT_ITEMS}", 76),
        ("orders with no items, status unavailable",
         f"SELECT count(*) {ORDERS_WITHOUT_ITEMS} AND o.order_status = 'unavailable'", 55),
        ("orders with no items, status canceled",
         f"SELECT count(*) {ORDERS_WITHOUT_ITEMS} AND o.order_status = 'canceled'", 21),
    ],
    "Domain quality: patterns the schema allows on purpose": [
        ("delivered orders with no customer delivery date",
         "SELECT count(*) FROM orders WHERE order_status = 'delivered' "
         "AND order_delivered_customer_date IS NULL", 1),
        ("non-delivered orders with a customer delivery date",
         "SELECT count(*) FROM orders WHERE order_status <> 'delivered' "
         "AND order_delivered_customer_date IS NOT NULL", 1),
        ("delivered orders with no approval timestamp",
         "SELECT count(*) FROM orders WHERE order_status = 'delivered' "
         "AND order_approved_at IS NULL", 1),
        ("orders with carrier handover before purchase",
         "SELECT count(*) FROM orders "
         "WHERE order_delivered_carrier_date < order_purchase_timestamp", 20),
        ("orders with customer delivery before carrier handover",
         "SELECT count(*) FROM orders "
         "WHERE order_delivered_customer_date < order_delivered_carrier_date", 1),
        ("products with no category",
         "SELECT count(*) FROM products WHERE product_category_name IS NULL", 103),
        ("order_items that reference a product with no category",
         f"SELECT count(*) FROM order_items WHERE product_id IN ({UNCATEGORISED})", 167),
        ("order_items with zero freight",
         "SELECT count(*) FROM order_items WHERE freight_value = 0", 40),
        ("customer_unique_id values under more than one customer_id",
         count_duplicates("customers", "customer_unique_id"), 34),
        ("seller_city values with characters other than a-z, space, apostrophe, hyphen",
         "SELECT count(*) FROM sellers WHERE seller_city !~ '^[a-z '' -]+$'", 9),
    ],
    "Domain quality: missing values kept as NULL": [
        ("orders.order_approved_at",
         "SELECT count(*) FROM orders WHERE order_approved_at IS NULL", 20),
        ("orders.order_delivered_carrier_date",
         "SELECT count(*) FROM orders WHERE order_delivered_carrier_date IS NULL", 182),
        ("orders.order_delivered_customer_date",
         "SELECT count(*) FROM orders WHERE order_delivered_customer_date IS NULL", 286),
        ("products.product_weight_g",
         "SELECT count(*) FROM products WHERE product_weight_g IS NULL", 3),
        ("products.product_length_cm",
         "SELECT count(*) FROM products WHERE product_length_cm IS NULL", 1),
        ("products.product_height_cm",
         "SELECT count(*) FROM products WHERE product_height_cm IS NULL", 1),
        ("products.product_width_cm",
         "SELECT count(*) FROM products WHERE product_width_cm IS NULL", 1),
    ],
    "Keys and measures": [
        ("distinct order_id in order_items",
         "SELECT count(DISTINCT order_id) FROM order_items", 9924),
        ("highest order_item_id", "SELECT max(order_item_id) FROM order_items", 8),
        ("distinct customer_unique_id",
         "SELECT count(DISTINCT customer_unique_id) FROM customers", 9966),
        ("SUM(price)", "SELECT sum(price) FROM order_items", "1363974.94"),
        ("SUM(freight_value)", "SELECT sum(freight_value) FROM order_items", "227417.02"),
        ("MIN(price)", "SELECT min(price) FROM order_items", "3.49"),
        ("MAX(price)", "SELECT max(price) FROM order_items", "6735.00"),
    ],
    "Order status counts": [
        (status, f"SELECT count(*) FROM orders WHERE order_status = '{status}'", expected)
        for status, expected in [
            ("delivered", 9714), ("shipped", 98), ("canceled", 71), ("unavailable", 55),
            ("invoiced", 31), ("processing", 30), ("approved", 1), ("created", 0),
        ]
    ],
    "Fingerprints: MD5 over all rows as text, ordered by primary key": [
        ("customers", fingerprint("customers", 't.customer_id COLLATE "C"'),
         "3faafdadacddbfabed94624db4edd27f"),
        ("sellers", fingerprint("sellers", 't.seller_id COLLATE "C"'),
         "a2e7b0f1d5906dc4e6cee2dae6017c9f"),
        ("products", fingerprint("products", 't.product_id COLLATE "C"'),
         "520f3e1f9fb5bbdf5ecdf4512d4eb941"),
        ("orders", fingerprint("orders", 't.order_id COLLATE "C"'),
         "df5e2fc9513cb6376840015227025e44"),
        ("order_items", fingerprint("order_items", 't.order_id COLLATE "C", t.order_item_id'),
         "f90851c7b6e575aad31fe02021123919"),
    ],
}


def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env, fill it in, and export it.")

    failed = 0
    total = 0
    try:
        with create_engine(database_url).connect() as connection:
            # Dates inside the fingerprint text must not depend on server settings.
            connection.execute(text("SET datestyle TO 'ISO, YMD'"))
            for group, checks in CHECKS.items():
                print(f"\n{group}")
                for label, sql, expected in checks:
                    actual = connection.execute(text(sql)).scalar()
                    passed = str(actual) == str(expected)
                    total += 1
                    failed += not passed
                    mark = "ok" if passed else "FAIL"
                    print(f"  [{mark}] {label}: {actual} (expected {expected})")
    except SQLAlchemyError as error:
        reason = str(getattr(error, "orig", None) or error).strip()
        sys.exit(f"Verification could not run: {reason}")

    print(f"\n{total} checks, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
