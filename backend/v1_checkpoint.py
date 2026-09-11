"""Content-validated sidecars: reuse only complete, unchanged stage artifacts."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def fingerprint(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class Checkpoint:
    def __init__(self, source, outputs, settings):
        self.outputs = [Path(p) for p in outputs]
        self.path = Path(str(self.outputs[0]) + '.v1-cache.json')
        self.key = None
        try:
            self.key = [fingerprint(source), settings]
        except OSError:
            pass

    def artifacts(self):
        if any(not p.is_file() or p.stat().st_size == 0 for p in self.outputs):
            raise ValueError('Missing or empty artifact')
        return [[str(p.resolve()), fingerprint(p)] for p in self.outputs]

    def hit(self):
        if self.key is None:
            return False
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data['key'] == self.key and data['outputs'] == self.artifacts()
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def save(self):
        if self.key is None:
            return
        temporary = None
        try:
            data = {'key': self.key, 'outputs': self.artifacts()}
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump(data, stream)
            os.replace(temporary, self.path)
        except (OSError, ValueError):
            pass
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

