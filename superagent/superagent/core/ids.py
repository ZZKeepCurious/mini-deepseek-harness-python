import hashlib
import json


def key(*values: str) -> str:
    return hashlib.sha256(json.dumps(list(values)).encode()).hexdigest()[:32]
