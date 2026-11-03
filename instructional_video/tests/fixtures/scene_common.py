from common import *

from ivideo.synced import *


class Main(SyncedScene):
    def construct(self):
        self.add(camera_icon().move_to(CAM_POS))  # carried over: on screen from frame 0
        self.play(FadeIn(title(self.scene_title)), run_time=0.4)
        self.finish()
