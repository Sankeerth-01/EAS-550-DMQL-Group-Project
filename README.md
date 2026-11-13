# Olist retail analytics: relational source schema and ingestion

## Project

We model orders from Olist, a Brazilian online marketplace where independent sellers list products and customers buy them. This repository holds the normalized PostgreSQL source schema and a repeatable load of a documented 10,000-order subset. The data will later answer two kinds of question: sales by product category and month, and item price and freight charge by seller.

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
