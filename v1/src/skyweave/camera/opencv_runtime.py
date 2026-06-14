from __future__ import annotations

import os


def configure_opencv_runtime(cv2_module) -> None:
    threads_text = os.environ.get("SKYWEAVE_OPENCV_THREADS")
    if threads_text is None:
        return
    try:
        threads = int(threads_text)
    except ValueError:
        threads = 1
    if threads > 0:
        cv2_module.setNumThreads(threads)
