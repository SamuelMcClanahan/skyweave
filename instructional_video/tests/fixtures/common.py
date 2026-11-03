"""A per-video drawing library, as the Design agent writes it."""

from ivideo.synced import *

CAM_POS = LEFT * 4 + DOWN * 1.5


def camera_icon(color=ACCENT):
    body = RoundedRectangle(width=0.7, height=0.45, corner_radius=0.08, color=color)
    lens = Triangle(color=color).scale(0.15).rotate(-PI / 2).next_to(body, RIGHT, buff=0)
    return VGroup(body, lens)


class Gallery(Scene):
    def construct(self):
        self.camera.background_color = BG
        self.add(camera_icon().move_to(CAM_POS), label("camera").next_to(CAM_POS, DOWN))
