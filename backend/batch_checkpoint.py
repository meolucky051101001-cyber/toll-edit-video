"""JSON checkpoints for legacy batch stages. Never deserialize executable objects."""
import hashlib
import json
from pathlib import Path
from datetime import timedelta
import srt
from pipeline_v2.atomic_io import atomic_write_json


def fingerprint(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Checkpoints:
    def __init__(self, directory, source):
        self.directory = Path(directory)
        self.identity = fingerprint(source)
        # Changing pipeline code invalidates checkpoints automatically.
        for name in ("batch_processor.py", "ai/transcription.py", "ai/translation.py",
                     "video_utils.py"):
            self.identity += fingerprint(Path(__file__).parent / name)

    async def subtitles(self, name, source, produce, output):
        key = hashlib.sha256((self.identity + fingerprint(source)).encode()).hexdigest()
        checkpoint = self.directory / (name + ".checkpoint.json")
        try:
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            if value["key"] == key and value["output_hash"] == fingerprint(output):
                return [srt.Subtitle(index=item["index"],
                            start=timedelta(seconds=item["start"]),
                            end=timedelta(seconds=item["end"]), content=item["content"])
                        for item in value["segments"]]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        segments = await produce()
        if not segments:
            raise RuntimeError("Stage returned no subtitles: " + name)
        # Save exact stage result atomically before publishing its checkpoint.
        from pipeline_v2.atomic_io import atomic_write_text
        atomic_write_text(output, srt.compose(segments, reindex=False))
        atomic_write_json(checkpoint, {
            "key": key, "output_hash": fingerprint(output),
            "segments": [{"index": x.index, "start": x.start.total_seconds(),
                          "end": x.end.total_seconds(), "content": x.content}
                         for x in segments],
        })
        return segments
