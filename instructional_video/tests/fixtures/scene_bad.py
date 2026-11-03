from ivideo.synced import *


class Main(SyncedScene):
    def construct(self):
        self.at_line(0)
        a = text("Overlapping label one")
        b = text("Overlapping label two").move_to(a.get_center() + 0.1 * DOWN)
        self.play(FadeIn(a), FadeIn(b), run_time=0.5)
        far = Circle(radius=0.5).move_to(RIGHT * 7.5)
        self.play(Create(far), run_time=0.5)
        tiny = Text("tiny", font_size=10).to_edge(DOWN)
        self.add(tiny)
        self.at_line(1)
        self.wait(0.2)
        self.finish()
