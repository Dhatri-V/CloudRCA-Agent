#!/usr/bin/env python3
"""Run the optional live JCode/Qwen/GLM compatibility smoke."""

from argparse import ArgumentParser
import json
from pathlib import Path

from cloudrca_compatibility.jcode_smoke import ProviderSpec, SmokeError, run_smoke


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--jcode", required=True, type=Path)
    parser.add_argument("--qwen-base-url", required=True)
    parser.add_argument("--qwen-model", required=True)
    parser.add_argument("--qwen-api-key-env")
    parser.add_argument("--glm-base-url", required=True)
    parser.add_argument("--glm-model", default="glm-5.3")
    parser.add_argument("--glm-api-key-env")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    try:
        result = run_smoke(
            args.jcode,
            ProviderSpec("cloudrca-qwen", args.qwen_base_url, args.qwen_model, args.qwen_api_key_env),
            ProviderSpec("cloudrca-glm", args.glm_base_url, args.glm_model, args.glm_api_key_env, 1_000_000),
            args.timeout,
        )
    except SmokeError as error:
        print(json.dumps({"status": "FAILED", "error": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

