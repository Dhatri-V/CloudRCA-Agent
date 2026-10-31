---
source: CloudRCA curated FastAPI and SQLite application guide
version: 1.0
layer: application
product: FastAPI with SQLAlchemy on SQLite
---
# Application behaviour with SQLite

## Lock contention
SQLite allows one writer at a time. "database is locked" errors can come from concurrent write requests, long-running transactions, or sessions that are not closed after an exception. Check request concurrency, write rate, and per-request database time before naming the host or the database file as the root cause.

## Resource-driven slowdowns
A slow host can lengthen transactions and so increase lock waits, but CPU pressure alone does not prove lock contention. Require evidence that database operations slowed during the pressure window before linking the two.
