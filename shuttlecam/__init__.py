"""shuttlecam: measure badminton smash speed with a high-frame-rate camera.

Pipeline: camera frames -> motion detector -> tracker (drag-aware Kalman filter
and RTS smoother) -> peak shuttle speed -> P10 LED matrix.
"""

__version__ = "2.0.0"
