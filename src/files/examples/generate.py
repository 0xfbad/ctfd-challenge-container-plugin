#!/usr/bin/env python3

import base64
import hashlib
import os
import random
from pathlib import Path


def main() -> None:
    output = Path(os.environ["OUTPUT_DIR"])
    rng = random.Random(hashlib.sha256(f"{os.environ['FRESHNESS_SEED']}:numbers-v1".encode()).digest())
    numbers = [rng.randrange(1000) for _ in range(10)]
    encoded = base64.b64encode(os.environ["FLAG"].encode("utf-8")).decode("ascii")
    (output / "message.txt").write_text(
        f"Numbers: {numbers}\nDecode this base64 message:\n{encoded}\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
