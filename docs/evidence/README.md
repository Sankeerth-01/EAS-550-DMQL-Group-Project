# Olist retail analytics: relational source schema and ingestion

## Project

We model orders from Olist, a Brazilian online marketplace where independent sellers list products and customers buy them. This repository holds the normalized PostgreSQL source schema and a repeatable load of a documented 10,000-order subset. The data will later answer two kinds of question: sales by product category and month, and item price and freight charge by seller.

## Data source and terms

- Dataset: Brazilian E-Commerce Public Dataset by Olist, published by Olist on Kaggle.
- URL: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce
- Licence: CC BY-NC-SA 4.0. The data is used here for non-commercial educational work, with attribution to Olist, and is not redistributed: the CSV files are not committed to this repository.
- Version 2, downloaded on 2026-11-11.

We use five files from the download. Ingestion prints the SHA-256 of each file and warns if it differs from the value below.

| File | SHA-256 |
|---|---|
| olist_customers_dataset.csv | `983a422239e1712ded753b3bf9ecf47dc73f144d306029dcfa99e70a226883d2` |
| olist_sellers_dataset.csv | `1f643d2b950373b85735e7794b20986f528d7a000432e7c6f9bcbb44d0846a0e` |
| olist_products_dataset.csv | `3e6569628a17fbc75fd206ee357b59e20364b9afa90f5b6cd5b4d624c58aa9cc` |
| olist_orders_dataset.csv | `8df58ef3d2d7e9944010f7beecd9b75367f5588ec6e3c91cec19ae3345ef9ecf` |
| olist_order_items_dataset.csv | `0bc4d068c4fe38cbb01bd90e8746e3c613fe7b4baef75fab7b0e329701c3e279` |

## Subset

Selection rule:

1. Sort `olist_orders_dataset.csv` by `order_id` ascending, as a plain string.
2. Keep the first 10,000 orders, every status included: `00010242fe8c5a6d1ba2dd792cb16214` to `19c37103c79e5f4a03f5fcc5b1fe97e7`.
3. Keep the customers those orders reference and all order items of those orders.
4. Keep the products and sellers those items reference.

`order_id` is a 32-character hex hash that is unrelated to the purchase date, so the sorted prefix behaves like a random sample and anyone can reproduce it without a seed. The subset covers purchases from October 2016 to October 2018 and takes between 9.3% and 11.9% of each month's orders from January 2017 to August 2018.

| Table | Rows in source file | Rows in subset |
|---|---:|---:|
| customers | 99,441 | 10,000 |
| sellers | 3,095 | 1,641 |
| products | 32,951 | 6,686 |
| orders | 99,441 | 10,000 |
| order_items | 112,650 | 11,310 |

## Requirements

Tested with Python 3.14.3 and PostgreSQL 18.7. The Python libraries are pinned in `requirements.txt`: pandas 3.1.1, SQLAlchemy 2.1.8, and the psycopg 3.3.9 driver. The commands below are for bash (macOS, Linux, WSL, or Git Bash) and need `psql` and `createdb` on the PATH.

## Setup

Run everything from the repository root.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

createdb olist_source

cp .env.example .env
mkdir -p data/raw
```

Then:

1. Edit `.env` and set `DATABASE_URL` in the form `postgresql://USER:PASSWORD@HOST:PORT/DBNAME`, in double quotes, for example `DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/olist_source"`. `DATA_DIR` can stay empty; it defaults to `data/raw`.
2. Copy the five CSV files listed above into `data/raw/`.
3. Export the variables into the current shell:

```bash
set -a; source .env; set +a
```

If your PostgreSQL superuser is `postgres` and not your own login, create the database with `createdb -U postgres olist_source`. A hosted PostgreSQL works the same way: create an empty database there and put its connection string in `DATABASE_URL`. `.env` and `data/raw/` are ignored by Git.

## Run

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema.sql

python src/ingest_data.py     | tee docs/evidence/run1_ingest.txt
python src/verify_ingestion.py | tee docs/evidence/run1_verify.txt

python src/ingest_data.py     | tee docs/evidence/run2_ingest.txt
python src/verify_ingestion.py | tee docs/evidence/run2_verify.txt

diff docs/evidence/run1_verify.txt docs/evidence/run2_verify.txt | tee docs/evidence/rerun_diff.txt
```

`schema.sql` creates the five tables in one transaction. `ingest_data.py` validates the subset and loads it in one transaction, parents before children; rows it rejects are written with a reason to `rejects/`, which Git ignores. `verify_ingestion.py` runs 58 labelled SQL checks and exits non-zero if any value differs from its expected value.

For a quick trial before the full load, `python src/ingest_data.py --limit 200` loads only the first 200 orders. The full load can follow on the same database; it then reports those rows as unchanged.

## Expected results

- First ingestion: 0 rows rejected, and rows inserted equal to the subset counts above.
- Second ingestion: 0 inserted, 0 updated, and every row unchanged, in all five tables.
- Both verification runs end with `58 checks, 0 failed`, and `rerun_diff.txt` is empty.

| Check | Expected |
|---|---:|
| SUM(price) | 1,363,974.94 |
| SUM(freight_value) | 227,417.02 |
| MIN(price), MAX(price) | 3.49, 6,735.00 |
| Distinct orders present in order_items | 9,924 |
| Orders with no items | 76 (55 unavailable, 21 canceled) |
| Highest order_item_id | 8 |
| Distinct customer_unique_id | 9,966 |
| Orders by status | delivered 9,714; shipped 98; canceled 71; unavailable 55; invoiced 31; processing 30; approved 1 |
| Rows corrected | 2 sellers (repeated spaces in the city), 2 products (zero weight stored as NULL) |
| Rows flagged and loaded unchanged | 9 sellers with an unusual city value |

Row counts alone would not show a changed value, so verification also prints an MD5 fingerprint of each table, computed over every column of every row in primary-key order. The fingerprints are identical in the two runs and match the values recorded in the script.

Output of our two runs on an empty database:

- [run1_ingest.txt](docs/evidence/run1_ingest.txt) and [run1_verify.txt](docs/evidence/run1_verify.txt)
- [run2_ingest.txt](docs/evidence/run2_ingest.txt) and [run2_verify.txt](docs/evidence/run2_verify.txt)
- [rerun_diff.txt](docs/evidence/rerun_diff.txt), which is empty

The commands in the Run section overwrite these files. On unchanged input, only the first line of each ingest file, the run time, should differ.

## Design

- [Entity-relationship diagram](docs/erd.md), also exported as [erd.png](docs/erd.png)
- [Design note](docs/design_note.md): functional dependencies, the 3NF argument, decomposition decisions, and the constraints and exceptions. The same note as a PDF: [design_note.pdf](docs/design_note.pdf)

## Reset

`schema.sql` has no DROP statements and ingestion never deletes rows. To start over, recreate the database and repeat the Run section:

```bash
dropdb olist_source
createdb olist_source
```

On a hosted service, delete and recreate the database from its console.
