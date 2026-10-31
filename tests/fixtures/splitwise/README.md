# Synthetic Splitwise CloudRCA records

**Synthetic, hand-written test data** (messages start with `SYNTHETIC:`), not output from a real deployment. `cloudrca_records.jsonl` contains one example of each CloudRCA record type (`aws.cloudwatch.metric`, `host.heartbeat`, `app.request_summary`, and `app.error` reported by the database and by the application) for instance `i-0synthetic01`. It exercises normalization and routing only; it encodes no expected root cause. No personal data, SQL parameters, or request bodies.
