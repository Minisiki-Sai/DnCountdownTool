"""Reuse offline replay decoding only when its inputs match Hit decoding.

Never installed on a live source. Local identity hints invalidate reuse when
consulted successfully; the existing independent decode remains the fallback.
"""
import os
import pickle
import uuid
import zlib
from pathlib import Path


class _ObservedHints(dict):
    used = False

    def get(self, key, default=None):
        value = super().get(key, default)
        if value:
            self.used = True
        return value


class ReplayHitEvidenceRecorder:
    def __init__(self, source, path):
        from .score_hit_evidence import addon_root, decode_signature, root
        addon_root()
        from skill_hit_tool.projectile_completion import ProjectileEvidenceCollector
        self.source = source
        self.key = decode_signature(path)
        self.destination = root() / 'decoded' / (self.key + '.bin')
        self.hints = _ObservedHints(source._decoder.known_players)
        self.original_hints = source._decoder.known_players
        source._decoder.known_players = self.hints
        try:
            self.collector = ProjectileEvidenceCollector(source)
        except Exception:
            source._decoder.known_players = self.original_hints
            raise
        self.events = []

    def append(self, batch):
        self.events.extend(batch)

    def finish(self, success):
        from skill_hit_tool.hit_ledger import entity_births_from_source
        from .score_hit_evidence import evidence_lock
        try:
            proofs = self.collector.finish()
            if not success or self.hints.used:
                return False
            bundle = dict(decoded=self.events,
                          births=entity_births_from_source(self.source),
                          end=int(self.source.capture_end_timestamp_ms or 0),
                          proofs=proofs,
                          registrations=self.collector.finish_registrations(),
                          life=self.collector.hammer.export())
            with evidence_lock('decode:' + self.key):
                self.destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.destination.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_bytes(zlib.compress(pickle.dumps(bundle, protocol=5), 1))
                    os.replace(temporary, self.destination)
                finally:
                    temporary.unlink(missing_ok=True)
            return True
        finally:
            self.source._decoder.known_players = self.original_hints
            self.events.clear()


def start_recorder(source, path, data_root=None):
    if os.environ.get('DN_SCORE_EXACT_DECODE_REUSE', '1') != '1':
        return None
    if data_root is not None and Path(data_root).resolve() != (Path(__file__).resolve().parents[1] / 'data').resolve():
        return None
    from .score_hit_evidence import decode_signature, root
    if (root() / 'decoded' / (decode_signature(path) + '.bin')).is_file():
        return None
    return ReplayHitEvidenceRecorder(source, path)
