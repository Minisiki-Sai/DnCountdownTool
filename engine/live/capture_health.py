"""Versioned capture provenance; metadata never enters the combat timeline."""
import json

ADAPTER_INDEX = 250
MAGIC = b'DNCH1\x00'
CAPTURE_IMPLEMENTATION = 'raw-first-no-size-drop-tls-proof-v1'


def encode_health(health):
    return MAGIC + json.dumps(health, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def decode_health(frame):
    if not frame.startswith(MAGIC) or len(frame) > 65536:
        raise ValueError('抓包完整性元数据损坏')
    health = json.loads(frame[len(MAGIC):])
    if not isinstance(health, dict) or health.get('version') != 1:
        raise ValueError('未知的抓包完整性元数据')
    return health
