---
source: CloudRCA curated Redis operations guide
version: 1.0
layer: database
product: Redis
---
# Redis operations

## Connection pressure
Connection timeouts can follow saturated worker CPU, slow disk persistence, or a blocked event loop. Compare Redis latency with the VM and host timeline before naming Redis as the root cause.

## Replication lag
Replication lag is evidence of delayed replica processing. Check network errors, disk latency, and CPU saturation before attributing the lag to a database fault.
