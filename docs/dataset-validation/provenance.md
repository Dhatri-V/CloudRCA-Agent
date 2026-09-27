# AIOps2025 validation provenance

## Canonical sources

- Full release: <https://www.aiops.cn/gitlab/aiops-live-benchmark/agenticopseval/-/tree/main/AIOps2025>
- Official sample: <https://www.aiops.cn/gitlab/aiops-live-benchmark/aiopschallengedata2025-sample>
- Sample repository commit inspected: `82afa405c8f0ff4fb008678906601820a96db9b2`
- License: Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0), as stated in the full-release README.

The release is distributed as 18 daily Git LFS archives plus `input.json`, `groundtruth.jsonl`, checksums, and documentation. Daily archives range from 419,611,075 to 769,281,074 compressed bytes. The complete published telemetry is approximately 11.9 GB.

## Real files inspected

The following files were downloaded to a temporary directory and were not committed. Archive hashes match the Git LFS SHA-256 object identifiers published by the official sample repository.

| Official sample file | Bytes | SHA-256 |
|---|---:|---|
| `abnormal/case1/log-parquet.tar.gz` | 2,865,219 | `d35971b1666249b76d11cf7e287919212c60d4cdb1b80d9e73a5c0de95d789fc` |
| `abnormal/case1/metric-parquet.tar.gz` | 2,815,368 | `e4efb15848ffc0b1cddd36d9634d4e6f9766b013f220dd4c7cb545ea88dfb31c` |
| `abnormal/case1/trace-parquet.tar.gz` | 7,412,405 | `2b6b005ca7fbc643e65c88bd55cbdb3066743ef280cddfaff56f826cabfb6d51` |
| `abnormal/case2/log-parquet.tar.gz` | 9,617,394 | `89915293e832482aec90727942f4b93eb4e58e2111749ebd34196e7543994f1b` |
| `abnormal/case2/metric-parquet.tar.gz` | 2,797,864 | `33c133b5ee523dfff54df70ad5377159145d4ad2dcea5bd4147fcedfda65d983` |
| `abnormal/case2/trace-parquet.tar.gz` | 18,455,886 | `92d8dab0b81cee738627712ff1eec4eab3849227375f68b040dd3f88c2682f1a` |
| `normal/metric-parquet.tar.gz` | 2,972,557 | `36edf356c2958564f83af735cedfc60c66bd752cb2c6eb0de7c9537628ecf6c2` |

The compressed telemetry subset is **46,936,693 bytes**. It expands to **243 Parquet files and 62,264,719 bytes**. The full-release `groundtruth.jsonl` (400 records), `input.json` (400 records), README, checksum manifest, and sample `input.json` (2 records) were also inspected. Total downloaded material was approximately 47.4 MB.

## Selection method

The two official abnormal cases were selected because they provide real metrics, logs, traces, incident windows, and labels in a small download. Normal metrics were selected because the abnormal sample archives omit TiDB-specific metric files, while the normal archive includes TiDB, TiKV, and PD schemas. Normal logs and traces were not downloaded because they total approximately 618 MB and do not add a missing explicit TiDB pod-to-node field.

The full daily archives were not downloaded. Each is 400–770 MB, crossing the brief's requirement to stop before a very large transfer. Raw and extracted dataset paths are ignored by Git.
