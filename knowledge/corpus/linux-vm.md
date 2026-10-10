---
source: CloudRCA curated Linux VM operations guide
version: 1.0
layer: vm_guest_os
product: Linux
---
# Linux VM operations

## Memory pressure
High reclaim activity and swapping indicate guest memory pressure. Correlate the onset with host contention and application latency to distinguish a local workload from an upstream resource problem.

## IO wait
High IO wait means runnable work is waiting for storage. Check the host storage queue and affected database latency in the same time window.
