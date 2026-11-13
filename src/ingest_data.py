"""Load the documented Olist subset into the tables created by schema.sql.

Usage, from the repository root:
    python src/ingest_data.py [--limit N]

DATABASE_URL and DATA_DIR come from the environment. The script only inserts
into and updates the five existing tables, in one transaction, so it can be
run again on the same input without changing the stored data.
"""

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import MetaData, create_engine, func, inspect, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

# Parents before children, so every foreign key finds its target.
LOAD_ORDER = ["customers", "sellers", "products", "orders", "order_items"]
FILES = {table: f"olist_{table}_dataset.csv" for table in LOAD_ORDER}

# SHA-256 of the files the documented counts were produced from.
RECORDED_SHA256 = {
    "customers": "983a422239e1712ded753b3bf9ecf47dc73f144d306029dcfa99e70a226883d2",
    "sellers": "1f643d2b950373b85735e7794b20986f528d7a000432e7c6f9bcbb44d0846a0e",
    "products": "3e6569628a17fbc75fd206ee357b59e20364b9afa90f5b6cd5b4d624c58aa9cc",
    "orders": "8df58ef3d2d7e9944010f7beecd9b75367f5588ec6e3c91cec19ae3345ef9ecf",
    "order_items": "0bc4d068c4fe38cbb01bd90e8746e3c613fe7b4baef75fab7b0e329701c3e279",
}

DEFAULT_LIMIT = 10_000
BATCH_ROWS = 1_000
REJECT_DIR = Path("rejects")

HEX32 = r"[0-9a-f]{32}"
STATE = r"[A-Z]{2}"
PLAIN_CITY = r"[a-z '\-]+"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
STATUSES = ["created", "approved", "invoiced", "processing",
            "shipped", "delivered", "unavailable", "canceled"]
MEASUREMENTS = ["product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"]
# Widths of the VARCHAR columns in schema.sql.
CITY_WIDTH = 100
CATEGORY_WIDTH = 100
CENT = Decimal("0.01")

C_WHITESPACE = "whitespace trimmed, repeated spaces in city collapsed"
C_ZERO = "zero product measurement stored as NULL"
F_CITY = "seller_city outside a-z, space, apostrophe, hyphen (loaded unchanged)"
R_ID = "identifier missing or not 32 lowercase hex characters"
R_DUP = "duplicate key within the file"
R_REQUIRED = "required field missing or unparseable, or state not two uppercase letters"
R_FIT = "optional value unparseable, or text longer than its column"
R_STATUS = "order_status outside the eight known values"
R_AMOUNT = "price not above 0, freight_value below 0, or order_item_id below 1"
R_DATES = "approval, customer delivery, or estimated delivery earlier than purchase"
R_PARENT = "parent row absent or rejected"


def parse_args():
    parser = argparse.ArgumentParser(description="Load the Olist subset into PostgreSQL.")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, metavar="N",
        help=f"load only the first N orders by order_id (default {DEFAULT_LIMIT})",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def read_settings():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env, fill it in, and export it.")
    data_dir = Path(os.environ.get("DATA_DIR") or "data/raw")
    missing = [name for name in FILES.values() if not (data_dir / name).is_file()]
    if missing:
        sys.exit(f"Input files not found in {data_dir}: {', '.join(missing)}")
    return database_url, data_dir


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path):
    # Everything is read as text: identifiers keep their leading zeros and no
    # value is converted before a rule has looked at it. Empty cells stay "".
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")


def normalize_text(frame, city_column=None):
    """Trim every field and collapse runs of spaces in the city name.

    Returns the cleaned frame and a mask of the rows that changed.
    """
    clean = frame.apply(lambda column: column.str.strip())
    if city_column:
        clean[city_column] = clean[city_column].str.replace(r"\s+", " ", regex=True)
    return clean, (clean != frame).any(axis=1)


def select_subset(source, limit):
    """First `limit` orders by order_id, plus every row those orders depend on."""
    # order_id is a hash with no relation to the purchase date, so a sorted
    # prefix is a repeatable sample that is spread over the whole period.
    orders = source["orders"].sort_values("order_id").head(limit)
    items = source["order_items"]
    items = items[items["order_id"].isin(orders["order_id"])]
    customers = source["customers"]
    products = source["products"]
    sellers = source["sellers"]
    return {
        "customers": customers[customers["customer_id"].isin(orders["customer_id"])],
        "sellers": sellers[sellers["seller_id"].isin(items["seller_id"])],
        "products": products[products["product_id"].isin(items["product_id"])],
        "orders": orders,
        "order_items": items,
    }


def is_hex(column):
    return column.str.fullmatch(HEX32)


def parse_timestamp(column):
    """Parsed timestamps, and a mask of non-empty values that did not parse."""
    present = column != ""
    parsed = pd.to_datetime(column.where(present), format=TIMESTAMP_FORMAT, errors="coerce")
    return parsed, present & parsed.isna()


def parse_whole_number(column):
    """Nullable integers, and a mask of non-empty values that are not whole numbers."""
    digits = column.str.fullmatch(r"\d+")
    parsed = pd.to_numeric(column.where(digits), errors="coerce").astype("Int64")
    return parsed, (column != "") & ~digits


def parse_money(column):
    """Exact two-decimal amounts; values with any other shape become missing."""
    shaped = column.str.fullmatch(r"-?\d+(\.\d{1,2})?")
    return column.where(shaped).map(lambda text: Decimal(text).quantize(CENT), na_action="ignore")


def true_where(mask):
    """A comparison against a missing value counts as not satisfied."""
    return mask.fillna(False).astype(bool)


def no_reasons(frame):
    return pd.Series("", index=frame.index, dtype=object)


def mark(reasons, failing, rule):
    # A row keeps the first rule it fails, so every rejected row is counted once.
    reasons[failing & (reasons == "")] = rule


def mark_parent(reasons, keys, parent, loaded, rejected):
    """Reject child rows whose parent will not be loaded, and say what happened to it."""
    def explain(key):
        if key in loaded:
            return ""
        if key in rejected:
            return f"{R_PARENT}: {parent} row rejected ({rejected[key]})"
        return f"{R_PARENT}: no {parent} row with this key"

    detail = keys.map(explain)
    failing = (detail != "") & (reasons == "")
    reasons[failing] = detail[failing]


def settle(frame, reasons, rules, stats):
    """Record reject counts per rule and keep the rejected rows for the rejects file."""
    stats["rejected"] = [(rule, int(reasons.str.startswith(rule).sum())) for rule in rules]
    stats["rejects"] = frame[reasons != ""].assign(reject_reason=reasons[reasons != ""])
    return reasons == ""


def loaded_and_rejected(valid, stats, key):
    """Keys that will be loaded, and the reason for each key that was rejected."""
    rejects = stats["rejects"]
    return set(valid[key]), dict(zip(rejects[key], rejects["reject_reason"]))


def validate_customers(frame, stats):
    reasons = no_reasons(frame)
    mark(reasons, ~is_hex(frame["customer_id"]) | ~is_hex(frame["customer_unique_id"]), R_ID)
    mark(reasons, frame["customer_id"].duplicated(keep=False), R_DUP)
    mark(reasons, (frame["customer_city"] == "") | ~frame["customer_state"].str.fullmatch(STATE),
         R_REQUIRED)
    mark(reasons, frame["customer_city"].str.len() > CITY_WIDTH, R_FIT)
    keep = settle(frame, reasons, [R_ID, R_DUP, R_REQUIRED, R_FIT], stats)
    columns = ["customer_id", "customer_unique_id", "customer_city", "customer_state"]
    return frame.loc[keep, columns]


def validate_sellers(frame, stats):
    reasons = no_reasons(frame)
    mark(reasons, ~is_hex(frame["seller_id"]), R_ID)
    mark(reasons, frame["seller_id"].duplicated(keep=False), R_DUP)
    mark(reasons, (frame["seller_city"] == "") | ~frame["seller_state"].str.fullmatch(STATE),
         R_REQUIRED)
    mark(reasons, frame["seller_city"].str.len() > CITY_WIDTH, R_FIT)
    keep = settle(frame, reasons, [R_ID, R_DUP, R_REQUIRED, R_FIT], stats)
    valid = frame.loc[keep, ["seller_id", "seller_city", "seller_state"]]
    # Sellers typed these themselves ("sbc/sp", an e-mail address). We have no
    # reference list to correct them against, so they load as they are.
    odd = valid[~valid["seller_city"].str.fullmatch(PLAIN_CITY)].sort_values("seller_id")
    stats["flagged"] = [(F_CITY, len(odd), list(odd["seller_city"]))]
    return valid


def validate_products(frame, stats):
    reasons = no_reasons(frame)
    mark(reasons, ~is_hex(frame["product_id"]), R_ID)
    mark(reasons, frame["product_id"].duplicated(keep=False), R_DUP)

    category = frame["product_category_name"]
    typed = pd.DataFrame({
        "product_id": frame["product_id"],
        "product_category_name": category.where(category != ""),
    })
    unfit = category.str.len() > CATEGORY_WIDTH
    had_zero = pd.Series(False, index=frame.index)
    for column in MEASUREMENTS:
        number, unparseable = parse_whole_number(frame[column])
        zero = true_where(number == 0)
        # A product cannot weigh or measure nothing: zero means "not measured".
        typed[column] = number.mask(zero)
        had_zero |= zero
        unfit |= unparseable
    mark(reasons, unfit, R_FIT)

    keep = settle(frame, reasons, [R_ID, R_DUP, R_FIT], stats)
    stats["corrected"].append((C_ZERO, int((had_zero & keep).sum())))
    valid = typed[keep]
    stats["nulls"] = [(column, int(valid[column].isna().sum()))
                      for column in ["product_category_name"] + MEASUREMENTS]
    return valid


def validate_orders(frame, stats, customers):
    reasons = no_reasons(frame)
    purchase, _ = parse_timestamp(frame["order_purchase_timestamp"])
    approved, approved_bad = parse_timestamp(frame["order_approved_at"])
    carrier, carrier_bad = parse_timestamp(frame["order_delivered_carrier_date"])
    delivered, delivered_bad = parse_timestamp(frame["order_delivered_customer_date"])
    estimate, _ = parse_timestamp(frame["order_estimated_delivery_date"])

    mark(reasons, ~is_hex(frame["order_id"]) | ~is_hex(frame["customer_id"]), R_ID)
    # customer_id is UNIQUE on orders, so a repeat there is a duplicate key too.
    mark(reasons, frame["order_id"].duplicated(keep=False)
         | frame["customer_id"].duplicated(keep=False), R_DUP)
    mark(reasons, (frame["order_status"] == "") | purchase.isna() | estimate.isna(), R_REQUIRED)
    mark(reasons, approved_bad | carrier_bad | delivered_bad, R_FIT)
    mark(reasons, ~frame["order_status"].isin(STATUSES), R_STATUS)
    # The carrier date is not compared: the source records some handovers
    # before the purchase and some after the delivery.
    mark(reasons, (approved < purchase) | (delivered < purchase)
         | (estimate.dt.normalize() < purchase.dt.normalize()), R_DATES)
    mark_parent(reasons, frame["customer_id"], "customers", *customers)

    keep = settle(frame, reasons, [R_ID, R_DUP, R_REQUIRED, R_FIT, R_STATUS, R_DATES, R_PARENT],
                  stats)
    valid = pd.DataFrame({
        "order_id": frame["order_id"],
        "customer_id": frame["customer_id"],
        "order_status": frame["order_status"],
        "order_purchase_timestamp": purchase,
        "order_approved_at": approved,
        "order_delivered_carrier_date": carrier,
        "order_delivered_customer_date": delivered,
        # The estimate is published as midnight of the promised day.
        "order_estimated_delivery_date": estimate.dt.date,
    })[keep]
    stats["nulls"] = [(column, int(valid[column].isna().sum())) for column in
                      ["order_approved_at", "order_delivered_carrier_date",
                       "order_delivered_customer_date"]]
    return valid


def validate_order_items(frame, stats, orders, products, sellers):
    reasons = no_reasons(frame)
    item_number, _ = parse_whole_number(frame["order_item_id"])
    price = parse_money(frame["price"])
    freight = parse_money(frame["freight_value"])
    key = pd.DataFrame({"order_id": frame["order_id"], "order_item_id": item_number})
    required = ["order_id", "order_item_id", "product_id", "seller_id", "price", "freight_value"]

    mark(reasons, ~is_hex(frame["order_id"]) | ~is_hex(frame["product_id"])
         | ~is_hex(frame["seller_id"]), R_ID)
    mark(reasons, key.duplicated(keep=False) & item_number.notna(), R_DUP)
    mark(reasons, (frame[required] == "").any(axis=1), R_REQUIRED)
    mark(reasons, ~true_where(price > 0) | ~true_where(freight >= 0)
         | ~true_where(item_number >= 1), R_AMOUNT)
    mark_parent(reasons, frame["order_id"], "orders", *orders)
    mark_parent(reasons, frame["product_id"], "products", *products)
    mark_parent(reasons, frame["seller_id"], "sellers", *sellers)

    keep = settle(frame, reasons, [R_ID, R_DUP, R_REQUIRED, R_AMOUNT, R_PARENT], stats)
    return pd.DataFrame({
        "order_id": frame["order_id"],
        "order_item_id": item_number,
        "product_id": frame["product_id"],
        "seller_id": frame["seller_id"],
        "price": price,
        "freight_value": freight,
    })[keep]


def validate(selected, report):
    """Validate parents first so each child can be checked against loadable parents."""
    valid = {}
    valid["customers"] = validate_customers(selected["customers"], report["customers"])
    valid["sellers"] = validate_sellers(selected["sellers"], report["sellers"])
    valid["products"] = validate_products(selected["products"], report["products"])
    valid["orders"] = validate_orders(
        selected["orders"], report["orders"],
        loaded_and_rejected(valid["customers"], report["customers"], "customer_id"),
    )
    valid["order_items"] = validate_order_items(
        selected["order_items"], report["order_items"],
        loaded_and_rejected(valid["orders"], report["orders"], "order_id"),
        loaded_and_rejected(valid["products"], report["products"], "product_id"),
        loaded_and_rejected(valid["sellers"], report["sellers"], "seller_id"),
    )
    return valid


def write_rejects(report):
    # One file per table on every run, so the folder always describes the last run.
    REJECT_DIR.mkdir(exist_ok=True)
    for table in LOAD_ORDER:
        report[table]["rejects"].to_csv(REJECT_DIR / f"{table}_rejected.csv", index=False)


def to_rows(frame):
    """Rows as plain Python values, with None for every missing value."""
    def plain(value):
        if pd.isna(value):
            return None
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime()
        return value

    return [{name: plain(value) for name, value in row.items()}
            for row in frame.to_dict("records")]


def count_rows(connection, table):
    return connection.execute(select(func.count()).select_from(table)).scalar_one()


def upsert(connection, table, rows):
    """Insert new rows and update rows whose stored values differ.

    Returns (inserted, updated, unchanged).
    """
    key = [column.name for column in table.primary_key.columns]
    rest = [column.name for column in table.columns if column.name not in key]
    before = count_rows(connection, table)
    written = 0
    for start in range(0, len(rows), BATCH_ROWS):
        statement = pg_insert(table).values(rows[start:start + BATCH_ROWS])
        stored = tuple_(*[table.c[name] for name in rest])
        incoming = tuple_(*[statement.excluded[name] for name in rest])
        # Without this WHERE a rerun would rewrite every row. With it, a row
        # that already matches is skipped, and RETURNING yields only the rows
        # that were actually inserted or updated.
        statement = statement.on_conflict_do_update(
            index_elements=key,
            set_={name: statement.excluded[name] for name in rest},
            where=stored.is_distinct_from(incoming),
        ).returning(table.c[key[0]])
        written += len(connection.execute(statement).all())
    inserted = count_rows(connection, table) - before
    return inserted, written - inserted, len(rows) - written


def load(engine, valid, report):
    missing = sorted(set(LOAD_ORDER) - set(inspect(engine).get_table_names()))
    if missing:
        sys.exit(f"Tables not found: {', '.join(missing)}. Create them with schema.sql first.")
    metadata = MetaData()
    metadata.reflect(engine, only=LOAD_ORDER)
    # One transaction for all five tables: an error in any of them rolls back
    # the rows already written for the others.
    with engine.begin() as connection:
        for table in LOAD_ORDER:
            report[table]["load"] = upsert(connection, metadata.tables[table],
                                           to_rows(valid[table]))


def print_line(label, value):
    print(f"  {label:<86}{value:>8}")


def print_table_report(table, stats):
    print(f"\n{table}")
    print_line("rows read", stats["read"])
    print_line("rows selected", stats["selected"])
    for label, count in stats["corrected"]:
        print_line(f"corrected: {label}", count)
    for label, count, values in stats.get("flagged", []):
        print_line(f"flagged: {label}", count)
        for value in values:
            print(f"      {value!r}")
    for label, count in stats["rejected"]:
        print_line(f"rejected: {label}", count)
    print_line("rows rejected in total", len(stats["rejects"]))
    for column, count in stats.get("nulls", []):
        print_line(f"missing values kept as NULL: {column}", count)
    if "load" in stats:
        inserted, updated, unchanged = stats["load"]
        print_line("rows inserted", inserted)
        print_line("rows updated", updated)
        print_line("rows unchanged", unchanged)


def main():
    args = parse_args()
    database_url, data_dir = read_settings()
    # The report prints city names with non-ASCII characters. When output is
    # piped on Windows, Python would otherwise write it in the local code page.
    sys.stdout.reconfigure(encoding="utf-8")

    print(f"Run started {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    print(f"Order limit: first {args.limit} orders by order_id")

    print("\nInput files (SHA-256)")
    for table in LOAD_ORDER:
        digest = sha256_of(data_dir / FILES[table])
        same = digest == RECORDED_SHA256[table]
        note = "matches recorded value" if same else "WARNING: differs from recorded value"
        print(f"  {FILES[table]:<32}{digest}  {note}")

    source, changed = {}, {}
    for table in LOAD_ORDER:
        raw = read_csv(data_dir / FILES[table])
        city = {"customers": "customer_city", "sellers": "seller_city"}.get(table)
        source[table], changed[table] = normalize_text(raw, city)

    selected = select_subset(source, args.limit)
    report = {}
    for table in LOAD_ORDER:
        report[table] = {
            "read": len(source[table]),
            "selected": len(selected[table]),
            "corrected": [(C_WHITESPACE, int(changed[table][selected[table].index].sum()))],
        }
    print("\nSelected orders")
    print(f"  first {selected['orders']['order_id'].iloc[0]}")
    print(f"  last  {selected['orders']['order_id'].iloc[-1]}")

    valid = validate(selected, report)
    write_rejects(report)

    failure = None
    try:
        load(create_engine(database_url), valid, report)
    except SQLAlchemyError as error:
        # Only the driver's own message is shown; the full exception text can
        # include the statement and its parameters.
        failure = str(getattr(error, "orig", None) or error).strip()
        for stats in report.values():
            stats.pop("load", None)

    for table in LOAD_ORDER:
        print_table_report(table, report[table])
    print(f"\nRejected rows are written to {REJECT_DIR}/, one file per table.")
    if failure:
        print(f"\nLoad failed and was rolled back. No table was changed.\n{failure}")
        sys.exit(1)
    print("Load committed.")


if __name__ == "__main__":
    main()
