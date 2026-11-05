"""Ramer-Douglas-Peucker stroke simplification (Architecture.md section 9).

A freehand stroke at mouse rate carries many nearly collinear points. Removing them before commit
keeps the single ``create path`` op small, which is what the performance target PF-02 measures.
"""

from __future__ import annotations

Flat = list[float]


def _segment_distance_sq(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2


def simplify(points: Flat, tolerance: float = 1.0) -> Flat:
    """Simplify a flat [x0, y0, x1, y1, ...] list. Iterative, so a long stroke cannot overflow."""
    count = len(points) // 2
    if count <= 2:
        return list(points)
    keep = [False] * count
    keep[0] = keep[-1] = True
    limit = tolerance * tolerance
    stack = [(0, count - 1)]
    while stack:
        first, last = stack.pop()
        ax, ay = points[first * 2], points[first * 2 + 1]
        bx, by = points[last * 2], points[last * 2 + 1]
        worst = -1.0
        worst_i = -1
        for i in range(first + 1, last):
            dist = _segment_distance_sq(points[i * 2], points[i * 2 + 1], ax, ay, bx, by)
            if dist > worst:
                worst, worst_i = dist, i
        if worst > limit:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))
    out: Flat = []
    for i, kept in enumerate(keep):
        if kept:
            out += [points[i * 2], points[i * 2 + 1]]
    return out
