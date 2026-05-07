"""JSON Reporter — dumps full analysis result as structured JSON."""

import json
import os
from datetime import datetime


class JsonReporter:
    def __init__(self, output_dir: str = "reports"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def write(self, analysis: dict) -> str:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(self.output_dir, f"analysis_{ts}.json")
        with open(path, "w") as f:
            json.dump(analysis, f, indent=2, default=str)
        return path
