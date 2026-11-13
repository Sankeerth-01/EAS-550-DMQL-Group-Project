# Olist retail analytics: relational source schema and ingestion

## Project

We model orders from Olist, a Brazilian online marketplace where independent sellers list products and customers buy them. This repository holds the normalized PostgreSQL source schema and a repeatable load of a documented 10,000-order subset. The data will later answer two kinds of question: sales by product category and month, and item price and freight charge by seller.

## Design

- [Entity-relationship diagram](docs/erd.md), also exported as [erd.png](docs/erd.png)
- [Design note](docs/design_note.md): functional dependencies, the 3NF argument, decomposition decisions, and the constraints and exceptions. The same note as a PDF: [design_note.pdf](docs/design_note.pdf)
