---
source: CloudRCA curated KVM operations guide
version: 1.0
layer: host_hypervisor
product: KVM
---
# KVM operations

## Storage queue saturation
An elevated host storage queue can delay guest IO. Verify that affected guests run on the host and that guest IO wait starts after host contention.

## CPU contention
Host CPU contention can increase guest scheduling delay. Treat it as a candidate cause only when timing and topology evidence connect it to the affected VM.
