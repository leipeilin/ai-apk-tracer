import json
import sys
from pathlib import Path
from shared.detector import execute

payload = json.loads(Path(sys.argv[1]).read_text("utf-8"))
print(json.dumps(execute(Path(__file__).parent.name, payload), ensure_ascii=False))
