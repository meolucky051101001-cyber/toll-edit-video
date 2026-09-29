"""Decode large sources once for OCR; final render always uses the original."""
import functools
import logging
import os
import tempfile
from pathlib import Path
import subprocess
try:
    from .batch_control import run
except ImportError:
    from batch_control import run

def ocr_proxy(fn):
    @functools.wraps(fn)
    def wrapped(video_path, *args, **kwargs):
        import cv2
        cap=cv2.VideoCapture(str(video_path))
        try:
            width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        finally:
            cap.release()
        if width <= 1440 or height <= 0:
            return fn(video_path,*args,**kwargs)
        with tempfile.TemporaryDirectory(prefix="v1-ocr-proxy-") as directory:
            proxy=Path(directory)/"ocr.mp4"
            cmd_nvenc=["ffmpeg","-v","error","-y","-i",str(video_path),"-an",
                       "-vf","scale=720:-2","-c:v","h264_nvenc","-preset","p1",
                       "-g","12","-fps_mode","passthrough",str(proxy)]
            cmd_cpu=["ffmpeg","-v","error","-y","-i",str(video_path),"-an",
                     "-vf","scale=720:-2","-c:v","libx264","-preset","ultrafast",
                     "-crf","18","-g","12","-fps_mode","passthrough",str(proxy)]
            proxy_ok = False
            for cmd in [cmd_nvenc, cmd_cpu]:
                try:
                    run(cmd,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),timeout=180)
                    proxy_ok = True
                    break
                except (OSError, subprocess.SubprocessError) as exc:
                    logging.getLogger(__name__).warning("OCR proxy attempt failed with %s: %s", type(exc).__name__, exc)
                    continue
            if not proxy_ok:
                logging.getLogger(__name__).warning("OCR proxy completely unavailable; using original")
                return fn(video_path,*args,**kwargs)
            logging.getLogger(__name__).info("OCR proxy enabled source=%dx%d; output resolution unchanged",width,height)
            blocks,_,_,y=fn(str(proxy),*args,**kwargs)
            return blocks,width,height,y
    return wrapped
