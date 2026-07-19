# Static findings used by this task

- `src/player.py:476-495`: local VFH is currently used only when A* returns `None`; a successful path with very low immediate heading clearance is only slowed afterward.
- `src/player.py:599-616`: path lookahead compares each segment independently instead of consuming one cumulative path-distance budget.
- `src/utils/path_planner.py:60-70`: neighbor acceptance currently checks only the neighbor node, not the traversed edge.
- `src/utils/path_planner.py:119-130`: all eight neighbors are generated without diagonal corner constraints.
- `src/utils/path_planner.py:134-138`: A* obstacle inflation uses raw radius plus `GLOBAL_OBSTACLE_MARGIN_M`.
- `src/player.py:586-597`: the speed layer can reduce an A*-legal low-clearance heading to a very small velocity.
- The implementation must preserve an outward first step when the start is already inside an inflated obstacle; otherwise adding continuous edge checks would create a new deterministic deadlock.
