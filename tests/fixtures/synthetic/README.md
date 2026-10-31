# Synthetic fixtures

**These records are synthetic.** They are hand-written test data, not observations from any real system, and every log message starts with `SYNTHETIC:`. They exist to exercise the full RCA path (specialist findings → main RCA → report) because the sanitized AIOps2025 fixtures in `../aiops2025/` contain no anomalies. Never present results on these files as real-world accuracy.

`redis_vm_cpu_incident.jsonl` uses the AIOps2025 record shapes (filebeat-style logs and node metrics) so the existing normalizer accepts it unchanged. Scenario and known expected root cause: `redis_vm_cpu_incident.expected.json` (CPU saturation on VM `vm-worker-08`, with Redis `redis-cart-0` as the downstream symptom).
