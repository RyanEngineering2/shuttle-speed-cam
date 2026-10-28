"""Badminton court geometry, used to calibrate a camera from the court itself.

World frame: origin on the floor at the centre of the court. X runs across the
court (to the camera's right), Y along it (away from the camera, net at Y = 0), Z
up. Metres. "Near" is the camera's half; "left" and "right" are as the camera sees
them.

Landmarks are named "<line across>/<line along>" for floor points where two lines
cross, for example "near_short/centre" or "far_base/right_singles", plus the net
posts: "post_left_top", "post_left_base", "post_right_top", "post_right_base", and
"net_top_centre".
"""

from __future__ import annotations

# Lines running across the court, by their distance along it (BWF dimensions).
ACROSS = {
    "near_base": -6.70,
    "near_long": -5.94,  # doubles long service line
    "near_short": -1.98,  # short service line
    "far_short": 1.98,
    "far_long": 5.94,
    "far_base": 6.70,
}
# Lines running along the court, by their offset across it.
ALONG = {
    "left_doubles": -3.05,
    "left_singles": -2.59,
    "centre": 0.0,
    "right_singles": 2.59,
    "right_doubles": 3.05,
}
NET_POST_HEIGHT = 1.55
NET_CENTRE_HEIGHT = 1.524
HALF_WIDTH = 3.05
HALF_LENGTH = 6.70


def landmarks() -> dict[str, tuple[float, float, float]]:
    """Every named landmark and its world position."""
    points: dict[str, tuple[float, float, float]] = {}
    for across, y in ACROSS.items():
        for along, x in ALONG.items():
            points[f"{across}/{along}"] = (x, y, 0.0)
    for side, x in (("left", -HALF_WIDTH), ("right", HALF_WIDTH)):
        points[f"post_{side}_base"] = (x, 0.0, 0.0)
        points[f"post_{side}_top"] = (x, 0.0, NET_POST_HEIGHT)
    points["net_top_centre"] = (0.0, 0.0, NET_CENTRE_HEIGHT)
    return points


def segments() -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    """Court lines, net posts and the net tape, for drawing overlays."""
    segs = []
    for y in ACROSS.values():
        segs.append(((-HALF_WIDTH, y, 0.0), (HALF_WIDTH, y, 0.0)))
    for name, x in ALONG.items():
        if name == "centre":
            segs.append(((0.0, -HALF_LENGTH, 0.0), (0.0, ACROSS["near_short"], 0.0)))
            segs.append(((0.0, ACROSS["far_short"], 0.0), (0.0, HALF_LENGTH, 0.0)))
        else:
            segs.append(((x, -HALF_LENGTH, 0.0), (x, HALF_LENGTH, 0.0)))
    for x in (-HALF_WIDTH, HALF_WIDTH):
        segs.append(((x, 0.0, 0.0), (x, 0.0, NET_POST_HEIGHT)))
    segs.append(((-HALF_WIDTH, 0.0, NET_POST_HEIGHT), (HALF_WIDTH, 0.0, NET_POST_HEIGHT)))
    return segs
