"""Global path planner for walk_to.

The planner is intentionally small: it builds an 8-neighbor grid inside the
field, marks existing circular obstacles as blocked, and runs A*.  The caller
can then follow the first waypoint while keeping the existing velocity control.
"""

from __future__ import annotations

import heapq
import math

from ..framework.types import Context
from ..param import (
    GLOBAL_FIELD_MARGIN_M,
    GLOBAL_GRID_RESOLUTION_M,
    GLOBAL_OBSTACLE_MARGIN_M,
)
from .obstacles import Obstacle


def plan_global_path(
    context: Context,
    start: tuple[float, float],
    target: tuple[float, float],
    obstacles: list[Obstacle],
) -> list[tuple[float, float]] | None:
    """Return an A* path from start to target, or None if no path is found."""
    min_x, max_x, min_y, max_y = _bounds(context)
    sx, sy = _clamp_point(start[0], start[1], min_x, max_x, min_y, max_y)
    tx, ty = _clamp_point(target[0], target[1], min_x, max_x, min_y, max_y)

    start_idx = _to_idx(sx, sy, min_x, min_y)
    goal_idx = _to_idx(tx, ty, min_x, min_y)
    if start_idx == goal_idx:
        return [(tx, ty)]

    start_point = (sx, sy)
    start_overlaps = _containing_obstacles(sx, sy, obstacles)
    goal_grid_point = _to_xy(goal_idx, min_x, min_y)
    # Preserve the existing blocked-goal endpoint policy while checking the
    # final edge against every obstacle that does not contain the goal.
    goal_overlaps = _merge_obstacles(
        _containing_obstacles(tx, ty, obstacles),
        _containing_obstacles(
            goal_grid_point[0], goal_grid_point[1], obstacles,
        ),
    )

    open_heap: list[tuple[float, int, tuple[int, int]]] = []
    counter = 0
    heapq.heappush(open_heap, (0.0, counter, start_idx))
    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    g_score: dict[tuple[int, int], float] = {start_idx: 0.0}
    closed: set[tuple[int, int]] = set()

    while open_heap:
        _, _, current = heapq.heappop(open_heap)
        if current in closed:
            continue
        if current == goal_idx:
            return _reconstruct(came_from, current, min_x, min_y, (tx, ty))
        closed.add(current)

        for neighbor, step_cost in _neighbors(current):
            x, y = _to_xy(neighbor, min_x, min_y)
            if not (min_x <= x <= max_x and min_y <= y <= max_y):
                continue
            if (
                neighbor != start_idx
                and neighbor != goal_idx
                and _blocked(x, y, obstacles)
            ):
                continue

            current_point = (
                start_point
                if current == start_idx
                else _to_xy(current, min_x, min_y)
            )
            ignored_obstacles = goal_overlaps if neighbor == goal_idx else []
            if current == start_idx and start_overlaps:
                # The first edge may leave an existing overlap, but it must
                # move outward from every obstacle that contains the start.
                if not _moves_outward_from_overlaps(
                    current_point, (x, y), start_overlaps,
                ):
                    continue
                ignored_obstacles = _merge_obstacles(
                    ignored_obstacles, start_overlaps,
                )
            if (
                neighbor != goal_idx
                and _diagonal_corner_blocked(
                    current,
                    neighbor,
                    min_x,
                    min_y,
                    obstacles,
                    ignored_obstacles,
                )
            ):
                continue
            if _edge_blocked(
                current_point,
                (x, y),
                obstacles,
                ignored_obstacles,
            ):
                continue
            tentative = g_score[current] + step_cost
            if tentative >= g_score.get(neighbor, math.inf):
                continue
            came_from[neighbor] = current
            g_score[neighbor] = tentative
            counter += 1
            heapq.heappush(
                open_heap,
                (tentative + _heuristic(neighbor, goal_idx), counter, neighbor),
            )

    return None


def _bounds(context: Context) -> tuple[float, float, float, float]:
    half_l = context.field.length / 2.0 - GLOBAL_FIELD_MARGIN_M
    half_w = context.field.width / 2.0 - GLOBAL_FIELD_MARGIN_M
    return -half_l, half_l, -half_w, half_w


def _clamp_point(
    x: float,
    y: float,
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
) -> tuple[float, float]:
    return (max(min_x, min(max_x, x)), max(min_y, min(max_y, y)))


def _to_idx(x: float, y: float, min_x: float, min_y: float) -> tuple[int, int]:
    return (
        int(round((x - min_x) / GLOBAL_GRID_RESOLUTION_M)),
        int(round((y - min_y) / GLOBAL_GRID_RESOLUTION_M)),
    )


def _to_xy(idx: tuple[int, int], min_x: float, min_y: float) -> tuple[float, float]:
    return (
        min_x + idx[0] * GLOBAL_GRID_RESOLUTION_M,
        min_y + idx[1] * GLOBAL_GRID_RESOLUTION_M,
    )


def _neighbors(idx: tuple[int, int]) -> list[tuple[tuple[int, int], float]]:
    x, y = idx
    out: list[tuple[tuple[int, int], float]] = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            cost = math.sqrt(2.0) if dx != 0 and dy != 0 else 1.0
            out.append(((x + dx, y + dy), cost))
    return out


def _blocked(x: float, y: float, obstacles: list[Obstacle]) -> bool:
    return _blocked_except(x, y, obstacles, [])


def _blocked_except(
    x: float,
    y: float,
    obstacles: list[Obstacle],
    ignored_obstacles: list[Obstacle],
) -> bool:
    for obstacle in obstacles:
        if obstacle in ignored_obstacles:
            continue
        obstacle_distance = math.hypot(x - obstacle.x, y - obstacle.y)
        inflated_radius = obstacle.radius + GLOBAL_OBSTACLE_MARGIN_M
        if obstacle_distance <= inflated_radius:
            return True
    return False


def _containing_obstacles(
    x: float,
    y: float,
    obstacles: list[Obstacle],
) -> list[Obstacle]:
    return [
        obstacle
        for obstacle in obstacles
        if math.hypot(x - obstacle.x, y - obstacle.y)
        <= obstacle.radius + GLOBAL_OBSTACLE_MARGIN_M
    ]


def _merge_obstacles(
    first: list[Obstacle],
    second: list[Obstacle],
) -> list[Obstacle]:
    merged = list(first)
    for obstacle in second:
        if obstacle not in merged:
            merged.append(obstacle)
    return merged


def _diagonal_corner_blocked(
    current: tuple[int, int],
    neighbor: tuple[int, int],
    min_x: float,
    min_y: float,
    obstacles: list[Obstacle],
    ignored_obstacles: list[Obstacle],
) -> bool:
    step_x = neighbor[0] - current[0]
    step_y = neighbor[1] - current[1]
    if step_x == 0 or step_y == 0:
        return False

    horizontal = _to_xy(
        (current[0] + step_x, current[1]), min_x, min_y,
    )
    vertical = _to_xy(
        (current[0], current[1] + step_y), min_x, min_y,
    )
    return (
        _blocked_except(
            horizontal[0], horizontal[1], obstacles, ignored_obstacles,
        )
        or _blocked_except(
            vertical[0], vertical[1], obstacles, ignored_obstacles,
        )
    )


def _moves_outward_from_overlaps(
    start: tuple[float, float],
    end: tuple[float, float],
    overlapping_obstacles: list[Obstacle],
) -> bool:
    movement_x = end[0] - start[0]
    movement_y = end[1] - start[1]
    for obstacle in overlapping_obstacles:
        start_offset_x = start[0] - obstacle.x
        start_offset_y = start[1] - obstacle.y
        start_distance = math.hypot(start_offset_x, start_offset_y)
        end_distance = math.hypot(end[0] - obstacle.x, end[1] - obstacle.y)
        if end_distance <= start_distance + 1e-9:
            return False
        if start_distance > 1e-9:
            outward_progress = (
                start_offset_x * movement_x
                + start_offset_y * movement_y
            )
            if outward_progress <= 0.0:
                return False
    return True


def _edge_blocked(
    start: tuple[float, float],
    end: tuple[float, float],
    obstacles: list[Obstacle],
    ignored_obstacles: list[Obstacle],
) -> bool:
    for obstacle in obstacles:
        if obstacle in ignored_obstacles:
            continue
        edge_clearance = _point_to_segment_distance(
            (obstacle.x, obstacle.y), start, end,
        )
        if edge_clearance <= obstacle.radius + GLOBAL_OBSTACLE_MARGIN_M:
            return True
    return False


def _point_to_segment_distance(
    point: tuple[float, float],
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
) -> float:
    segment_x = segment_end[0] - segment_start[0]
    segment_y = segment_end[1] - segment_start[1]
    segment_length_squared = segment_x * segment_x + segment_y * segment_y
    if segment_length_squared <= 1e-12:
        return math.hypot(
            point[0] - segment_start[0],
            point[1] - segment_start[1],
        )

    projection = (
        (point[0] - segment_start[0]) * segment_x
        + (point[1] - segment_start[1]) * segment_y
    ) / segment_length_squared
    projection = max(0.0, min(1.0, projection))
    nearest_x = segment_start[0] + segment_x * projection
    nearest_y = segment_start[1] + segment_y * projection
    return math.hypot(point[0] - nearest_x, point[1] - nearest_y)


def _heuristic(a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _reconstruct(
    came_from: dict[tuple[int, int], tuple[int, int]],
    current: tuple[int, int],
    min_x: float,
    min_y: float,
    target: tuple[float, float],
) -> list[tuple[float, float]]:
    indices = [current]
    while current in came_from:
        current = came_from[current]
        indices.append(current)
    indices.reverse()
    path = [_to_xy(idx, min_x, min_y) for idx in indices]
    path[-1] = target
    return _smooth_path(path)


def _smooth_path(path: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(path) <= 2:
        return path
    smoothed = [path[0]]
    prev_dx = 0
    prev_dy = 0
    for i in range(1, len(path)):
        dx = _sign(path[i][0] - path[i - 1][0])
        dy = _sign(path[i][1] - path[i - 1][1])
        if i > 1 and (dx, dy) != (prev_dx, prev_dy):
            smoothed.append(path[i - 1])
        prev_dx, prev_dy = dx, dy
    smoothed.append(path[-1])
    return smoothed


def _sign(v: float) -> int:
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0
