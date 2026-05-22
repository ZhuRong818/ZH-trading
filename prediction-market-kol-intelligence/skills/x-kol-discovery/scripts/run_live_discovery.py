#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path


if __name__ == "__main__":
    script_path = Path(__file__).with_name("run_live_smoke_test.py")
    spec = importlib.util.spec_from_file_location("run_live_smoke_test", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
