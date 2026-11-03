from ivideo.synced import *


class Main(SyncedScene):
    def construct(self):
        t = title(self.scene_title)
        self.play(FadeIn(t), run_time=0.4)
        self.at_line(0)
        cam = Square(0.6, color=ACCENT).shift(LEFT * 4)
        self.play(Create(cam), run_time=self.fit(0, 0.4))
        for i in range(1, len(self.lines)):
            self.at_line(i)
            self.play(cam.animate.shift(RIGHT * 1.5), run_time=self.fit(i, 0.5))
        self.finish()
