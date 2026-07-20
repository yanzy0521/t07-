"""SoccerSim 策略入口 —— 比赛策略主逻辑都在这里,改打法就改这个文件。

结构(由浅入深):
- main.py(本文件):比赛策略。play() 按 Phase 状态机分派到 _act_*;各 _act_* 选出
  offensive striker 或 defensive presser 并直接调 player 动作。
- player.py:Player 控制 handle + 高层动作(attack / take_kickoff /
  move_to_position / walk_to);想加拐棍/技术动作直接改它。
- utils/:走位/几何/避障工具(opponent_goal / dist / angle_to ...)。
- framework/:平台管线,用户不改。

改打法主要改本文件:Phase 状态机、各 _act_* 行为、站位公式。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum

from booster_agent_framework import AgentBase

from .framework.agent import SoccerAgentMixin
from .framework.types import KICKING_TEAM_NONE, Context, GameState, SetPlay
from .param import *
from .player import Player
from .utils import (
    angle_to,
    clamp,
    dist,
    normalize_angle,
    opponent_goal,
    own_goal,
    own_goal_area_center,
)


_log = logging.getLogger(__name__)


# ======================================================================
# Phase 状态机 —— 比赛阶段分类
# ======================================================================


class Phase(Enum):
    """比赛阶段。顶层状态机,决定当前是正常拼抢/开球/定位球/准备/停止。"""
    NORMAL = "normal"              # PLAYING 正常拼抢
    OUR_KICKOFF = "our_kickoff"    # 我方开球(SET+PLAYING 初期,take_kickoff)
    OPP_KICKOFF = "opp_kickoff"    # 对方开球(避让)
    OUR_SET_PLAY = "our_set_play"  # 我方定位球(任意球/角球/球门球)
    OPP_SET_PLAY = "opp_set_play"  # 对方定位球(避让)
    READY = "ready"                # READY 走位
    STOPPED = "stopped"            # SET(非开球重开) / INITIAL / FINISHED / stopped


class OpenPlayMode(Enum):
    """普通比赛的稳定战术模式。"""

    ATTACKING = "attacking"
    DEFENDING = "defending"
    CONTESTED = "contested"


class GoalkeeperMode(Enum):
    """普通 live play 中的守门员战术状态。"""

    HOLD = "hold"
    TRACK = "track"
    BLOCK = "block"
    CHALLENGE = "challenge"
    CLEAR = "clear"
    RETURN = "return"


class ThrowInRegion(Enum):
    """按场地长度比例划分的界外球区域。"""

    BACKFIELD = "backfield"
    MIDFIELD = "midfield"
    FRONTFIELD = "frontfield"


class ThrowInStage(Enum):
    """我方界外球固定战术执行阶段。"""

    POSITIONING = "positioning"
    KICKING = "kicking"
    PASS_IN_FLIGHT = "pass_in_flight"
    FOLLOW_UP = "follow_up"
    COMPLETE = "complete"
    ABORTED = "aborted"


class OpenPlayAvailability(Enum):
    """普通比赛角色分配可使用的机器人数量。"""

    FULL_THREE = "3_available"
    DEGRADED_TWO = "2_available"
    DEGRADED_ONE = "1_available"
    UNAVAILABLE = "0_available"


@dataclass(frozen=True)
class OpenPlayRoleAssignment:
    """一帧普通比赛中彼此独立的进攻与防守职责分配结果。"""

    goalkeeper_id: int | None
    offensive_striker_id: int | None
    front_partner_id: int | None
    defensive_presser_id: int | None
    defensive_protector_id: int | None
    available_player_ids: tuple[int, ...]
    availability: OpenPlayAvailability


@dataclass(frozen=True)
class OpenPlayModeEstimate:
    """一帧普通比赛的可解释模式估计结果。"""

    candidate_mode: OpenPlayMode
    reason: str
    our_nearest_ball_distance: float | None
    opponent_nearest_ball_distance: float | None
    distance_advantage: float | None
    ball_in_own_danger_area: bool


@dataclass(frozen=True)
class GoalkeeperThreatEstimate:
    """守门员使用的可解释射门威胁估计。"""

    fast_goal_threat: bool
    position_threat: bool
    projected_goal_y: float | None
    reason: str


@dataclass(frozen=True)
class ThrowInRoleAssignment:
    """一次界外球上下文中只选择一次的固定职责。"""

    goalkeeper_id: int | None
    kicker_id: int | None
    receiver_id: int | None
    available_player_ids_at_entry: tuple[int, ...]


@dataclass
class ThrowInTacticState:
    """我方界外球固定战术的锁定几何和执行进度。"""

    stage: ThrowInStage
    region: ThrowInRegion
    started_at: float
    stage_started_at: float
    origin: tuple[float, float]
    infield_y_direction: float
    long_clearance: bool
    pass_target: tuple[float, float]
    receiver_target: tuple[float, float] | None
    kicker_stage_target: tuple[float, float]
    pass_direction: tuple[float, float]
    pass_power: float
    kick_command_started: bool = False
    kicking_start_ball_position: tuple[float, float] | None = None
    follow_up_start_ball_position: tuple[float, float] | None = None
    follow_up_target: tuple[float, float] | None = None
    follow_up_action_started: bool = False
    terminal_reason: str | None = None


_THROW_IN_TRANSIENT_INIT_FAILURES = frozenset(
    {
        "ball_not_near_touchline",
        "no_valid_pass_target",
        "no_valid_kicker_stage",
        "ball_unavailable",
    },
)


@dataclass
class InactivityPreventionState:
    """单个机器人基于实际 pose 的 M-05 静止窗口和 nudge 状态。"""

    last_sample_position: tuple[float, float] | None = None
    stationary_anchor_position: tuple[float, float] | None = None
    stationary_window_started_at: float | None = None
    last_confirmed_movement_at: float | None = None
    nudge_active: bool = False
    nudge_target: tuple[float, float] | None = None
    nudge_start_position: tuple[float, float] | None = None
    nudge_started_at: float | None = None
    nudge_action: str | None = None
    nudge_phase: Phase | None = None
    cooldown_until: float = 0.0


def get_phase(context: Context) -> Phase:
    """根据裁判机状态判断当前比赛阶段。"""
    g = context.game
    if g is None:
        return Phase.STOPPED

    # 裁判明确停止时优先停车,包括 READY + stopped 等组合状态。
    if g.stopped:
        return Phase.STOPPED

    state = g.state

    # READY:走 ready 位
    if state == GameState.READY:
        return Phase.READY

    # PLAYING:正常拼抢 or 开球/定位球执行中
    if state == GameState.PLAYING:
        # 定位球:set_play != NONE,kicking_team 指示哪方
        if g.set_play != SetPlay.NONE and g.kicking_team != KICKING_TEAM_NONE:
            our_team = context.team_id
            if g.kicking_team == our_team:
                return Phase.OUR_SET_PLAY
            else:
                return Phase.OPP_SET_PLAY

        # 开球:secondary_time > 0(倒计时窗口),kicking_team 指示哪方
        if g.secondary_time > 0 and g.kicking_team != KICKING_TEAM_NONE:
            our_team = context.team_id
            if g.kicking_team == our_team:
                return Phase.OUR_KICKOFF
            else:
                return Phase.OPP_KICKOFF

        # 正常拼抢
        return Phase.NORMAL

    # SET / INITIAL / FINISHED:站定
    return Phase.STOPPED

def get_set_play_type(context: Context) -> SetPlay:
    """当前生效的定位球类型;无定位球(或无裁判机数据)时返回 ``SetPlay.NONE``。

    直接读裁判机的 ``set_play`` 字段,不区分是哪方主罚 —— 哪方由 :func:`get_phase`
    (OUR_SET_PLAY / OPP_SET_PLAY)判定。这里只回答"是什么类型的定位球"。

    共 7 种可能返回值(见 framework.types.SetPlay):
    - ``NONE``:无定位球(正常比赛/开球等)
    - ``DIRECT_FREE_KICK``:直接任意球(可直接射门得分)
    - ``INDIRECT_FREE_KICK``:间接任意球(须先触碰他人才能进球)
    - ``PENALTY_KICK``:点球
    - ``THROW_IN``:界外球(踢入)
    - ``GOAL_KICK``:球门球
    - ``CORNER_KICK``:角球
    """
    g = context.game
    if g is None:
        return SetPlay.NONE
    return g.set_play


# ======================================================================
# Agent 入口
# ======================================================================


class SoccerSimAgent(SoccerAgentMixin, AgentBase):
    """3v3 SoccerSim agent。"""

    player_class = Player

    def init_store(self, store) -> None:
        _log.info("init_store called")
        store.prev_phase = None       # 上一帧 phase,用于检测 phase 跳变(边沿)
        store.cur_phase = None
        store.kickoff_taker = None    # 锁定的开球主罚球员 id(每次进入开球时重选)
        store.offensive_striker_id = None
        store.defensive_presser_id = None
        store.last_ball_position = None
        store.last_ball_seen_at = None
        store.ball_visible_frames = 0
        store.ball_searcher = None
        store.ball_lost_since = None
        store.open_play_mode = None
        store.open_play_mode_entered_at = None
        store.open_play_last_switch_at = None
        store.open_play_mode_reason = "inactive"
        store.open_play_last_switch_reason = None
        store.open_play_our_ball_distance = None
        store.open_play_opponent_ball_distance = None
        store.open_play_distance_advantage = None
        store.goalkeeper_strategy_player_id = None
        store.goalkeeper_mode = None
        store.goalkeeper_mode_entered_at = None
        store.goalkeeper_challenge_started_at = None
        store.goalkeeper_threat_reason = "inactive"
        store.goalkeeper_previous_ball_position = None
        store.goalkeeper_previous_ball_sample_at = None
        store.goalkeeper_ball_velocity = None
        store.goalkeeper_ball_speed = None
        store.goalkeeper_target = None
        store.goalkeeper_clearance_target = None
        store.inactivity_prevention_states = {}
        store.inactivity_prevention_active_player_id = None
        store.player_availability = {}
        store.available_player_ids = ()
        store.default_goalkeeper_id = None
        store.temporary_goalkeeper_id = None
        store.current_goalkeeper_id = None
        store.available_field_player_ids = ()
        store.can_run_two_player_tactic = False
        store.latest_open_play_role_assignment = OpenPlayRoleAssignment(
            goalkeeper_id=None,
            offensive_striker_id=None,
            front_partner_id=None,
            defensive_presser_id=None,
            defensive_protector_id=None,
            available_player_ids=(),
            availability=OpenPlayAvailability.UNAVAILABLE,
        )
        # 后续固定战术可以维护自己的锁定职责,不与普通比赛分配状态混用。
        store.active_tactic = None
        store.locked_roles = None
        store.tactic_roles = None
        store.throw_in_state = None
        store.throw_in_context_consumed = False
        store.throw_in_last_outcome = None
        store.throw_in_last_reason = None

    @staticmethod
    def play(context: Context, players: list[Player], store) -> None:
        phase = get_phase(context)
        store.prev_phase = store.cur_phase
        store.cur_phase = phase

        readiness_actions_allowed = phase != Phase.STOPPED
        _synchronize_readiness_permissions(
            players,
            readiness_actions_allowed,
        )

        # 画可视化(每帧)
        _analyze_and_draw(context, players, store)

        # 当前 phase 以 label 画在场外。
        from .framework import debugdraw
        g = context.game
        game_state = g.state.value if g is not None else "none"
        set_play = g.set_play.value if g is not None else "none"
        secondary_time = g.secondary_time if g is not None else 0.0
        debugdraw.text(
            0.0, context.field.width / 2.0 + 0.2,
            f"phase={phase.value} state={game_state} set={set_play} secondary={secondary_time:.1f}",
            rgb=(1.0, 1.0, 0.0), ns="phase",
        )

        available_players = _collect_available_players(
            players,
            store,
            readiness_actions_allowed,
        )
        current_goalkeeper = _select_current_goalkeeper(
            context,
            players,
            available_players,
            phase,
            store,
        )
        if current_goalkeeper is None or store.prev_phase != phase:
            _reset_goalkeeper_strategy(store)

        _synchronize_throw_in_tactic_context(
            context,
            players,
            store,
        )

        # 按 phase 对整队分派一次(角色分配等全队计算只在 _act_* 里算一次)。
        if phase == Phase.NORMAL:
            _act_normal(context, available_players, current_goalkeeper, store)
        elif phase == Phase.OUR_KICKOFF:
            _clear_normal_sticky(store)
            _act_our_kickoff(
                context, available_players, current_goalkeeper, store,
            )
        elif phase == Phase.OPP_KICKOFF:
            _clear_normal_sticky(store)
            _act_opp_kickoff(context, available_players, current_goalkeeper)
        elif phase == Phase.OUR_SET_PLAY:
            _clear_normal_sticky(store)
            _act_our_set_play(
                context, available_players, current_goalkeeper, store,
            )
        elif phase == Phase.OPP_SET_PLAY:
            _clear_normal_sticky(store)
            _act_opp_set_play(
                context, available_players, current_goalkeeper, store,
            )
        elif phase == Phase.READY:
            _clear_normal_sticky(store)
            _act_ready(context, available_players, current_goalkeeper, store)
        elif phase == Phase.STOPPED:
            _clear_normal_sticky(store)
            for player in available_players:
                player.action = "stopped"
                player.stop()

        _apply_inactivity_prevention(
            context,
            players,
            available_players,
            current_goalkeeper,
            phase,
            store,
        )

        # 队员可视化统一在最后画一遍:覆盖所有球员(含判罚/未就绪/STOPPED),
        # 修复 SET 等状态下红球/标签消失的问题。
        for p in players:
            _draw_teammate_marker(p)


def _synchronize_readiness_permissions(
    players: list[Player],
    phase_allows_readiness: bool,
) -> None:
    """先于 readiness 检查同步许可,并在禁行状态取消旧异步意图。"""
    for player in players:
        player.set_readiness_actions_allowed(
            phase_allows_readiness and not player.is_penalized,
        )


def _collect_available_players(
    players: list[Player],
    store,
    readiness_actions_allowed: bool,
) -> list[Player]:
    """统一分类每帧可用性,并清除不可用球员的旧动作命令。"""
    player_availability: dict[int, str] = {}
    available_players: list[Player] = []

    for player in players:
        if player.is_penalized:
            availability = "penalized"
        elif readiness_actions_allowed:
            ready = player.ensure_ready()
            if player.is_fallen:
                availability = "fallen"
            elif not ready:
                availability = "switching_mode"
            elif player.pose is None:
                availability = "no_pose"
            else:
                availability = "available"
        elif player.is_fallen:
            availability = "fallen"
        elif player.mode != "walk":
            availability = "switching_mode"
        elif player.pose is None:
            availability = "no_pose"
        else:
            availability = "available"

        player_availability[player.id] = availability
        player.action = availability
        if availability == "available":
            available_players.append(player)
        else:
            player.stop()

    store.player_availability = player_availability
    store.available_player_ids = tuple(
        player.id for player in available_players
    )
    return available_players


def _get_inactivity_prevention_state(
    store,
    player_id: int,
) -> InactivityPreventionState:
    states = getattr(store, "inactivity_prevention_states", None)
    if states is None:
        states = {}
        store.inactivity_prevention_states = states
    state = states.get(player_id)
    if state is None:
        state = InactivityPreventionState()
        states[player_id] = state
    return state


def _clear_inactivity_nudge(state: InactivityPreventionState) -> None:
    state.nudge_active = False
    state.nudge_target = None
    state.nudge_start_position = None
    state.nudge_started_at = None
    state.nudge_action = None
    state.nudge_phase = None


def _clear_inactivity_window(
    state: InactivityPreventionState,
    current_position: tuple[float, float] | None,
) -> None:
    """条件失效时清除窗口,避免跨 STOP、罚下或观测丢失继承计时。"""
    state.last_sample_position = current_position
    state.stationary_anchor_position = None
    state.stationary_window_started_at = None
    state.last_confirmed_movement_at = None
    state.cooldown_until = 0.0
    _clear_inactivity_nudge(state)


def _confirm_inactivity_movement(
    state: InactivityPreventionState,
    current_position: tuple[float, float],
    now: float,
) -> None:
    """记录已由实际 pose 确认的有效平移,并从当前位置重开静止窗口。"""
    state.last_sample_position = current_position
    state.stationary_anchor_position = current_position
    state.stationary_window_started_at = now
    state.last_confirmed_movement_at = now
    state.cooldown_until = now + INACTIVITY_NUDGE_COOLDOWN_SEC
    _clear_inactivity_nudge(state)


def _player_is_inactivity_eligible(
    context: Context,
    player: Player,
) -> bool:
    game = context.game
    ball = context.ball
    pose = player.pose
    if (
        game is None
        or game.state != GameState.PLAYING
        or game.stopped
        or player.is_penalized
        or pose is None
        or ball is None
    ):
        return False
    return (
        dist(pose.x, pose.y, ball.x, ball.y)
        <= INACTIVITY_BALL_DISTANCE_M
    )


def _update_inactivity_tracking(
    context: Context,
    players: list[Player],
    store,
) -> set[int]:
    """更新实际位移锚点,返回本帧满足 M-05 计时条件的球员编号。"""
    eligible_player_ids: set[int] = set()
    active_player_id = getattr(
        store,
        "inactivity_prevention_active_player_id",
        None,
    )

    for player in players:
        state = _get_inactivity_prevention_state(store, player.id)
        pose = player.pose
        current_position = (
            (pose.x, pose.y) if pose is not None else None
        )
        if not _player_is_inactivity_eligible(context, player):
            _clear_inactivity_window(state, current_position)
            if active_player_id == player.id:
                active_player_id = None
            continue

        eligible_player_ids.add(player.id)
        if current_position is None:
            continue

        state.last_sample_position = current_position
        anchor_position = state.stationary_anchor_position
        if anchor_position is None:
            state.stationary_anchor_position = current_position
            state.stationary_window_started_at = context.now
            state.last_confirmed_movement_at = context.now
            continue

        displacement_from_anchor = dist(
            anchor_position[0],
            anchor_position[1],
            current_position[0],
            current_position[1],
        )
        if displacement_from_anchor >= INACTIVITY_MOVEMENT_RESET_M:
            _confirm_inactivity_movement(
                state,
                current_position,
                context.now,
            )
            if active_player_id == player.id:
                active_player_id = None

    store.inactivity_prevention_active_player_id = active_player_id
    return eligible_player_ids


def _clamp_inactivity_target_to_field(
    context: Context,
    target: tuple[float, float],
) -> tuple[float, float]:
    half_length = max(
        0.0,
        context.field.length / 2.0 - INACTIVITY_FIELD_MARGIN_M,
    )
    half_width = max(
        0.0,
        context.field.width / 2.0 - INACTIVITY_FIELD_MARGIN_M,
    )
    return (
        clamp(target[0], -half_length, half_length),
        clamp(target[1], -half_width, half_width),
    )


def _constrain_inactivity_target(
    context: Context,
    target: tuple[float, float],
    phase: Phase,
) -> tuple[float, float] | None:
    """把动态 nudge 目标投影到当前重启规则允许的区域。"""
    safe_target = _clamp_inactivity_target_to_field(context, target)
    if phase == Phase.OUR_KICKOFF:
        safe_target = (
            min(safe_target[0], -INACTIVITY_KICKOFF_HALF_MARGIN_M),
            safe_target[1],
        )
    elif phase == Phase.OPP_KICKOFF:
        safe_target = (
            min(safe_target[0], -INACTIVITY_KICKOFF_HALF_MARGIN_M),
            safe_target[1],
        )
        safe_target = _prepare_restart_target(
            context,
            safe_target,
            stay_outside_center_circle=True,
        )
    elif phase == Phase.OPP_SET_PLAY:
        safe_target = _prepare_restart_target(context, safe_target)

    safe_target = _clamp_inactivity_target_to_field(context, safe_target)
    ball = context.ball
    if phase in (Phase.OPP_KICKOFF, Phase.OPP_SET_PLAY):
        if (
            ball is None
            or dist(safe_target[0], safe_target[1], ball.x, ball.y)
            < OPPONENT_RESTART_AVOID_M
        ):
            return None
    if phase == Phase.OPP_KICKOFF:
        center_clearance = context.field.circle_radius + CIRCLE_MARGIN_M
        if (
            safe_target[0] > -INACTIVITY_KICKOFF_HALF_MARGIN_M
            or math.hypot(safe_target[0], safe_target[1]) < center_clearance
        ):
            return None
    if (
        phase == Phase.OUR_KICKOFF
        and safe_target[0] > -INACTIVITY_KICKOFF_HALF_MARGIN_M
    ):
        return None
    return safe_target


def _get_relative_inactivity_target(
    context: Context,
    player: Player,
    phase: Phase,
) -> tuple[float, float] | None:
    """优先沿球的切向并略微远离球,生成不依赖固定比赛坐标的小位移。"""
    pose = player.pose
    ball = context.ball
    if pose is None or ball is None:
        return None

    away_x = pose.x - ball.x
    away_y = pose.y - ball.y
    away_length = math.hypot(away_x, away_y)
    if away_length <= 1e-6:
        own_goal_x, own_goal_y = own_goal(context)
        away_x = own_goal_x - ball.x
        away_y = own_goal_y - ball.y
        away_length = math.hypot(away_x, away_y)
    if away_length <= 1e-6:
        away_x, away_y, away_length = -1.0, 0.0, 1.0

    away_x /= away_length
    away_y /= away_length
    tangent_x = -away_y
    tangent_y = away_x
    preferred_side = 1.0 if player.id % 2 == 0 else -1.0
    candidate_directions = (
        (
            tangent_x * preferred_side
            + away_x * INACTIVITY_AWAY_FROM_BALL_BIAS,
            tangent_y * preferred_side
            + away_y * INACTIVITY_AWAY_FROM_BALL_BIAS,
        ),
        (
            -tangent_x * preferred_side
            + away_x * INACTIVITY_AWAY_FROM_BALL_BIAS,
            -tangent_y * preferred_side
            + away_y * INACTIVITY_AWAY_FROM_BALL_BIAS,
        ),
        (away_x, away_y),
    )

    current_ball_distance = dist(pose.x, pose.y, ball.x, ball.y)
    safe_candidates: list[tuple[float, float]] = []
    for direction_x, direction_y in candidate_directions:
        direction_length = math.hypot(direction_x, direction_y)
        if direction_length <= 1e-6:
            continue
        preferred_target = (
            pose.x
            + direction_x / direction_length * INACTIVITY_NUDGE_DISTANCE_M,
            pose.y
            + direction_y / direction_length * INACTIVITY_NUDGE_DISTANCE_M,
        )
        safe_target = _constrain_inactivity_target(
            context,
            preferred_target,
            phase,
        )
        if safe_target is None:
            continue
        target_displacement = dist(
            pose.x,
            pose.y,
            safe_target[0],
            safe_target[1],
        )
        target_ball_distance = dist(
            safe_target[0],
            safe_target[1],
            ball.x,
            ball.y,
        )
        if (
            target_displacement >= INACTIVITY_MIN_TARGET_DISPLACEMENT_M
            and target_ball_distance >= current_ball_distance
        ):
            safe_candidates.append(safe_target)

    if not safe_candidates:
        return None
    return max(
        safe_candidates,
        key=lambda candidate: (
            dist(candidate[0], candidate[1], ball.x, ball.y),
            dist(pose.x, pose.y, candidate[0], candidate[1]),
        ),
    )


def _get_goalkeeper_inactivity_target(
    context: Context,
    goalkeeper: Player,
    phase: Phase,
    store,
) -> tuple[float, float] | None:
    """守门员仅围绕当前门前职责目标做很小的横向重新站位。"""
    pose = goalkeeper.pose
    ball = context.ball
    if pose is None or ball is None:
        return None

    base_target = getattr(store, "goalkeeper_target", None)
    if base_target is None:
        base_target = own_goal_area_center(context)
    lateral_limit = _goalkeeper_safe_lateral_limit(context)
    preferred_side = 1.0 if goalkeeper.id % 2 == 0 else -1.0
    candidate_sides = (preferred_side, -preferred_side)
    current_ball_distance = dist(pose.x, pose.y, ball.x, ball.y)
    safe_candidates: list[tuple[float, float]] = []

    for candidate_side in candidate_sides:
        target_x = clamp(
            base_target[0],
            pose.x - INACTIVITY_GOALKEEPER_LONGITUDINAL_ADJUSTMENT_M,
            pose.x + INACTIVITY_GOALKEEPER_LONGITUDINAL_ADJUSTMENT_M,
        )
        preferred_target = (
            target_x,
            clamp(
                pose.y
                + candidate_side * INACTIVITY_GOALKEEPER_NUDGE_DISTANCE_M,
                -lateral_limit,
                lateral_limit,
            ),
        )
        safe_target = _constrain_inactivity_target(
            context,
            preferred_target,
            phase,
        )
        if safe_target is None:
            continue
        target_displacement = dist(
            pose.x,
            pose.y,
            safe_target[0],
            safe_target[1],
        )
        target_ball_distance = dist(
            safe_target[0],
            safe_target[1],
            ball.x,
            ball.y,
        )
        if (
            target_displacement >= INACTIVITY_MIN_TARGET_DISPLACEMENT_M
            and target_ball_distance >= current_ball_distance
        ):
            safe_candidates.append(safe_target)

    if not safe_candidates:
        return None
    return max(
        safe_candidates,
        key=lambda candidate: dist(
            candidate[0], candidate[1], ball.x, ball.y,
        ),
    )


def _inactivity_override_is_protected(
    player: Player,
    current_goalkeeper: Player | None,
    phase: Phase,
    store,
) -> bool:
    """不覆盖主罚、追球、踢球和守门员紧急响应。"""
    if player.is_kicking:
        return True
    locked_tactic_roles = getattr(store, "locked_roles", None) or frozenset()
    if (
        getattr(store, "active_tactic", None) == "throw_in"
        and player.id in locked_tactic_roles
    ):
        return True
    if (
        phase == Phase.OUR_KICKOFF
        and player.id == getattr(store, "kickoff_taker", None)
    ):
        return True

    latest_assignment = getattr(
        store,
        "latest_open_play_role_assignment",
        None,
    )
    if (
        phase == Phase.OUR_SET_PLAY
        and latest_assignment is not None
        and player.id == latest_assignment.offensive_striker_id
    ):
        return True

    if current_goalkeeper is not None and player.id == current_goalkeeper.id:
        goalkeeper_mode = getattr(store, "goalkeeper_mode", None)
        if goalkeeper_mode in (
            GoalkeeperMode.BLOCK,
            GoalkeeperMode.CHALLENGE,
            GoalkeeperMode.CLEAR,
            GoalkeeperMode.RETURN,
        ):
            return True

    protected_action_prefixes = (
        "attack:offensive_striker:",
        "attack:partner_challenge:",
        "defense:presser:",
        "contested:presser:",
        "ball_search:",
    )
    return player.action.startswith(protected_action_prefixes)


def _get_inactivity_action_label(
    player: Player,
    current_goalkeeper: Player | None,
    phase: Phase,
) -> str:
    if current_goalkeeper is not None and player.id == current_goalkeeper.id:
        return "inactivity_prevention:goalkeeper"
    if phase in (Phase.OPP_KICKOFF, Phase.OPP_SET_PLAY):
        return "inactivity_prevention:opp_restart"
    if phase == Phase.OUR_KICKOFF:
        return "inactivity_prevention:kickoff_support"
    return "inactivity_prevention:normal"


def _cancel_active_inactivity_nudge(
    state: InactivityPreventionState,
    now: float,
) -> None:
    _clear_inactivity_nudge(state)
    state.cooldown_until = now + INACTIVITY_NUDGE_COOLDOWN_SEC


def _active_inactivity_target_needs_replacement(
    context: Context,
    player: Player,
    state: InactivityPreventionState,
    current_goalkeeper: Player | None,
    phase: Phase,
) -> bool:
    """阶段、职责或球位变化后重新生成目标,不沿用旧规则环境下的点。"""
    target = state.nudge_target
    if target is None or state.nudge_phase != phase:
        return True
    expected_action = _get_inactivity_action_label(
        player,
        current_goalkeeper,
        phase,
    )
    if state.nudge_action != expected_action:
        return True

    constrained_target = _constrain_inactivity_target(
        context,
        target,
        phase,
    )
    if constrained_target is None or constrained_target != target:
        return True
    ball = context.ball
    pose = player.pose
    return (
        ball is not None
        and pose is not None
        and dist(target[0], target[1], ball.x, ball.y)
        < dist(pose.x, pose.y, ball.x, ball.y)
    )


def _apply_inactivity_prevention(
    context: Context,
    players: list[Player],
    available_players: list[Player],
    current_goalkeeper: Player | None,
    phase: Phase,
    store,
) -> None:
    """策略分派后至多覆盖一名真正长期静止的机器人做安全小位移。"""
    eligible_player_ids = _update_inactivity_tracking(
        context,
        players,
        store,
    )
    if not eligible_player_ids:
        return

    available_players_by_id = {
        player.id: player for player in available_players
    }
    active_player_id = getattr(
        store,
        "inactivity_prevention_active_player_id",
        None,
    )
    active_player = available_players_by_id.get(active_player_id)
    if active_player is not None:
        active_state = _get_inactivity_prevention_state(
            store,
            active_player.id,
        )
        nudge_timed_out = (
            active_state.nudge_started_at is not None
            and context.now - active_state.nudge_started_at
            >= INACTIVITY_NUDGE_TIMEOUT_SEC
        )
        target_needs_replacement = _active_inactivity_target_needs_replacement(
            context,
            active_player,
            active_state,
            current_goalkeeper,
            phase,
        )
        if target_needs_replacement or nudge_timed_out:
            _cancel_active_inactivity_nudge(active_state, context.now)
            store.inactivity_prevention_active_player_id = None
            active_player = None
        elif (
            active_player.id not in eligible_player_ids
            or not active_state.nudge_active
            or active_state.nudge_target is None
            or _inactivity_override_is_protected(
                active_player,
                current_goalkeeper,
                phase,
                store,
            )
        ):
            _cancel_active_inactivity_nudge(active_state, context.now)
            store.inactivity_prevention_active_player_id = None
            active_player = None
    elif active_player_id is not None:
        state = _get_inactivity_prevention_state(store, active_player_id)
        _cancel_active_inactivity_nudge(state, context.now)
        store.inactivity_prevention_active_player_id = None

    if active_player is None:
        trigger_time = min(
            INACTIVITY_PREVENTION_TRIGGER_SEC,
            INACTIVITY_RULE_TIMEOUT_SEC,
        )
        candidates: list[tuple[bool, float, int, Player]] = []
        for player in available_players:
            if player.id not in eligible_player_ids:
                continue
            state = _get_inactivity_prevention_state(store, player.id)
            window_started_at = state.stationary_window_started_at
            if (
                window_started_at is None
                or context.now < state.cooldown_until
                or _inactivity_override_is_protected(
                    player,
                    current_goalkeeper,
                    phase,
                    store,
                )
            ):
                continue
            stationary_duration = context.now - window_started_at
            if stationary_duration >= trigger_time:
                is_goalkeeper = (
                    current_goalkeeper is not None
                    and player.id == current_goalkeeper.id
                )
                candidates.append(
                    (
                        is_goalkeeper,
                        -stationary_duration,
                        player.id,
                        player,
                    )
                )

        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        for _is_goalkeeper, _negative_duration, _player_id, candidate in candidates:
            if (
                current_goalkeeper is not None
                and candidate.id == current_goalkeeper.id
            ):
                target = _get_goalkeeper_inactivity_target(
                    context,
                    candidate,
                    phase,
                    store,
                )
            else:
                target = _get_relative_inactivity_target(
                    context,
                    candidate,
                    phase,
                )
            if target is None or candidate.pose is None:
                continue

            state = _get_inactivity_prevention_state(store, candidate.id)
            state.nudge_active = True
            state.nudge_target = target
            state.nudge_start_position = (
                candidate.pose.x,
                candidate.pose.y,
            )
            state.nudge_started_at = context.now
            state.nudge_action = _get_inactivity_action_label(
                candidate,
                current_goalkeeper,
                phase,
            )
            state.nudge_phase = phase
            store.inactivity_prevention_active_player_id = candidate.id
            active_player = candidate
            break

    if active_player is None or active_player.pose is None:
        return
    active_state = _get_inactivity_prevention_state(
        store,
        active_player.id,
    )
    target = active_state.nudge_target
    action = active_state.nudge_action
    if target is None or action is None:
        return

    ball = context.ball
    face = (
        angle_to(
            active_player.pose.x,
            active_player.pose.y,
            ball.x,
            ball.y,
        )
        if ball is not None else None
    )
    arrived = active_player.walk_to(
        target,
        face=face,
        avoid_ball=True,
        avoid_robots=True,
        arrive_dist=INACTIVITY_NUDGE_ARRIVE_DISTANCE_M,
    )
    active_player.action = action
    if arrived:
        current_position = (
            active_player.pose.x,
            active_player.pose.y,
        )
        _confirm_inactivity_movement(
            active_state,
            current_position,
            context.now,
        )
        store.inactivity_prevention_active_player_id = None


def _resolve_default_goalkeeper_id(
    context: Context,
    players: list[Player],
) -> int | None:
    """解析裁判指定的默认守门员,无效时使用稳定 roster 回退。"""
    roster_ids = {player.id for player in players}
    if not roster_ids:
        return None

    team_state = (
        context.game.get_team_state(context.team_id)
        if context.game is not None else None
    )
    referee_goalkeeper_id = (
        team_state.goalkeeper if team_state is not None else 0
    )
    if referee_goalkeeper_id > 0 and referee_goalkeeper_id in roster_ids:
        return referee_goalkeeper_id
    if DEFAULT_GOALKEEPER_ID in roster_ids:
        return DEFAULT_GOALKEEPER_ID
    return min(roster_ids)


def _select_current_goalkeeper(
    context: Context,
    all_players: list[Player],
    available_players: list[Player],
    phase: Phase,
    store,
) -> Player | None:
    """选择并保持当前守门员,只在安全窗口交还默认守门员职责。"""
    default_goalkeeper_id = _resolve_default_goalkeeper_id(context, all_players)
    store.default_goalkeeper_id = default_goalkeeper_id

    available_by_id = {
        player.id: player for player in available_players
    }
    default_goalkeeper = available_by_id.get(default_goalkeeper_id)
    temporary_goalkeeper_id = getattr(
        store, "temporary_goalkeeper_id", None,
    )

    safe_handover_window = phase in (Phase.READY, Phase.STOPPED)
    if safe_handover_window and default_goalkeeper is not None:
        temporary_goalkeeper_id = None

    temporary_goalkeeper = available_by_id.get(temporary_goalkeeper_id)
    if temporary_goalkeeper is None:
        temporary_goalkeeper_id = None

    if temporary_goalkeeper is not None:
        current_goalkeeper = temporary_goalkeeper
    elif default_goalkeeper is not None:
        current_goalkeeper = default_goalkeeper
    elif available_players:
        own_goal_x, own_goal_y = own_goal(context)
        current_goalkeeper = min(
            available_players,
            key=lambda player: dist(
                player.pose.x, player.pose.y, own_goal_x, own_goal_y,
            ),
        )
        temporary_goalkeeper_id = current_goalkeeper.id
    else:
        current_goalkeeper = None

    store.temporary_goalkeeper_id = temporary_goalkeeper_id
    store.current_goalkeeper_id = (
        current_goalkeeper.id if current_goalkeeper is not None else None
    )
    field_players = [
        player for player in available_players
        if player is not current_goalkeeper
    ]
    store.available_field_player_ids = tuple(
        player.id for player in field_players
    )
    store.can_run_two_player_tactic = len(field_players) >= 2
    return current_goalkeeper


def _clear_normal_sticky(store) -> None:
    store.offensive_striker_id = None
    store.defensive_presser_id = None
    store.last_ball_position = None
    store.last_ball_seen_at = None
    store.ball_visible_frames = 0
    store.ball_searcher = None
    store.ball_lost_since = None
    store.open_play_mode = None
    store.open_play_mode_entered_at = None
    store.open_play_last_switch_at = None
    store.open_play_mode_reason = "inactive"
    store.open_play_last_switch_reason = None
    store.open_play_our_ball_distance = None
    store.open_play_opponent_ball_distance = None
    store.open_play_distance_advantage = None
    store.latest_open_play_role_assignment = OpenPlayRoleAssignment(
        goalkeeper_id=None,
        offensive_striker_id=None,
        front_partner_id=None,
        defensive_presser_id=None,
        defensive_protector_id=None,
        available_player_ids=(),
        availability=OpenPlayAvailability.UNAVAILABLE,
    )


def _reset_goalkeeper_strategy(store) -> None:
    """清除 phase 或守门员身份相关的高级守门跨帧状态。"""
    store.goalkeeper_strategy_player_id = None
    store.goalkeeper_mode = None
    store.goalkeeper_mode_entered_at = None
    store.goalkeeper_challenge_started_at = None
    store.goalkeeper_threat_reason = "inactive"
    store.goalkeeper_previous_ball_position = None
    store.goalkeeper_previous_ball_sample_at = None
    store.goalkeeper_ball_velocity = None
    store.goalkeeper_ball_speed = None
    store.goalkeeper_target = None
    store.goalkeeper_clearance_target = None


def _player_dist_to_ball(context: Context, p: Player) -> float:
    """球员到球当前位置的距离。"""
    ball = context.ball
    return (
        dist(p.pose.x, p.pose.y, ball.x, ball.y) + _fallen_time_cost(p)
        if ball is not None else math.inf
    )


def _fallen_time_cost(p: Player) -> float:
    return FALLEN_COST if p.is_fallen else 0.0


def _select_closest_player_to_ball(
    context: Context,
    players: list[Player],
) -> Player:
    """从非空可用列表中选择当前到球距离最小的球员。"""
    return min(
        players,
        key=lambda player: _player_dist_to_ball(context, player),
    )


def _select_player_with_distance_hysteresis(
    context: Context,
    players: list[Player],
    preferred_id: int | None,
    keep_distance_margin: float,
) -> Player:
    """优先保持现任，只有候选明显更近时才交换职责。"""
    ranked = [(p, _player_dist_to_ball(context, p)) for p in players]
    best, best_dist = min(ranked, key=lambda item: item[1])
    preferred = next((item for item in ranked if item[0].id == preferred_id), None)
    if (
        preferred is not None
        and preferred[1] <= best_dist + keep_distance_margin
    ):
        return preferred[0]
    return best


def _select_offensive_striker(
    context: Context,
    field_players: list[Player],
    preferred_id: int | None,
) -> Player:
    """以独立进攻迟滞选择 offensive striker。"""
    return _select_player_with_distance_hysteresis(
        context,
        field_players,
        preferred_id,
        OFFENSIVE_STRIKER_KEEP_DISTANCE_MARGIN_M,
    )


def _select_defensive_presser(
    context: Context,
    field_players: list[Player],
    preferred_id: int | None,
) -> Player:
    """以独立防守迟滞选择 defensive presser。"""
    return _select_player_with_distance_hysteresis(
        context,
        field_players,
        preferred_id,
        DEFENSIVE_PRESSER_KEEP_DISTANCE_MARGIN_M,
    )


def _classify_open_play_availability(
    available_player_count: int,
) -> OpenPlayAvailability:
    """把 3v3 可用人数映射为明确的普通比赛降级等级。"""
    if available_player_count >= 3:
        return OpenPlayAvailability.FULL_THREE
    if available_player_count == 2:
        return OpenPlayAvailability.DEGRADED_TWO
    if available_player_count == 1:
        return OpenPlayAvailability.DEGRADED_ONE
    return OpenPlayAvailability.UNAVAILABLE


def _assign_open_play_roles(
    context: Context,
    available_players: list[Player],
    current_goalkeeper: Player | None,
    open_play_mode: OpenPlayMode,
    store,
) -> OpenPlayRoleAssignment:
    """按当前模式独立分配进攻 striker 或防守 presser。"""
    available_player_ids = tuple(
        player.id for player in available_players
    )
    availability = _classify_open_play_availability(
        len(available_players),
    )
    available_by_id = {
        player.id: player for player in available_players
    }
    goalkeeper_id = (
        current_goalkeeper.id
        if current_goalkeeper is not None
        and current_goalkeeper.id in available_by_id
        else None
    )
    field_players = [
        player for player in available_players
        if player.id != goalkeeper_id
    ]

    offensive_striker_id = None
    front_partner_id = None
    defensive_presser_id = None
    defensive_protector_id = None
    if open_play_mode == OpenPlayMode.ATTACKING and field_players:
        offensive_striker = _select_offensive_striker(
            context,
            field_players,
            getattr(store, "offensive_striker_id", None),
        )
        offensive_striker_id = offensive_striker.id
        store.offensive_striker_id = offensive_striker_id
        front_partner = next(
            (
                player for player in field_players
                if player.id != offensive_striker_id
            ),
            None,
        )
        front_partner_id = (
            front_partner.id if front_partner is not None else None
        )
    elif field_players:
        defensive_presser = _select_defensive_presser(
            context,
            field_players,
            getattr(store, "defensive_presser_id", None),
        )
        defensive_presser_id = defensive_presser.id
        store.defensive_presser_id = defensive_presser_id
        defensive_protector = next(
            (
                player for player in field_players
                if player.id != defensive_presser_id
            ),
            None,
        )
        defensive_protector_id = (
            defensive_protector.id
            if defensive_protector is not None
            else None
        )

    assignment = OpenPlayRoleAssignment(
        goalkeeper_id=goalkeeper_id,
        offensive_striker_id=offensive_striker_id,
        front_partner_id=front_partner_id,
        defensive_presser_id=defensive_presser_id,
        defensive_protector_id=defensive_protector_id,
        available_player_ids=available_player_ids,
        availability=availability,
    )
    store.latest_open_play_role_assignment = assignment
    return assignment


def _get_assigned_player(
    available_players_by_id: dict[int, Player],
    player_id: int | None,
) -> Player | None:
    """按角色结果中的 ID 取得本帧 available Player。"""
    if player_id is None:
        return None
    return available_players_by_id.get(player_id)


def _act_offensive_striker(offensive_striker: Player) -> None:
    """执行 offensive striker 追球射门并保留内部子动作。"""
    _act_offensive_ball_handler(
        offensive_striker,
        "offensive_striker",
    )


def _act_offensive_front_partner(front_partner: Player) -> None:
    """执行普通进攻回退中的前场搭档支援。"""
    front_partner.support()
    front_partner.action = "attack:partner_support"


def _get_offensive_striker_subaction(player: Player) -> str:
    """把 Player.attack 的内部状态压缩为可读的进攻标签。"""
    if player.action.startswith("offensive_striker:"):
        return player.action.removeprefix("offensive_striker:")
    return "kick" if player.is_kicking else "chase"


def _act_offensive_ball_handler(player: Player, role_label: str) -> None:
    """执行进攻射门入口，并保留绕球、追球或踢球子动作。"""
    player.attack()
    player.action = (
        f"attack:{role_label}:{_get_offensive_striker_subaction(player)}"
    )


def _select_attack_support_side(
    context: Context,
    ball_handler: Player,
    supporting_player: Player,
    goal_direction: tuple[float, float],
) -> float:
    """选择接应侧:边线附近向内,其他位置优先避开主攻到球线路。"""
    ball = context.ball
    if (
        ball is None
        or ball_handler.pose is None
        or supporting_player.pose is None
    ):
        return 1.0

    lateral_direction = (-goal_direction[1], goal_direction[0])
    handler_lateral_offset = (
        (ball_handler.pose.x - ball.x) * lateral_direction[0]
        + (ball_handler.pose.y - ball.y) * lateral_direction[1]
    )
    supporting_lateral_offset = (
        (supporting_player.pose.x - ball.x) * lateral_direction[0]
        + (supporting_player.pose.y - ball.y) * lateral_direction[1]
    )
    half_width = context.field.width / 2.0
    ball_near_touchline = (
        half_width - abs(ball.y)
        <= NORMAL_ATTACK_SUPPORT_TOUCHLINE_ZONE_M
    )

    best_side = 1.0
    best_score = -math.inf
    for candidate_side in (-1.0, 1.0):
        score = 0.0
        candidate_y_direction = candidate_side * lateral_direction[1]
        if ball_near_touchline and candidate_y_direction * ball.y < 0.0:
            score += 4.0
        if (
            abs(handler_lateral_offset) > 1e-6
            and candidate_side * handler_lateral_offset < 0.0
        ):
            score += 2.0
        if candidate_side * supporting_lateral_offset >= 0.0:
            score += 0.5
        if score > best_score:
            best_side = candidate_side
            best_score = score
    return best_side


def _ball_in_attack_rebound_area(context: Context) -> bool:
    """判断球是否进入对方禁区附近的补射、二点球区域。"""
    ball = context.ball
    if ball is None:
        return False

    opponent_goal_line_x = context.field.length / 2.0
    distance_inside_goal_line = opponent_goal_line_x - ball.x
    return (
        -NORMAL_ATTACK_REBOUND_PENALTY_MARGIN_M
        <= distance_inside_goal_line
        <= context.field.penalty_area_length
        + NORMAL_ATTACK_REBOUND_PENALTY_MARGIN_M
        and abs(ball.y)
        <= context.field.penalty_area_width / 2.0
        + NORMAL_ATTACK_REBOUND_PENALTY_MARGIN_M
    )


def _get_dynamic_attack_support_target(
    context: Context,
    ball_handler: Player,
    supporting_player: Player,
) -> tuple[float, float] | None:
    """计算围绕当前球位、朝向对方球门的动态接应或补射站位。"""
    ball = context.ball
    if (
        ball is None
        or ball_handler.pose is None
        or supporting_player.pose is None
    ):
        return None

    opponent_goal_x, opponent_goal_y = opponent_goal(context)
    goal_offset_x = opponent_goal_x - ball.x
    goal_offset_y = opponent_goal_y - ball.y
    goal_distance = math.hypot(goal_offset_x, goal_offset_y)
    if goal_distance <= 1e-6:
        goal_direction = (1.0, 0.0)
    else:
        goal_direction = (
            goal_offset_x / goal_distance,
            goal_offset_y / goal_distance,
        )
    lateral_direction = (-goal_direction[1], goal_direction[0])
    support_side = _select_attack_support_side(
        context,
        ball_handler,
        supporting_player,
        goal_direction,
    )

    near_opponent_penalty_area = _ball_in_attack_rebound_area(context)
    if near_opponent_penalty_area:
        forward_distance = NORMAL_ATTACK_REBOUND_FORWARD_DISTANCE_M
        lateral_distance = NORMAL_ATTACK_REBOUND_LATERAL_DISTANCE_M
    else:
        forward_distance = NORMAL_ATTACK_SUPPORT_FORWARD_DISTANCE_M
        lateral_distance = NORMAL_ATTACK_SUPPORT_LATERAL_DISTANCE_M

    def build_target(selected_lateral_distance: float) -> tuple[float, float]:
        return (
            ball.x + goal_direction[0] * forward_distance
            + lateral_direction[0] * support_side * selected_lateral_distance,
            ball.y + goal_direction[1] * forward_distance
            + lateral_direction[1] * support_side * selected_lateral_distance,
        )

    target_x, target_y = build_target(lateral_distance)
    distance_from_handler = dist(
        target_x,
        target_y,
        ball_handler.pose.x,
        ball_handler.pose.y,
    )
    if distance_from_handler < NORMAL_ATTACK_SUPPORT_STRIKER_SPACING_M:
        lateral_distance += (
            NORMAL_ATTACK_SUPPORT_STRIKER_SPACING_M - distance_from_handler
        )
        target_x, target_y = build_target(lateral_distance)

    half_length = max(
        0.0,
        context.field.length / 2.0 - NORMAL_ATTACK_SUPPORT_FIELD_MARGIN_M,
    )
    half_width = max(
        0.0,
        context.field.width / 2.0 - NORMAL_ATTACK_SUPPORT_FIELD_MARGIN_M,
    )
    clamped_target_x = clamp(target_x, -half_length, half_length)
    clamped_target_y = clamp(target_y, -half_width, half_width)
    clamped_handler_distance = dist(
        clamped_target_x,
        clamped_target_y,
        ball_handler.pose.x,
        ball_handler.pose.y,
    )
    if clamped_handler_distance < NORMAL_ATTACK_SUPPORT_STRIKER_SPACING_M:
        away_from_handler_x = clamped_target_x - ball_handler.pose.x
        away_from_handler_y = clamped_target_y - ball_handler.pose.y
        away_length = math.hypot(away_from_handler_x, away_from_handler_y)
        if away_length <= 1e-6:
            away_from_handler_x = lateral_direction[0] * support_side
            away_from_handler_y = lateral_direction[1] * support_side
            away_length = 1.0
        spacing_scale = NORMAL_ATTACK_SUPPORT_STRIKER_SPACING_M / away_length
        clamped_target_x = clamp(
            ball_handler.pose.x + away_from_handler_x * spacing_scale,
            -half_length,
            half_length,
        )
        clamped_target_y = clamp(
            ball_handler.pose.y + away_from_handler_y * spacing_scale,
            -half_width,
            half_width,
        )
    return (clamped_target_x, clamped_target_y)


def _should_front_partner_challenge(
    context: Context,
    offensive_striker: Player,
    front_partner: Player,
) -> bool:
    """允许近球且明显更近的搭档临时接管,不改变粘滞角色。"""
    if offensive_striker.is_kicking:
        return False
    partner_distance = _player_dist_to_ball(context, front_partner)
    striker_distance = _player_dist_to_ball(context, offensive_striker)
    return (
        partner_distance <= NORMAL_ATTACK_PARTNER_CHALLENGE_DISTANCE_M
        and partner_distance + NORMAL_ATTACK_PARTNER_CLOSER_MARGIN_M
        < striker_distance
    )


def _act_normal_attacking_shape(
    context: Context,
    goalkeeper: Player | None,
    offensive_striker: Player | None,
    front_partner: Player | None,
    store,
) -> None:
    """执行普通比赛双前场进攻形态及安全人数降级。"""
    if offensive_striker is None:
        return
    if front_partner is None:
        _act_offensive_ball_handler(
            offensive_striker,
            "offensive_striker",
        )
        return

    if _should_front_partner_challenge(
        context,
        offensive_striker,
        front_partner,
    ):
        followup_target = _get_dynamic_attack_support_target(
            context,
            front_partner,
            offensive_striker,
        )
        _act_offensive_ball_handler(front_partner, "partner_challenge")
        offensive_striker.move_to_position(followup_target)
        offensive_striker.action = "attack:partner_support"
        return

    support_target = _get_dynamic_attack_support_target(
        context,
        offensive_striker,
        front_partner,
    )
    _act_offensive_ball_handler(
        offensive_striker,
        "offensive_striker",
    )
    front_partner.move_to_position(support_target)
    front_partner.action = (
        "attack:rebound"
        if _ball_in_attack_rebound_area(context)
        else "attack:partner_support"
    )


def _nearest_available_teammate_distance(
    context: Context,
    field_players: list[Player],
) -> float | None:
    """返回可用非守门员到球的最近距离。"""
    ball = context.ball
    player_distances = [
        dist(player.pose.x, player.pose.y, ball.x, ball.y)
        for player in field_players
        if ball is not None and player.pose is not None
    ]
    return min(player_distances) if player_distances else None


def _nearest_opponent_distance(context: Context) -> float | None:
    """返回具有有效位姿的对方机器人到球的最近距离。"""
    ball = context.ball
    opponent_distances = [
        dist(robot.pose.x, robot.pose.y, ball.x, ball.y)
        for robot in context.opponents.values()
        if ball is not None and robot.pose is not None
    ]
    return min(opponent_distances) if opponent_distances else None


def _ball_in_own_danger_area(context: Context) -> bool:
    """判断球是否位于可立即打断模式迟滞的己方门前危险区。"""
    ball = context.ball
    if ball is None:
        return False

    own_goal_line_x = -context.field.length / 2.0
    own_penalty_edge_x = own_goal_line_x + context.field.penalty_area_length
    danger_front_x = max(OPEN_PLAY_DANGER_X_M, own_penalty_edge_x)
    danger_half_width = (
        context.field.penalty_area_width / 2.0
        + OPEN_PLAY_DANGER_LATERAL_MARGIN_M
    )
    in_penalty_channel = (
        ball.x <= danger_front_x
        and abs(ball.y) <= danger_half_width
    )
    immediately_near_goal = (
        ball.x
        <= own_goal_line_x + OPEN_PLAY_IMMEDIATE_GOAL_DANGER_DEPTH_M
    )
    return in_penalty_channel or immediately_near_goal


def _goalkeeper_safe_lateral_limit(context: Context) -> float:
    """返回门柱内侧的守门员最大横向站位。"""
    goal_limited_lateral = max(
        0.0,
        context.field.goal_width / 2.0 - GOALKEEPER_POST_MARGIN_M,
    )
    return min(GOALKEEPER_MAX_LATERAL_M, goal_limited_lateral)


def _get_goalkeeper_home_target(context: Context) -> tuple[float, float]:
    """根据球到己方门的距离计算远近不同增益的动态门前目标。"""
    own_goal_x, _own_goal_y = own_goal(context)
    home_x = own_goal_x + GOALKEEPER_HOME_X_OFFSET_M
    ball = context.ball
    if ball is None:
        return (home_x, 0.0)

    ball_goal_distance = max(0.0, ball.x - own_goal_x)
    near_weight = 1.0 - clamp(
        ball_goal_distance / max(GOALKEEPER_TRACK_NEAR_DISTANCE_M, 1e-6),
        0.0,
        1.0,
    )
    tracking_gain = (
        GOALKEEPER_TRACK_GAIN_FAR
        + (GOALKEEPER_TRACK_GAIN_NEAR - GOALKEEPER_TRACK_GAIN_FAR)
        * near_weight
    )
    lateral_limit = _goalkeeper_safe_lateral_limit(context)
    home_y = clamp(
        ball.y * tracking_gain,
        -lateral_limit,
        lateral_limit,
    )
    return (home_x, home_y)


def _estimate_goalkeeper_ball_velocity(
    context: Context,
    store,
) -> tuple[float, float] | None:
    """以新球观测做有限差分；异常样本只重播种，不输出速度。"""
    ball = context.ball
    if ball is None:
        store.goalkeeper_previous_ball_position = None
        store.goalkeeper_previous_ball_sample_at = None
        store.goalkeeper_ball_velocity = None
        store.goalkeeper_ball_speed = None
        return None

    sample_at = ball.last_seen_at if ball.last_seen_at > 0.0 else context.now
    current_position = (ball.x, ball.y)
    previous_position = getattr(
        store, "goalkeeper_previous_ball_position", None,
    )
    previous_sample_at = getattr(
        store, "goalkeeper_previous_ball_sample_at", None,
    )

    if previous_position is None or previous_sample_at is None:
        store.goalkeeper_previous_ball_position = current_position
        store.goalkeeper_previous_ball_sample_at = sample_at
        store.goalkeeper_ball_velocity = None
        store.goalkeeper_ball_speed = None
        return None

    sample_interval = sample_at - previous_sample_at
    if sample_interval <= 0.0:
        velocity_age = max(0.0, context.now - previous_sample_at)
        if velocity_age <= GOALKEEPER_BALL_VELOCITY_MAX_AGE_SEC:
            return getattr(store, "goalkeeper_ball_velocity", None)
        store.goalkeeper_ball_velocity = None
        store.goalkeeper_ball_speed = None
        return None

    displacement = dist(
        previous_position[0],
        previous_position[1],
        current_position[0],
        current_position[1],
    )
    store.goalkeeper_previous_ball_position = current_position
    store.goalkeeper_previous_ball_sample_at = sample_at

    valid_sample_interval = (
        GOALKEEPER_BALL_SAMPLE_MIN_SEC
        <= sample_interval
        <= GOALKEEPER_BALL_SAMPLE_MAX_SEC
    )
    if (
        not valid_sample_interval
        or displacement > GOALKEEPER_BALL_SAMPLE_MAX_JUMP_M
    ):
        store.goalkeeper_ball_velocity = None
        store.goalkeeper_ball_speed = None
        return None

    velocity_x = (current_position[0] - previous_position[0]) / sample_interval
    velocity_y = (current_position[1] - previous_position[1]) / sample_interval
    ball_speed = math.hypot(velocity_x, velocity_y)
    if ball_speed > GOALKEEPER_BALL_MAX_CREDIBLE_SPEED_MPS:
        store.goalkeeper_ball_velocity = None
        store.goalkeeper_ball_speed = None
        return None

    velocity = (velocity_x, velocity_y)
    store.goalkeeper_ball_velocity = velocity
    store.goalkeeper_ball_speed = ball_speed
    return velocity


def _estimate_goalkeeper_threat(
    context: Context,
    ball_velocity: tuple[float, float] | None,
    opponent_nearest_distance: float | None,
) -> GoalkeeperThreatEstimate:
    """判断快速射门投影或无可靠速度时的保守门前位置威胁。"""
    ball = context.ball
    if ball is None:
        return GoalkeeperThreatEstimate(False, False, None, "ball_unknown")

    own_goal_x, _own_goal_y = own_goal(context)
    goal_projection_limit = (
        context.field.goal_width / 2.0
        + GOALKEEPER_SHOT_PROJECTION_MARGIN_M
    )
    if ball_velocity is not None:
        velocity_x, velocity_y = ball_velocity
        ball_speed = math.hypot(velocity_x, velocity_y)
        moving_toward_goal = velocity_x <= -GOALKEEPER_GOALWARD_VX_MPS
        if moving_toward_goal and ball.x > own_goal_x:
            time_to_goal_line = (own_goal_x - ball.x) / velocity_x
            projected_goal_y = ball.y + velocity_y * time_to_goal_line
            fast_goal_threat = (
                ball.x < 0.0
                and ball_speed >= GOALKEEPER_FAST_BALL_SPEED_MPS
                and time_to_goal_line >= 0.0
                and abs(projected_goal_y) <= goal_projection_limit
            )
            if fast_goal_threat:
                return GoalkeeperThreatEstimate(
                    True,
                    True,
                    projected_goal_y,
                    "fast_shot_projection",
                )

    opponent_can_shoot = (
        opponent_nearest_distance is not None
        and opponent_nearest_distance
        <= GOALKEEPER_POSITION_THREAT_OPPONENT_DISTANCE_M
    )
    ball_in_shooting_channel = (
        ball.x <= GOALKEEPER_POSITION_THREAT_X_M
        and abs(ball.y) <= GOALKEEPER_POSITION_THREAT_LATERAL_M
    )
    position_threat = (
        ball_in_shooting_channel
        and (opponent_can_shoot or _ball_in_own_danger_area(context))
    )
    return GoalkeeperThreatEstimate(
        False,
        position_threat,
        None,
        "position_threat" if position_threat else "no_direct_threat",
    )


def _get_goalkeeper_block_target(
    context: Context,
    ball_velocity: tuple[float, float] | None,
) -> tuple[float, float]:
    """计算球运动射线在门前封堵 X 上的交点。"""
    own_goal_x, _own_goal_y = own_goal(context)
    block_x = own_goal_x + GOALKEEPER_BLOCK_X_OFFSET_M
    home_target = _get_goalkeeper_home_target(context)
    block_y = home_target[1]
    ball = context.ball
    if ball is not None and ball_velocity is not None:
        velocity_x, velocity_y = ball_velocity
        if velocity_x < -1e-6 and ball.x > block_x:
            time_to_block_x = (block_x - ball.x) / velocity_x
            if time_to_block_x >= 0.0:
                block_y = ball.y + velocity_y * time_to_block_x

    lateral_limit = _goalkeeper_safe_lateral_limit(context)
    return (
        block_x,
        clamp(block_y, -lateral_limit, lateral_limit),
    )


def _goalkeeper_can_challenge(
    context: Context,
    goalkeeper: Player,
    opponent_nearest_distance: float | None,
    ball_speed: float | None,
    field_player_count: int,
    current_mode: GoalkeeperMode | None,
) -> bool:
    """仅在慢球、对手数据有效且守门员明确先到时允许出击。"""
    ball = context.ball
    pose = goalkeeper.pose
    if (
        ball is None
        or pose is None
        or opponent_nearest_distance is None
        or field_player_count < 2
    ):
        return False

    distance_limit = (
        GOALKEEPER_CHALLENGE_EXIT_DISTANCE_M
        if current_mode == GoalkeeperMode.CHALLENGE
        else GOALKEEPER_CHALLENGE_ENTER_DISTANCE_M
    )
    goalkeeper_distance = dist(pose.x, pose.y, ball.x, ball.y)
    ball_is_slow_enough = (
        ball_speed is None or ball_speed <= GOALKEEPER_SLOW_BALL_SPEED_MPS
    )
    ball_in_challenge_area = (
        ball.x <= GOALKEEPER_CHALLENGE_MAX_X_M
        and abs(ball.y) <= GOALKEEPER_CHALLENGE_MAX_LATERAL_M
    )
    goalkeeper_arrives_first = (
        goalkeeper_distance + GOALKEEPER_CHALLENGE_ADVANTAGE_M
        < opponent_nearest_distance
    )
    return (
        ball_is_slow_enough
        and ball_in_challenge_area
        and goalkeeper_distance <= distance_limit
        and goalkeeper_arrives_first
    )


def _point_to_segment_distance(
    point: tuple[float, float],
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
) -> float:
    """返回点到线段的最短距离，用于选择较空的解围走廊。"""
    segment_x = segment_end[0] - segment_start[0]
    segment_y = segment_end[1] - segment_start[1]
    segment_length_squared = segment_x * segment_x + segment_y * segment_y
    if segment_length_squared <= 1e-9:
        return dist(point[0], point[1], segment_start[0], segment_start[1])

    projection = (
        (point[0] - segment_start[0]) * segment_x
        + (point[1] - segment_start[1]) * segment_y
    ) / segment_length_squared
    projection = clamp(projection, 0.0, 1.0)
    nearest_x = segment_start[0] + segment_x * projection
    nearest_y = segment_start[1] + segment_y * projection
    return dist(point[0], point[1], nearest_x, nearest_y)


def _get_safe_goalkeeper_clearance_target(
    context: Context,
) -> tuple[float, float] | None:
    """从全部正 X 候选中选择对手走廊净空最大的前场或边路目标。"""
    ball = context.ball
    if ball is None:
        return None

    half_length = (
        context.field.length / 2.0 - GOALKEEPER_CLEAR_FIELD_MARGIN_M
    )
    half_width = max(
        0.0,
        context.field.width / 2.0 - GOALKEEPER_CLEAR_FIELD_MARGIN_M,
    )
    if half_length <= ball.x:
        return None

    minimum_target_x = min(ball.x + 0.5, half_length)
    target_x = clamp(
        ball.x + GOALKEEPER_CLEAR_FORWARD_DISTANCE_M,
        minimum_target_x,
        half_length,
    )
    center_target_y = clamp(
        ball.y * 0.35,
        -min(half_width, GOALKEEPER_CLEAR_CENTER_BAND_M),
        min(half_width, GOALKEEPER_CLEAR_CENTER_BAND_M),
    )
    side_target_y = min(
        half_width,
        max(abs(ball.y), GOALKEEPER_CLEAR_LATERAL_DISTANCE_M),
    )
    candidates = [
        (target_x, center_target_y),
        (target_x, side_target_y),
        (target_x, -side_target_y),
    ]
    opponent_positions = [
        (robot.pose.x, robot.pose.y)
        for robot in context.opponents.values()
        if robot.pose is not None
    ]
    if not opponent_positions:
        return candidates[0]

    ball_position = (ball.x, ball.y)

    def corridor_clearance(candidate: tuple[float, float]) -> float:
        return min(
            _point_to_segment_distance(
                opponent_position,
                ball_position,
                candidate,
            )
            for opponent_position in opponent_positions
        )

    return max(candidates, key=corridor_clearance)


def _goalkeeper_has_returned(
    goalkeeper: Player,
    home_target: tuple[float, float],
) -> bool:
    pose = goalkeeper.pose
    return (
        pose is not None
        and dist(pose.x, pose.y, home_target[0], home_target[1])
        <= GOALKEEPER_RETURN_ARRIVE_M
    )


def _update_goalkeeper_mode(
    context: Context,
    goalkeeper: Player,
    threat: GoalkeeperThreatEstimate,
    can_challenge: bool,
    ball_clearable: bool,
    home_target: tuple[float, float],
    allow_active_response: bool,
    store,
) -> GoalkeeperMode:
    """按快速封堵优先级、迟滞和超时更新守门员模式。"""
    current_mode = getattr(store, "goalkeeper_mode", None)
    mode_entered_at = getattr(store, "goalkeeper_mode_entered_at", None)
    challenge_started_at = getattr(
        store, "goalkeeper_challenge_started_at", None,
    )
    mode_elapsed = (
        math.inf
        if mode_entered_at is None
        else max(0.0, context.now - mode_entered_at)
    )
    challenge_timed_out = (
        current_mode == GoalkeeperMode.CHALLENGE
        and challenge_started_at is not None
        and context.now - challenge_started_at
        >= GOALKEEPER_CHALLENGE_TIMEOUT_SEC
    )
    clear_timed_out = (
        current_mode == GoalkeeperMode.CLEAR
        and mode_elapsed >= GOALKEEPER_CLEAR_TIMEOUT_SEC
    )

    ball = context.ball
    if not allow_active_response:
        candidate_mode = (
            GoalkeeperMode.HOLD if ball is None else GoalkeeperMode.TRACK
        )
        reason = "conservative_restart"
    elif threat.fast_goal_threat:
        candidate_mode = GoalkeeperMode.BLOCK
        reason = threat.reason
    elif challenge_timed_out:
        candidate_mode = GoalkeeperMode.RETURN
        reason = "challenge_timeout"
    elif clear_timed_out:
        candidate_mode = GoalkeeperMode.RETURN
        reason = "clear_timeout"
    elif (
        current_mode == GoalkeeperMode.CLEAR
        and mode_elapsed < GOALKEEPER_CLEAR_MIN_HOLD_SEC
    ):
        candidate_mode = GoalkeeperMode.CLEAR
        reason = "clear_min_hold"
    elif current_mode == GoalkeeperMode.RETURN and not _goalkeeper_has_returned(
        goalkeeper, home_target,
    ):
        if threat.position_threat:
            candidate_mode = GoalkeeperMode.BLOCK
            reason = threat.reason
        else:
            candidate_mode = GoalkeeperMode.RETURN
            reason = "returning_home"
    elif ball_clearable:
        candidate_mode = GoalkeeperMode.CLEAR
        reason = "danger_ball_in_clear_range"
    elif can_challenge:
        candidate_mode = GoalkeeperMode.CHALLENGE
        reason = "goalkeeper_arrives_first"
    elif threat.position_threat:
        candidate_mode = GoalkeeperMode.BLOCK
        reason = threat.reason
    elif current_mode in (GoalkeeperMode.CHALLENGE, GoalkeeperMode.CLEAR):
        candidate_mode = GoalkeeperMode.RETURN
        reason = "active_condition_ended"
    elif ball is not None and ball.x <= OPEN_PLAY_CONTESTED_BAND_M:
        candidate_mode = GoalkeeperMode.TRACK
        reason = "track_visible_ball"
    else:
        candidate_mode = GoalkeeperMode.HOLD
        reason = "hold_safe_area"

    returning_into_position_threat = (
        current_mode == GoalkeeperMode.RETURN
        and threat.position_threat
    )
    active_mode_lost_ball = (
        ball is None
        and current_mode in (
            GoalkeeperMode.BLOCK,
            GoalkeeperMode.CHALLENGE,
            GoalkeeperMode.CLEAR,
        )
    )
    force_switch = (
        not allow_active_response
        or threat.fast_goal_threat
        or ball_clearable
        or challenge_timed_out
        or clear_timed_out
        or returning_into_position_threat
        or active_mode_lost_ball
    )
    held_long_enough = (
        current_mode is None
        or mode_elapsed >= GOALKEEPER_MODE_MIN_HOLD_SEC
    )
    if (
        current_mode is None
        or (
            candidate_mode != current_mode
            and (force_switch or held_long_enough)
        )
    ):
        previous_mode = current_mode
        current_mode = candidate_mode
        store.goalkeeper_mode = current_mode
        store.goalkeeper_mode_entered_at = context.now
        if current_mode == GoalkeeperMode.CHALLENGE:
            store.goalkeeper_challenge_started_at = context.now
        else:
            store.goalkeeper_challenge_started_at = None
        _log.info(
            "goalkeeper mode %s -> %s reason=%s",
            previous_mode.value if previous_mode is not None else "none",
            current_mode.value,
            reason,
        )
    elif candidate_mode != current_mode:
        reason = "hold_hysteresis"

    store.goalkeeper_threat_reason = reason
    return current_mode


def _draw_goalkeeper_strategy(
    context: Context,
    goalkeeper: Player,
    mode: GoalkeeperMode,
    target: tuple[float, float],
    threat: GoalkeeperThreatEstimate,
    clearance_target: tuple[float, float] | None,
    store,
) -> None:
    """显示守门模式、动态目标、可靠射门线和解围目标。"""
    from .framework import debugdraw

    debugdraw.point(
        target[0], target[1],
        rgb=(0.0, 0.8, 1.0), scale=0.22, ns="goalkeeper_target",
    )
    ball = context.ball
    if ball is not None and threat.projected_goal_y is not None:
        own_goal_x, _own_goal_y = own_goal(context)
        debugdraw.line(
            [(ball.x, ball.y), (own_goal_x, threat.projected_goal_y)],
            rgb=(1.0, 0.3, 0.0), ns="goalkeeper_shot_line",
        )
    if ball is not None and clearance_target is not None:
        debugdraw.line(
            [(ball.x, ball.y), clearance_target],
            rgb=(0.2, 1.0, 0.4), ns="goalkeeper_clearance",
        )
        debugdraw.point(
            clearance_target[0], clearance_target[1],
            rgb=(0.2, 1.0, 0.4), scale=0.18,
            ns="goalkeeper_clearance_target",
        )

    goalkeeper_kind = (
        "temporary"
        if goalkeeper.id == getattr(store, "temporary_goalkeeper_id", None)
        else "default"
    )
    speed = getattr(store, "goalkeeper_ball_speed", None)
    speed_label = "n/a" if speed is None else f"{speed:.2f}"
    debugdraw.text(
        0.0,
        context.field.width / 2.0 + 0.9,
        (
            f"goalkeeper={goalkeeper_kind} mode={mode.value} "
            f"reason={store.goalkeeper_threat_reason} speed={speed_label}"
        ),
        rgb=(0.2, 0.8, 1.0),
        ns="goalkeeper_mode",
    )


def _act_goalkeeper_strategy(
    context: Context,
    goalkeeper: Player,
    field_players: list[Player],
    store,
    *,
    allow_active_response: bool,
) -> None:
    """统一执行默认或临时守门员的动态站位和高级动作。"""
    if goalkeeper.pose is None:
        goalkeeper.action = "goalkeeper:no_pose"
        goalkeeper.stop()
        return

    if getattr(store, "goalkeeper_strategy_player_id", None) != goalkeeper.id:
        _reset_goalkeeper_strategy(store)
        store.goalkeeper_strategy_player_id = goalkeeper.id

    ball_velocity = _estimate_goalkeeper_ball_velocity(context, store)
    ball_speed = getattr(store, "goalkeeper_ball_speed", None)
    opponent_nearest_distance = _nearest_opponent_distance(context)
    threat = _estimate_goalkeeper_threat(
        context,
        ball_velocity,
        opponent_nearest_distance,
    )
    home_target = _get_goalkeeper_home_target(context)
    current_mode = getattr(store, "goalkeeper_mode", None)
    open_play_mode = getattr(store, "open_play_mode", None)
    has_explicit_field_protection = open_play_mode in (
        OpenPlayMode.DEFENDING,
        OpenPlayMode.CONTESTED,
    )
    can_challenge = (
        allow_active_response
        and has_explicit_field_protection
        and not threat.fast_goal_threat
        and _goalkeeper_can_challenge(
            context,
            goalkeeper,
            opponent_nearest_distance,
            ball_speed,
            len(field_players),
            current_mode,
        )
    )

    ball = context.ball
    goalkeeper_ball_distance = (
        dist(
            goalkeeper.pose.x,
            goalkeeper.pose.y,
            ball.x,
            ball.y,
        )
        if ball is not None else math.inf
    )
    clear_distance_limit = (
        GOALKEEPER_CLEAR_EXIT_DISTANCE_M
        if current_mode == GoalkeeperMode.CLEAR
        else GOALKEEPER_CLEAR_ENTER_DISTANCE_M
    )
    ball_clearable = (
        allow_active_response
        and ball is not None
        and not threat.fast_goal_threat
        and _ball_in_own_danger_area(context)
        and goalkeeper_ball_distance <= clear_distance_limit
    )
    mode = _update_goalkeeper_mode(
        context,
        goalkeeper,
        threat,
        can_challenge,
        ball_clearable,
        home_target,
        allow_active_response,
        store,
    )

    clearance_target = None
    goalkeeper_subaction = None
    if mode == GoalkeeperMode.BLOCK:
        target = _get_goalkeeper_block_target(context, ball_velocity)
        goalkeeper.guard(
            target,
            avoid_ball=False,
            avoid_robots=True,
            arrive_dist=GOALKEEPER_TRACK_ARRIVE_M,
        )
    elif mode == GoalkeeperMode.CHALLENGE and ball is not None:
        target = (ball.x, ball.y)
        goalkeeper.goalkeeper_challenge(target)
        goalkeeper_subaction = goalkeeper.action.removeprefix(
            "goalkeeper:challenge:",
        )
    elif mode == GoalkeeperMode.CLEAR:
        clearance_target = _get_safe_goalkeeper_clearance_target(context)
        target = (ball.x, ball.y) if ball is not None else home_target
        if clearance_target is None:
            goalkeeper.guard(home_target)
            goalkeeper_subaction = "fallback_guard"
        else:
            goalkeeper.goalkeeper_clear(
                clearance_target,
                GOALKEEPER_CLEAR_POWER,
            )
            goalkeeper_subaction = goalkeeper.action.removeprefix(
                "goalkeeper:clear:",
            )
    else:
        target = home_target
        goalkeeper.guard(
            target,
            avoid_ball=True,
            avoid_robots=True,
            arrive_dist=GOALKEEPER_TRACK_ARRIVE_M,
        )

    store.goalkeeper_target = target
    store.goalkeeper_clearance_target = clearance_target
    goalkeeper_kind = (
        "temporary"
        if goalkeeper.id == getattr(store, "temporary_goalkeeper_id", None)
        else "default"
    )
    goalkeeper.action = f"goalkeeper:{mode.value}:{goalkeeper_kind}"
    if goalkeeper_subaction is not None:
        goalkeeper.action = f"{goalkeeper.action}:{goalkeeper_subaction}"
    _draw_goalkeeper_strategy(
        context,
        goalkeeper,
        mode,
        target,
        threat,
        clearance_target,
        store,
    )


def _estimate_open_play_mode(
    context: Context,
    field_players: list[Player],
    previous_mode: OpenPlayMode | None,
) -> OpenPlayModeEstimate:
    """综合门前危险、双方到球距离和球场区域估计普通比赛模式。"""
    ball = context.ball
    our_nearest_distance = _nearest_available_teammate_distance(
        context, field_players,
    )
    opponent_nearest_distance = _nearest_opponent_distance(context)
    distance_advantage = (
        opponent_nearest_distance - our_nearest_distance
        if our_nearest_distance is not None
        and opponent_nearest_distance is not None
        else None
    )
    ball_in_danger_area = _ball_in_own_danger_area(context)

    opponent_is_actively_challenging = (
        opponent_nearest_distance is not None
        and opponent_nearest_distance
        <= OPEN_PLAY_OPPONENT_ACTIVE_CHALLENGE_DISTANCE_M
    )

    if ball_in_danger_area:
        candidate_mode = OpenPlayMode.DEFENDING
        reason = "own_danger_area"
    elif opponent_is_actively_challenging:
        if (
            distance_advantage is not None
            and distance_advantage
            <= -OPEN_PLAY_DEFENSE_DISTANCE_ADVANTAGE_M
        ):
            candidate_mode = OpenPlayMode.DEFENDING
            reason = "opponent_active_challenge_advantage"
        else:
            candidate_mode = OpenPlayMode.CONTESTED
            reason = "opponent_active_challenge"
    elif distance_advantage is not None:
        if distance_advantage >= OPEN_PLAY_ATTACK_DISTANCE_ADVANTAGE_M:
            candidate_mode = OpenPlayMode.ATTACKING
            reason = "own_distance_advantage"
        elif distance_advantage <= -OPEN_PLAY_DEFENSE_DISTANCE_ADVANTAGE_M:
            candidate_mode = OpenPlayMode.DEFENDING
            reason = "opponent_distance_advantage"
        elif abs(distance_advantage) <= OPEN_PLAY_CONTESTED_DISTANCE_BAND_M:
            candidate_mode = OpenPlayMode.CONTESTED
            reason = "balanced_ball_distance"
        elif ball is not None and ball.x >= OPEN_PLAY_CONTESTED_BAND_M:
            candidate_mode = OpenPlayMode.ATTACKING
            reason = "opponent_half_no_clear_opponent_advantage"
        elif previous_mode is not None:
            candidate_mode = previous_mode
            reason = "hold_hysteresis"
        else:
            candidate_mode = OpenPlayMode.CONTESTED
            reason = "uncertain_distance_advantage"
    elif our_nearest_distance is not None:
        if ball is not None and ball.x >= -OPEN_PLAY_CONTESTED_BAND_M:
            candidate_mode = OpenPlayMode.ATTACKING
            reason = "opponent_data_missing_safe_ball"
        else:
            candidate_mode = OpenPlayMode.CONTESTED
            reason = "opponent_data_missing_backfield"
    elif opponent_nearest_distance is not None:
        candidate_mode = OpenPlayMode.DEFENDING
        reason = "no_available_field_player"
    else:
        candidate_mode = OpenPlayMode.CONTESTED
        reason = "insufficient_pose_data"

    return OpenPlayModeEstimate(
        candidate_mode=candidate_mode,
        reason=reason,
        our_nearest_ball_distance=our_nearest_distance,
        opponent_nearest_ball_distance=opponent_nearest_distance,
        distance_advantage=distance_advantage,
        ball_in_own_danger_area=ball_in_danger_area,
    )


def _update_open_play_mode(
    context: Context,
    estimate: OpenPlayModeEstimate,
    store,
) -> OpenPlayMode:
    """应用最短保持和危险区抢占，返回本帧稳定战术模式。"""
    current_mode = getattr(store, "open_play_mode", None)
    mode_entered_at = getattr(store, "open_play_mode_entered_at", None)

    store.open_play_our_ball_distance = estimate.our_nearest_ball_distance
    store.open_play_opponent_ball_distance = (
        estimate.opponent_nearest_ball_distance
    )
    store.open_play_distance_advantage = estimate.distance_advantage

    danger_forces_defense = (
        estimate.ball_in_own_danger_area
        and estimate.candidate_mode == OpenPlayMode.DEFENDING
    )
    mode_has_been_held_long_enough = (
        mode_entered_at is None
        or context.now - mode_entered_at >= OPEN_PLAY_MODE_MIN_HOLD_SEC
    )
    should_switch = (
        current_mode is None
        or (
            estimate.candidate_mode != current_mode
            and (danger_forces_defense or mode_has_been_held_long_enough)
        )
    )

    if should_switch:
        previous_mode = current_mode
        current_mode = estimate.candidate_mode
        store.open_play_mode = current_mode
        store.open_play_mode_entered_at = context.now
        store.open_play_last_switch_at = context.now
        store.open_play_mode_reason = estimate.reason
        store.open_play_last_switch_reason = estimate.reason
        _log.info(
            "open play mode %s -> %s reason=%s our_distance=%s "
            "opponent_distance=%s advantage=%s",
            previous_mode.value if previous_mode is not None else "none",
            current_mode.value,
            estimate.reason,
            estimate.our_nearest_ball_distance,
            estimate.opponent_nearest_ball_distance,
            estimate.distance_advantage,
        )
    elif estimate.candidate_mode == current_mode:
        store.open_play_mode_reason = estimate.reason
    else:
        store.open_play_mode_reason = "hold_hysteresis"

    return current_mode


def _format_open_play_distance(distance: float | None) -> str:
    return "n/a" if distance is None else f"{distance:.2f}"


def _draw_open_play_mode(context: Context, store) -> None:
    """在场外显示当前模式、原因和双方最近到球距离。"""
    from .framework import debugdraw

    mode = getattr(store, "open_play_mode", None)
    if mode is None:
        return
    reason = getattr(store, "open_play_mode_reason", "unknown")
    our_distance = _format_open_play_distance(
        getattr(store, "open_play_our_ball_distance", None),
    )
    opponent_distance = _format_open_play_distance(
        getattr(store, "open_play_opponent_ball_distance", None),
    )
    advantage = _format_open_play_distance(
        getattr(store, "open_play_distance_advantage", None),
    )
    debugdraw.text(
        0.0,
        context.field.width / 2.0 + 0.55,
        (
            f"mode={mode.value} mode_reason={reason} "
            f"our={our_distance} opp={opponent_distance} adv={advantage}"
        ),
        rgb=(0.4, 1.0, 1.0),
        ns="open_play_mode",
    )


def _get_normal_defense_protect_target(
    context: Context,
) -> tuple[float, float] | None:
    """计算球到己方球门线段上的保护点。"""
    ball = context.ball
    if ball is None:
        return None

    own_goal_x, own_goal_y = own_goal(context)
    route_x = own_goal_x - ball.x
    route_y = own_goal_y - ball.y
    route_length = math.hypot(route_x, route_y)
    if route_length <= 1e-6:
        return own_goal_area_center(context)

    desired_route_ratio = min(
        1.0,
        NORMAL_DEFENSE_PROTECT_DISTANCE_M / route_length,
    )
    if route_length > NORMAL_DEFENSE_GOAL_LINE_CLEARANCE_M:
        maximum_route_ratio = (
            1.0
            - NORMAL_DEFENSE_GOAL_LINE_CLEARANCE_M / route_length
        )
    else:
        # 球已贴近门线时无法同时满足门线余量，退化为线段中点。
        maximum_route_ratio = 0.5

    half_length = context.field.length / 2.0
    field_margin_x = -half_length + NORMAL_DEFENSE_FIELD_MARGIN_M
    if ball.x >= field_margin_x and ball.x > own_goal_x:
        maximum_field_ratio = (
            (ball.x - field_margin_x) / (ball.x - own_goal_x)
        )
        maximum_route_ratio = min(
            maximum_route_ratio,
            maximum_field_ratio,
        )

    route_ratio = clamp(
        min(desired_route_ratio, maximum_route_ratio),
        0.0,
        1.0,
    )
    return (
        ball.x + route_x * route_ratio,
        ball.y + route_y * route_ratio,
    )


def _act_defensive_presser(
    defensive_presser: Player,
    action_scope: str,
) -> None:
    """执行独立防守逼抢入口，并标明 defense 或 contested 场景。"""
    defensive_presser.defensive_press_and_clear()
    presser_subaction = defensive_presser.action.removeprefix(
        "defensive_presser:",
    )
    defensive_presser.action = (
        f"{action_scope}:presser:{presser_subaction}"
    )


def _act_normal_defense(
    context: Context,
    goalkeeper: Player | None,
    defensive_presser: Player | None,
    defensive_protector: Player | None,
    store,
) -> None:
    """按已分配职责执行普通比赛的守门、逼抢和保护。"""
    if defensive_presser is not None:
        _act_defensive_presser(defensive_presser, "defense")

    if defensive_protector is not None:
        protect_target = _get_normal_defense_protect_target(context)
        defensive_protector.move_to_position(protect_target)
        defensive_protector.action = "defense:protector"


def _act_normal_contested_shape(
    context: Context,
    goalkeeper: Player | None,
    defensive_presser: Player | None,
    defensive_protector: Player | None,
    store,
) -> None:
    """执行争议球安全结构：一人处理球，另一人保护球门方向中路。"""
    if defensive_presser is not None:
        _act_defensive_presser(defensive_presser, "contested")

    if defensive_protector is not None:
        protect_target = _get_normal_defense_protect_target(context)
        defensive_protector.move_to_position(protect_target)
        defensive_protector.action = "contested:protector"


def _act_normal(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    store,
    *,
    allow_ball_search: bool = True,
) -> None:
    """NORMAL:稳定选择进攻、防守或争议球结构后执行对应动作。

    固定战术未来可在调用本入口前独立分派,从而绕过普通比赛角色分配。
    """
    available_players_by_id = {
        player.id: player for player in players
    }
    availability = _classify_open_play_availability(len(players))
    goalkeeper_id = (
        goalkeeper.id
        if goalkeeper is not None
        and goalkeeper.id in available_players_by_id
        else None
    )
    role_goalkeeper = _get_assigned_player(
        available_players_by_id,
        goalkeeper_id,
    )
    field_players = [
        player for player in players
        if player.id != goalkeeper_id
    ]
    store.latest_open_play_role_assignment = OpenPlayRoleAssignment(
        goalkeeper_id=goalkeeper_id,
        offensive_striker_id=None,
        front_partner_id=None,
        defensive_presser_id=None,
        defensive_protector_id=None,
        available_player_ids=tuple(player.id for player in players),
        availability=availability,
    )

    if availability == OpenPlayAvailability.UNAVAILABLE:
        return

    ball_confirmed = (
        _update_ball_recovery_state(context, store)
        if allow_ball_search else context.ball is not None
    )
    if not ball_confirmed:
        if allow_ball_search:
            _act_ball_recovery(
                context, field_players, role_goalkeeper, store,
            )
        else:
            if role_goalkeeper is not None:
                _act_goalkeeper_guard(
                    context,
                    role_goalkeeper,
                    field_players,
                    store,
                    allow_active_response=False,
                )
            for player in field_players:
                player.action = "ball_unknown:stop"
                player.stop()
        return

    if not allow_ball_search:
        # OUR_SET_PLAY 暂时复用该入口，但固定战术不进入普通比赛三态。
        assignment = _assign_open_play_roles(
            context,
            players,
            role_goalkeeper,
            OpenPlayMode.ATTACKING,
            store,
        )
        offensive_striker = _get_assigned_player(
            available_players_by_id,
            assignment.offensive_striker_id,
        )
        front_partner = _get_assigned_player(
            available_players_by_id,
            assignment.front_partner_id,
        )
        if role_goalkeeper is not None:
            _act_goalkeeper_guard(
                context,
                role_goalkeeper,
                field_players,
                store,
                allow_active_response=False,
            )
        if offensive_striker is not None:
            _act_offensive_striker(offensive_striker)
        if front_partner is not None:
            _act_offensive_front_partner(front_partner)
        return

    mode_estimate = _estimate_open_play_mode(
        context,
        field_players,
        getattr(store, "open_play_mode", None),
    )
    open_play_mode = _update_open_play_mode(
        context, mode_estimate, store,
    )
    _draw_open_play_mode(context, store)

    assignment = _assign_open_play_roles(
        context,
        players,
        role_goalkeeper,
        open_play_mode,
        store,
    )
    offensive_striker = _get_assigned_player(
        available_players_by_id,
        assignment.offensive_striker_id,
    )
    front_partner = _get_assigned_player(
        available_players_by_id,
        assignment.front_partner_id,
    )
    defensive_presser = _get_assigned_player(
        available_players_by_id,
        assignment.defensive_presser_id,
    )
    defensive_protector = _get_assigned_player(
        available_players_by_id,
        assignment.defensive_protector_id,
    )

    if (
        assignment.availability == OpenPlayAvailability.DEGRADED_ONE
        and role_goalkeeper is not None
        and not field_players
    ):
        # 唯一可用者既然承担守门身份，就不再降级为场上球处理职责。
        _act_goalkeeper_guard(
            context,
            role_goalkeeper,
            field_players,
            store,
            allow_active_response=True,
        )
        return

    if role_goalkeeper is not None:
        _act_goalkeeper_guard(
            context,
            role_goalkeeper,
            field_players,
            store,
            allow_active_response=True,
        )

    if open_play_mode == OpenPlayMode.ATTACKING:
        _act_normal_attacking_shape(
            context,
            role_goalkeeper,
            offensive_striker,
            front_partner,
            store,
        )
        return
    if open_play_mode == OpenPlayMode.DEFENDING:
        _act_normal_defense(
            context,
            role_goalkeeper,
            defensive_presser,
            defensive_protector,
            store,
        )
        return

    _act_normal_contested_shape(
        context,
        role_goalkeeper,
        defensive_presser,
        defensive_protector,
        store,
    )


def _act_goalkeeper_guard(
    context: Context,
    goalkeeper: Player,
    field_players: list[Player],
    store,
    *,
    allow_active_response: bool,
) -> None:
    """统一守门入口；固定重启只允许保守 HOLD/TRACK。"""
    _act_goalkeeper_strategy(
        context,
        goalkeeper,
        field_players,
        store,
        allow_active_response=allow_active_response,
    )


def _update_ball_recovery_state(context: Context, store) -> bool:
    """更新球记忆,返回本帧是否已满足恢复正常策略的确认条件。"""
    ball = context.ball
    if ball is None:
        store.ball_visible_frames = 0
        if store.ball_lost_since is None:
            store.ball_lost_since = context.now
        return False

    store.last_ball_position = (ball.x, ball.y)
    store.last_ball_seen_at = ball.last_seen_at
    store.ball_visible_frames = min(
        store.ball_visible_frames + 1,
        BALL_REACQUIRE_FRAMES,
    )
    if store.ball_visible_frames < BALL_REACQUIRE_FRAMES:
        return False

    store.ball_lost_since = None
    store.ball_searcher = None
    return True


def _act_ball_recovery(
    context: Context,
    field_players: list[Player],
    goalkeeper: Player | None,
    store,
) -> None:
    """NORMAL 丢球恢复:一人定向扫场,其余保持守位或停止。"""
    if goalkeeper is not None:
        _act_goalkeeper_guard(
            context,
            goalkeeper,
            field_players,
            store,
            allow_active_response=False,
        )
    if not field_players:
        return

    active_ids = {player.id for player in field_players}
    preferred_searcher = getattr(store, "ball_searcher", None)
    if preferred_searcher not in active_ids:
        current_mode = getattr(store, "open_play_mode", None)
        if current_mode == OpenPlayMode.ATTACKING:
            preferred_searcher = getattr(
                store,
                "offensive_striker_id",
                None,
            )
        else:
            preferred_searcher = getattr(
                store,
                "defensive_presser_id",
                None,
            )
    if preferred_searcher not in active_ids:
        preferred_searcher = min(player.id for player in field_players)
    store.ball_searcher = preferred_searcher

    searcher = next(
        player for player in field_players if player.id == preferred_searcher
    )
    last_ball_position = getattr(store, "last_ball_position", None)
    last_ball_seen_at = getattr(store, "last_ball_seen_at", None)
    memory_is_fresh = (
        last_ball_position is not None
        and last_ball_seen_at is not None
        and context.now - last_ball_seen_at <= BALL_LAST_SEEN_MEMORY_SEC
    )
    search_target = last_ball_position if memory_is_fresh else None

    lost_since = getattr(store, "ball_lost_since", None)
    if lost_since is None:
        lost_since = context.now
        store.ball_lost_since = lost_since
    sweep_index = int(
        max(0.0, context.now - lost_since) / max(BALL_SEARCH_SWEEP_SEC, 1e-6)
    )
    base_direction = 1.0 if searcher.id % 2 == 0 else -1.0
    sweep_direction = base_direction if sweep_index % 2 == 0 else -base_direction

    searcher.action = (
        "ball_search:last_seen" if search_target is not None
        else "ball_search:sweep"
    )
    searcher.search_for_ball(search_target, sweep_direction)

    for player in field_players:
        if player is searcher:
            continue
        player.action = "ball_unknown:hold"
        player.stop()


def _act_our_kickoff(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    store,
) -> None:
    """OUR_KICKOFF:守门员留守,场上球员沿用现有开球入口。"""
    if not players:
        return

    field_players = [
        player for player in players if player is not goalkeeper
    ]
    if goalkeeper is not None:
        _act_goalkeeper_guard(
            context,
            goalkeeper,
            field_players,
            store,
            allow_active_response=False,
        )
    if not field_players:
        store.kickoff_taker = None
        return

    active_ids = {player.id for player in field_players}
    if store.prev_phase != Phase.OUR_KICKOFF or store.kickoff_taker not in active_ids:
        # 进入开球阶段，重新选择开球球员
        store.kickoff_taker = _select_closest_player_to_ball(
            context, field_players,
        ).id

    attacker_id = store.kickoff_taker
    attacker = next(
        (player for player in field_players if player.id == attacker_id),
        None,
    )
    if attacker is None:
        return

    attacker.action = "kickoff"
    attacker.reset_ball_handling_state()
    attacker.kick(0.1, KICK_POWER_OUR_KICKOFF)

    for player in field_players:
        if player is attacker:
            continue
        player.action = "stay"
        player.stop()


def _clamp_restart_target(
    context: Context,
    target: tuple[float, float],
) -> tuple[float, float]:
    """把重启等待点限制在场内,避免规则避让点落到边线之外。"""
    field_margin = 0.3
    half_length = max(0.0, context.field.length / 2.0 - field_margin)
    half_width = max(0.0, context.field.width / 2.0 - field_margin)
    return (
        clamp(target[0], -half_length, half_length),
        clamp(target[1], -half_width, half_width),
    )


def _project_target_outside_radius(
    target: tuple[float, float],
    center: tuple[float, float],
    minimum_distance: float,
    fallback_direction: tuple[float, float],
) -> tuple[float, float]:
    """目标落入禁入圆时,沿当前方向投影到圆外。"""
    offset_x = target[0] - center[0]
    offset_y = target[1] - center[1]
    current_distance = math.hypot(offset_x, offset_y)
    if current_distance >= minimum_distance:
        return target

    if current_distance > 1e-6:
        direction_x = offset_x / current_distance
        direction_y = offset_y / current_distance
    else:
        fallback_length = math.hypot(*fallback_direction)
        if fallback_length <= 1e-6:
            direction_x, direction_y = -1.0, 0.0
        else:
            direction_x = fallback_direction[0] / fallback_length
            direction_y = fallback_direction[1] / fallback_length

    return (
        center[0] + direction_x * minimum_distance,
        center[1] + direction_y * minimum_distance,
    )


def _prepare_restart_target(
    context: Context,
    preferred_target: tuple[float, float],
    *,
    stay_outside_center_circle: bool = False,
) -> tuple[float, float]:
    """生成场内、避球且可选中圈外的对方重启等待点。"""
    target = _clamp_restart_target(context, preferred_target)
    own_goal_x, own_goal_y = own_goal(context)

    # 反复投影可处理球不完全位于中点时两个禁入圆部分重叠的情况。
    for _projection_pass in range(4):
        if stay_outside_center_circle:
            target = _project_target_outside_radius(
                target,
                (0.0, 0.0),
                context.field.circle_radius + CIRCLE_MARGIN_M,
                (-1.0, 0.0),
            )
            target = _clamp_restart_target(context, target)

        ball = context.ball
        if ball is not None:
            target = _project_target_outside_radius(
                target,
                (ball.x, ball.y),
                OPPONENT_RESTART_AVOID_M,
                (own_goal_x - ball.x, own_goal_y - ball.y),
            )
            target = _clamp_restart_target(context, target)

    return target


def _walk_to_restart_target(
    context: Context,
    player: Player,
    target: tuple[float, float],
    action: str,
    *,
    stay_outside_center_circle: bool = False,
) -> None:
    """对方重启期间只执行避球、避机器人的安全走位。"""
    safe_target = _prepare_restart_target(
        context,
        target,
        stay_outside_center_circle=stay_outside_center_circle,
    )
    face = 0.0
    ball = context.ball
    if ball is not None:
        face = angle_to(player.pose.x, player.pose.y, ball.x, ball.y)

    player.action = action
    player.walk_to(
        safe_target,
        face=face,
        avoid_ball=True,
        avoid_robots=True,
    )


def _act_opp_kickoff(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
) -> None:
    """对方中场开球:守门并在中圈、球的合法距离外等待。"""
    if not players:
        return

    if goalkeeper is not None:
        _walk_to_restart_target(
            context,
            goalkeeper,
            own_goal_area_center(context),
            "opp_kickoff:guard",
            stay_outside_center_circle=True,
        )

    waiting_players = [
        player for player in players if player is not goalkeeper
    ]
    center_clearance = context.field.circle_radius + CIRCLE_MARGIN_M
    waiting_slots = [
        (-center_clearance - 0.4, 0.0),
        (-center_clearance - 1.2, 1.0),
        (-center_clearance - 1.2, -1.0),
    ]
    for slot_index, player in enumerate(waiting_players):
        target = waiting_slots[slot_index % len(waiting_slots)]
        _walk_to_restart_target(
            context,
            player,
            target,
            "opp_kickoff:avoid",
            stay_outside_center_circle=True,
        )


def _is_our_throw_in_context(context: Context) -> bool:
    """仅接受 GameController 明确报告的可执行我方界外球。"""
    game = context.game
    return (
        game is not None
        and game.state == GameState.PLAYING
        and not game.stopped
        and game.set_play == SetPlay.THROW_IN
        and game.kicking_team == context.team_id
    )


def _finish_throw_in_tactic(
    players: list[Player],
    store,
    outcome: ThrowInStage,
    reason: str,
) -> None:
    """释放固定职责，同时保留当前连续界外球上下文的消费锁存。"""
    state = getattr(store, "throw_in_state", None)
    if state is not None:
        state.stage = outcome
        state.terminal_reason = reason

    locked_player_ids = getattr(store, "locked_roles", None) or frozenset()
    for player in players:
        if player.id not in locked_player_ids:
            continue
        player.stop()
        player.action = f"throw_in:{outcome.value}:{reason}"

    store.active_tactic = None
    store.locked_roles = None
    store.tactic_roles = None
    store.throw_in_state = None
    store.throw_in_context_consumed = True
    store.throw_in_last_outcome = outcome.value
    store.throw_in_last_reason = reason
    _log.info("throw-in tactic %s reason=%s", outcome.value, reason)


def _consume_throw_in_context_without_tactic(store, reason: str) -> None:
    """人数或观测不足时消费当前上下文，禁止随后重新初始化。"""
    store.active_tactic = None
    store.locked_roles = None
    store.tactic_roles = None
    store.throw_in_state = None
    store.throw_in_context_consumed = True
    store.throw_in_last_outcome = ThrowInStage.ABORTED.value
    store.throw_in_last_reason = reason


def _hold_throw_in_context_for_retry(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    store,
    reason: str,
) -> None:
    """初始化所需观测暂不可用时安全等待，但不消费本次界外球。"""
    store.active_tactic = None
    store.locked_roles = None
    store.tactic_roles = None
    store.throw_in_state = None
    store.throw_in_context_consumed = False
    store.throw_in_last_outcome = "init_wait"
    store.throw_in_last_reason = reason

    field_players = [player for player in players if player is not goalkeeper]
    _guard_throw_in_goalkeeper(
        context,
        goalkeeper,
        field_players,
        store,
    )
    for player in field_players:
        player.stop()
        player.action = f"throw_in:init_wait:{reason}"


def _synchronize_throw_in_tactic_context(
    context: Context,
    players: list[Player],
    store,
) -> None:
    """裁判上下文变化时终止固定战术，并只在明确离开后解锁新事件。"""
    our_throw_in_context = _is_our_throw_in_context(context)
    if (
        getattr(store, "active_tactic", None) == "throw_in"
        and not our_throw_in_context
    ):
        reason = (
            "game_controller_unavailable"
            if context.game is None
            else "game_context_changed"
        )
        _finish_throw_in_tactic(
            players,
            store,
            ThrowInStage.ABORTED,
            reason,
        )

    # 暂时收不到裁判数据时保留 consumed，避免同一上下文恢复后重跑。
    if context.game is not None and not our_throw_in_context:
        store.throw_in_context_consumed = False


def _classify_throw_in_region(context: Context, origin_x: float) -> ThrowInRegion:
    half_length = max(context.field.length / 2.0, 1e-6)
    normalized_x = origin_x / half_length
    if normalized_x <= THROW_IN_BACKFIELD_MAX_X_RATIO:
        return ThrowInRegion.BACKFIELD
    if normalized_x >= THROW_IN_FRONTFIELD_MIN_X_RATIO:
        return ThrowInRegion.FRONTFIELD
    return ThrowInRegion.MIDFIELD


def _get_valid_opponent_positions(context: Context) -> list[tuple[float, float]]:
    return [
        (opponent.pose.x, opponent.pose.y)
        for opponent in context.opponents.values()
        if opponent.pose is not None
    ]


def _get_throw_in_lane_clearance(
    opponent_positions: list[tuple[float, float]],
    origin: tuple[float, float],
    target: tuple[float, float],
) -> float:
    segment_delta_x = target[0] - origin[0]
    segment_delta_y = target[1] - origin[1]
    segment_length_squared = (
        segment_delta_x * segment_delta_x
        + segment_delta_y * segment_delta_y
    )
    minimum_clearance = math.inf
    for opponent_x, opponent_y in opponent_positions:
        if segment_length_squared <= 1e-12:
            line_distance = dist(
                opponent_x,
                opponent_y,
                origin[0],
                origin[1],
            )
        else:
            projection = clamp(
                (
                    (opponent_x - origin[0]) * segment_delta_x
                    + (opponent_y - origin[1]) * segment_delta_y
                ) / segment_length_squared,
                0.0,
                1.0,
            )
            nearest_x = origin[0] + segment_delta_x * projection
            nearest_y = origin[1] + segment_delta_y * projection
            line_distance = dist(
                opponent_x,
                opponent_y,
                nearest_x,
                nearest_y,
            )
        minimum_clearance = min(minimum_clearance, line_distance)
    return minimum_clearance


def _get_throw_in_target_clearance(
    opponent_positions: list[tuple[float, float]],
    target: tuple[float, float],
) -> float:
    return min(
        (
            dist(target[0], target[1], opponent_x, opponent_y)
            for opponent_x, opponent_y in opponent_positions
        ),
        default=math.inf,
    )


def _constrain_throw_in_target(
    context: Context,
    origin: tuple[float, float],
    infield_y_direction: float,
    preferred_target: tuple[float, float],
) -> tuple[float, float] | None:
    """约束候选后重新检查向前、向内，拒绝 clamp 造成的退化。"""
    half_length = max(
        0.0,
        context.field.length / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    half_width = max(
        0.0,
        context.field.width / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    target = (
        clamp(preferred_target[0], -half_length, half_length),
        clamp(preferred_target[1], -half_width, half_width),
    )
    forward_progress = target[0] - origin[0]
    infield_progress = (target[1] - origin[1]) * infield_y_direction
    if (
        forward_progress < THROW_IN_MIN_FORWARD_PROGRESS_M
        or infield_progress < THROW_IN_MIN_INFIELD_PROGRESS_M
    ):
        return None
    return target


def _throw_in_line_crosses_own_goal_front(
    context: Context,
    origin: tuple[float, float],
    target: tuple[float, float],
) -> bool:
    """判断向前线路是否进入己方球门正前方保护矩形。"""
    own_goal_line_x = -context.field.length / 2.0
    protected_front_x = own_goal_line_x + THROW_IN_OWN_GOAL_FRONT_DEPTH_M
    if origin[0] > protected_front_x:
        return False

    segment_delta_x = target[0] - origin[0]
    if segment_delta_x <= 1e-6:
        return True
    protected_end_ratio = clamp(
        (protected_front_x - origin[0]) / segment_delta_x,
        0.0,
        1.0,
    )
    protected_end_y = origin[1] + (
        target[1] - origin[1]
    ) * protected_end_ratio
    protected_half_width = (
        context.field.goal_width / 2.0
        + THROW_IN_OWN_GOAL_FRONT_LATERAL_MARGIN_M
    )
    minimum_y = min(origin[1], protected_end_y)
    maximum_y = max(origin[1], protected_end_y)
    return (
        minimum_y <= protected_half_width
        and maximum_y >= -protected_half_width
    )


def _get_throw_in_region_offsets(
    region: ThrowInRegion,
) -> tuple[float, float]:
    if region == ThrowInRegion.BACKFIELD:
        return (
            THROW_IN_BACKFIELD_FORWARD_OFFSET_M,
            THROW_IN_BACKFIELD_INFIELD_OFFSET_M,
        )
    if region == ThrowInRegion.FRONTFIELD:
        return (
            THROW_IN_FRONTFIELD_FORWARD_OFFSET_M,
            THROW_IN_FRONTFIELD_INFIELD_OFFSET_M,
        )
    return (
        THROW_IN_MIDFIELD_FORWARD_OFFSET_M,
        THROW_IN_MIDFIELD_INFIELD_OFFSET_M,
    )


def _select_throw_in_short_pass_target(
    context: Context,
    origin: tuple[float, float],
    infield_y_direction: float,
    region: ThrowInRegion,
    receiver: Player | None,
    opponent_positions: list[tuple[float, float]],
) -> tuple[float, float] | None:
    forward_offset, infield_offset = _get_throw_in_region_offsets(region)
    offset_candidates = [
        (forward_offset, infield_offset),
        (forward_offset * 0.85, infield_offset * 1.25),
        (forward_offset * 1.20, infield_offset * 0.90),
    ]
    if receiver is not None and receiver.pose is not None:
        receiver_forward_offset = max(
            forward_offset * 0.75,
            receiver.pose.x - origin[0] + THROW_IN_RECEIVER_TRAIL_DISTANCE_M,
        )
        receiver_infield_offset = max(
            infield_offset * 0.75,
            (receiver.pose.y - origin[1]) * infield_y_direction
            + THROW_IN_RECEIVER_TRAIL_DISTANCE_M,
        )
        offset_candidates.append(
            (receiver_forward_offset, receiver_infield_offset),
        )

    ranked_candidates: list[
        tuple[float, float, float, float, tuple[float, float]]
    ] = []
    for candidate_forward, candidate_infield in offset_candidates:
        candidate = _constrain_throw_in_target(
            context,
            origin,
            infield_y_direction,
            (
                origin[0] + candidate_forward,
                origin[1] + infield_y_direction * candidate_infield,
            ),
        )
        if candidate is None:
            continue
        if (
            region == ThrowInRegion.BACKFIELD
            and _throw_in_line_crosses_own_goal_front(
                context,
                origin,
                candidate,
            )
        ):
            continue

        lane_clearance = _get_throw_in_lane_clearance(
            opponent_positions,
            origin,
            candidate,
        )
        target_clearance = _get_throw_in_target_clearance(
            opponent_positions,
            candidate,
        )
        if (
            lane_clearance < THROW_IN_SHORT_PASS_LANE_CLEARANCE_M
            or target_clearance < THROW_IN_TARGET_OPPONENT_CLEARANCE_M
        ):
            continue
        receiver_distance = (
            dist(
                receiver.pose.x,
                receiver.pose.y,
                candidate[0],
                candidate[1],
            )
            if receiver is not None and receiver.pose is not None
            else 0.0
        )
        ranked_candidates.append(
            (
                lane_clearance,
                target_clearance,
                -receiver_distance,
                candidate[0] - origin[0],
                candidate,
            ),
        )

    if not ranked_candidates:
        return None
    return max(ranked_candidates, key=lambda item: item[:4])[4]


def _build_throw_in_long_clearance_target(
    context: Context,
    origin: tuple[float, float],
    infield_y_direction: float,
) -> tuple[float, float] | None:
    candidates = (
        (
            origin[0] + THROW_IN_BACKFIELD_LONG_DISTANCE_M,
            origin[1]
            + infield_y_direction * THROW_IN_BACKFIELD_LONG_INFIELD_M,
        ),
        (
            origin[0] + THROW_IN_BACKFIELD_LONG_DISTANCE_M * 0.90,
            origin[1]
            + infield_y_direction * THROW_IN_BACKFIELD_LONG_INFIELD_M * 1.20,
        ),
    )
    opponent_positions = _get_valid_opponent_positions(context)
    ranked_candidates: list[
        tuple[float, float, float, tuple[float, float]]
    ] = []
    for preferred_target in candidates:
        target = _constrain_throw_in_target(
            context,
            origin,
            infield_y_direction,
            preferred_target,
        )
        if target is None:
            continue
        if _throw_in_line_crosses_own_goal_front(context, origin, target):
            continue
        ranked_candidates.append(
            (
                _get_throw_in_lane_clearance(
                    opponent_positions,
                    origin,
                    target,
                ),
                _get_throw_in_target_clearance(
                    opponent_positions,
                    target,
                ),
                target[0] - origin[0],
                target,
            ),
        )
    if not ranked_candidates:
        return None
    return max(ranked_candidates, key=lambda item: item[:3])[3]


def _throw_in_backfield_requires_long_clearance(
    context: Context,
    origin: tuple[float, float],
    kicker: Player,
    short_target: tuple[float, float] | None,
) -> bool:
    opponent_positions = _get_valid_opponent_positions(context)
    if not opponent_positions or short_target is None:
        return True

    origin_pressure = min(
        dist(origin[0], origin[1], opponent_x, opponent_y)
        for opponent_x, opponent_y in opponent_positions
    )
    kicker_pressure = min(
        dist(
            kicker.pose.x,
            kicker.pose.y,
            opponent_x,
            opponent_y,
        )
        for opponent_x, opponent_y in opponent_positions
    )
    lane_clearance = _get_throw_in_lane_clearance(
        opponent_positions,
        origin,
        short_target,
    )
    return (
        origin_pressure <= THROW_IN_PRESSURE_DISTANCE_M
        or kicker_pressure <= THROW_IN_PRESSURE_DISTANCE_M
        or lane_clearance <= THROW_IN_PRESSURE_LANE_CLEARANCE_M
    )


def _get_throw_in_short_pass_power(pass_distance: float) -> float:
    distance_span = (
        THROW_IN_SHORT_PASS_FAR_DISTANCE_M
        - THROW_IN_SHORT_PASS_NEAR_DISTANCE_M
    )
    if distance_span <= 1e-6:
        return THROW_IN_SHORT_PASS_POWER_MIN
    interpolation_ratio = clamp(
        (
            pass_distance - THROW_IN_SHORT_PASS_NEAR_DISTANCE_M
        ) / distance_span,
        0.0,
        1.0,
    )
    return (
        THROW_IN_SHORT_PASS_POWER_MIN
        + (
            THROW_IN_SHORT_PASS_POWER_MAX
            - THROW_IN_SHORT_PASS_POWER_MIN
        ) * interpolation_ratio
    )


def _get_throw_in_receiver_target(
    context: Context,
    pass_target: tuple[float, float],
    pass_direction: tuple[float, float],
) -> tuple[float, float]:
    half_length = max(
        0.0,
        context.field.length / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    half_width = max(
        0.0,
        context.field.width / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    return (
        clamp(
            pass_target[0]
            - pass_direction[0] * THROW_IN_RECEIVER_TRAIL_DISTANCE_M,
            -half_length,
            half_length,
        ),
        clamp(
            pass_target[1]
            - pass_direction[1] * THROW_IN_RECEIVER_TRAIL_DISTANCE_M,
            -half_width,
            half_width,
        ),
    )


def _select_throw_in_kicker_stage_target(
    context: Context,
    origin: tuple[float, float],
    infield_y_direction: float,
    pass_direction: tuple[float, float],
    kicker: Player,
) -> tuple[float, float] | None:
    half_length = max(
        0.0,
        context.field.length / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    half_width = max(
        0.0,
        context.field.width / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    preferred_candidates = (
        (
            origin[0]
            - pass_direction[0] * THROW_IN_KICKER_STAGE_DISTANCE_M,
            origin[1]
            - pass_direction[1] * THROW_IN_KICKER_STAGE_DISTANCE_M,
        ),
        (
            origin[0]
            - pass_direction[0] * THROW_IN_KICKER_STAGE_DISTANCE_M * 0.65,
            origin[1]
            + infield_y_direction * THROW_IN_KICKER_STAGE_INFIELD_M,
        ),
        (
            origin[0]
            - pass_direction[0] * THROW_IN_KICKER_STAGE_DISTANCE_M * 0.25,
            origin[1]
            + infield_y_direction * THROW_IN_KICKER_STAGE_INFIELD_M * 1.15,
        ),
    )
    legal_candidates: list[tuple[float, float]] = []
    for preferred_target in preferred_candidates:
        target = (
            clamp(preferred_target[0], -half_length, half_length),
            clamp(preferred_target[1], -half_width, half_width),
        )
        target_ball_distance = dist(
            target[0],
            target[1],
            origin[0],
            origin[1],
        )
        if target_ball_distance < THROW_IN_KICKER_STAGE_DISTANCE_M * 0.65:
            continue
        legal_candidates.append(target)

    if not legal_candidates or kicker.pose is None:
        return None
    return min(
        legal_candidates,
        key=lambda target: dist(
            kicker.pose.x,
            kicker.pose.y,
            target[0],
            target[1],
        ),
    )


def _build_throw_in_plan(
    context: Context,
    kicker: Player,
    receiver: Player | None,
) -> tuple[ThrowInTacticState | None, str | None]:
    ball = context.ball
    if ball is None:
        return None, "ball_unavailable"

    origin = (ball.x, ball.y)
    half_width = context.field.width / 2.0
    touchline_distance = abs(half_width - abs(origin[1]))
    if touchline_distance > THROW_IN_TOUCHLINE_TOLERANCE_M:
        return None, "ball_not_near_touchline"

    infield_y_direction = -1.0 if origin[1] >= 0.0 else 1.0
    region = _classify_throw_in_region(context, origin[0])
    opponent_positions = _get_valid_opponent_positions(context)
    short_target = _select_throw_in_short_pass_target(
        context,
        origin,
        infield_y_direction,
        region,
        receiver,
        opponent_positions,
    )
    long_clearance = (
        region == ThrowInRegion.BACKFIELD
        and _throw_in_backfield_requires_long_clearance(
            context,
            origin,
            kicker,
            short_target,
        )
    )
    if long_clearance:
        pass_target = _build_throw_in_long_clearance_target(
            context,
            origin,
            infield_y_direction,
        )
    else:
        pass_target = short_target
    if pass_target is None:
        return None, "no_valid_pass_target"

    pass_delta_x = pass_target[0] - origin[0]
    pass_delta_y = pass_target[1] - origin[1]
    pass_distance = math.hypot(pass_delta_x, pass_delta_y)
    if pass_distance <= 1e-6:
        return None, "degenerate_pass_target"
    pass_direction = (
        pass_delta_x / pass_distance,
        pass_delta_y / pass_distance,
    )
    kicker_stage_target = _select_throw_in_kicker_stage_target(
        context,
        origin,
        infield_y_direction,
        pass_direction,
        kicker,
    )
    if kicker_stage_target is None:
        return None, "no_valid_kicker_stage"

    receiver_target = (
        _get_throw_in_receiver_target(
            context,
            pass_target,
            pass_direction,
        )
        if receiver is not None
        else None
    )
    pass_power = (
        THROW_IN_BACKFIELD_LONG_KICK_POWER
        if long_clearance
        else _get_throw_in_short_pass_power(pass_distance)
    )
    return (
        ThrowInTacticState(
            stage=ThrowInStage.POSITIONING,
            region=region,
            started_at=context.now,
            stage_started_at=context.now,
            origin=origin,
            infield_y_direction=infield_y_direction,
            long_clearance=long_clearance,
            pass_target=pass_target,
            receiver_target=receiver_target,
            kicker_stage_target=kicker_stage_target,
            pass_direction=pass_direction,
            pass_power=pass_power,
        ),
        None,
    )


def _draw_throw_in_plan(state: ThrowInTacticState) -> None:
    from .framework import debugdraw

    debugdraw.point(
        state.origin[0],
        state.origin[1],
        rgb=(1.0, 0.8, 0.0),
        scale=0.18,
        ns="throw_in_origin",
    )
    debugdraw.line(
        [state.origin, state.pass_target],
        rgb=(0.2, 1.0, 0.4),
        ns="throw_in_pass",
    )
    debugdraw.point(
        state.pass_target[0],
        state.pass_target[1],
        rgb=(0.2, 1.0, 0.4),
        scale=0.18,
        ns="throw_in_pass_target",
    )
    debugdraw.point(
        state.kicker_stage_target[0],
        state.kicker_stage_target[1],
        rgb=(1.0, 0.4, 0.1),
        scale=0.16,
        ns="throw_in_kicker_stage",
    )
    if state.receiver_target is not None:
        debugdraw.point(
            state.receiver_target[0],
            state.receiver_target[1],
            rgb=(0.2, 0.7, 1.0),
            scale=0.16,
            ns="throw_in_receiver_target",
        )


def _draw_throw_in_status(context: Context, store) -> None:
    """把界外球内部锁存状态画出来，便于手动判断卡住原因。"""
    from .framework import debugdraw

    state = getattr(store, "throw_in_state", None)
    stage = (
        state.stage.value
        if isinstance(state, ThrowInTacticState)
        else "none"
    )
    debugdraw.text(
        0.0,
        context.field.width / 2.0 + 0.6,
        (
            f"throw_in active={getattr(store, 'active_tactic', None)} "
            f"stage={stage} "
            "consumed="
            f"{getattr(store, 'throw_in_context_consumed', False)} "
            f"reason={getattr(store, 'throw_in_last_reason', None)}"
        ),
        rgb=(0.2, 1.0, 1.0),
        ns="throw_in_state",
    )


def _get_throw_in_roles(
    players: list[Player],
    goalkeeper: Player | None,
    roles: ThrowInRoleAssignment,
) -> tuple[Player | None, Player | None, Player | None, str | None]:
    available_by_id = {player.id: player for player in players}
    current_goalkeeper_id = goalkeeper.id if goalkeeper is not None else None
    if roles.goalkeeper_id != current_goalkeeper_id:
        return None, None, None, "goalkeeper_changed"

    locked_goalkeeper = available_by_id.get(roles.goalkeeper_id)
    kicker = available_by_id.get(roles.kicker_id)
    receiver = available_by_id.get(roles.receiver_id)
    if roles.kicker_id is not None and kicker is None:
        return locked_goalkeeper, None, receiver, "kicker_unavailable"
    if roles.receiver_id is not None and receiver is None:
        return locked_goalkeeper, kicker, None, "receiver_unavailable"
    if roles.goalkeeper_id is not None and locked_goalkeeper is None:
        return None, kicker, receiver, "goalkeeper_unavailable"
    return locked_goalkeeper, kicker, receiver, None


def _guard_throw_in_goalkeeper(
    context: Context,
    goalkeeper: Player | None,
    field_players: list[Player],
    store,
) -> None:
    if goalkeeper is None:
        return
    _act_goalkeeper_guard(
        context,
        goalkeeper,
        field_players,
        store,
        allow_active_response=False,
    )
    goalkeeper.action = "throw_in:goalkeeper:hold"


def _get_throw_in_ball_progress(
    state: ThrowInTacticState,
    ball_x: float,
    ball_y: float,
) -> tuple[float, float, float]:
    movement_x = ball_x - state.origin[0]
    movement_y = ball_y - state.origin[1]
    movement_distance = math.hypot(movement_x, movement_y)
    forward_progress = (
        movement_x * state.pass_direction[0]
        + movement_y * state.pass_direction[1]
    )
    lateral_deviation = abs(
        movement_x * state.pass_direction[1]
        - movement_y * state.pass_direction[0]
    )
    return movement_distance, forward_progress, lateral_deviation


def _get_throw_in_rear_support_target(
    context: Context,
    state: ThrowInTacticState,
    receiver: Player | None,
) -> tuple[float, float]:
    ball = context.ball
    if receiver is not None and receiver.pose is not None:
        reference_x, reference_y = receiver.pose.x, receiver.pose.y
    elif ball is not None:
        reference_x, reference_y = ball.x, ball.y
    else:
        reference_x, reference_y = state.pass_target

    half_length = max(
        0.0,
        context.field.length / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    half_width = max(
        0.0,
        context.field.width / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    return (
        clamp(
            reference_x - THROW_IN_REAR_SUPPORT_DISTANCE_M,
            -half_length,
            half_length,
        ),
        clamp(
            reference_y
            + state.infield_y_direction * THROW_IN_REAR_SUPPORT_INFIELD_M,
            -half_width,
            half_width,
        ),
    )


def _move_throw_in_receiver_to_target(
    context: Context,
    receiver: Player,
    target: tuple[float, float],
    action: str,
    *,
    avoid_ball: bool,
) -> None:
    ball = context.ball
    face = (
        angle_to(receiver.pose.x, receiver.pose.y, ball.x, ball.y)
        if ball is not None and receiver.pose is not None
        else None
    )
    receiver.walk_to(
        target,
        face=face,
        avoid_ball=avoid_ball,
        avoid_robots=True,
        arrive_dist=THROW_IN_RECEIVER_READY_DISTANCE_M,
    )
    receiver.action = action


def _abort_throw_in(
    players: list[Player],
    store,
    reason: str,
) -> None:
    _finish_throw_in_tactic(
        players,
        store,
        ThrowInStage.ABORTED,
        reason,
    )


def _upgrade_throw_in_to_long_clearance(
    context: Context,
    state: ThrowInTacticState,
    kicker: Player,
) -> bool:
    long_target = _build_throw_in_long_clearance_target(
        context,
        state.origin,
        state.infield_y_direction,
    )
    if long_target is None:
        return False
    delta_x = long_target[0] - state.origin[0]
    delta_y = long_target[1] - state.origin[1]
    target_distance = math.hypot(delta_x, delta_y)
    if target_distance <= 1e-6:
        return False
    pass_direction = (delta_x / target_distance, delta_y / target_distance)
    stage_target = _select_throw_in_kicker_stage_target(
        context,
        state.origin,
        state.infield_y_direction,
        pass_direction,
        kicker,
    )
    if stage_target is None:
        return False

    state.long_clearance = True
    state.pass_target = long_target
    state.pass_direction = pass_direction
    state.pass_power = THROW_IN_BACKFIELD_LONG_KICK_POWER
    state.kicker_stage_target = stage_target
    state.receiver_target = _get_throw_in_receiver_target(
        context,
        long_target,
        pass_direction,
    )
    return True


def _act_throw_in_positioning(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    kicker: Player,
    receiver: Player | None,
    state: ThrowInTacticState,
    store,
) -> None:
    ball = context.ball
    if ball is None:
        _abort_throw_in(players, store, "ball_unavailable")
        return
    if (
        dist(ball.x, ball.y, state.origin[0], state.origin[1])
        > THROW_IN_BALL_ORIGIN_TOLERANCE_M
    ):
        _hold_throw_in_context_for_retry(
            context,
            players,
            goalkeeper,
            store,
            "ball_moved_before_kick",
        )
        return
    if (
        context.now - state.stage_started_at
        >= THROW_IN_POSITIONING_TIMEOUT_SEC
    ):
        _abort_throw_in(players, store, "positioning_timeout")
        return

    if (
        state.region == ThrowInRegion.BACKFIELD
        and not state.long_clearance
        and _throw_in_backfield_requires_long_clearance(
            context,
            state.origin,
            kicker,
            state.pass_target,
        )
    ):
        if not _upgrade_throw_in_to_long_clearance(context, state, kicker):
            _abort_throw_in(players, store, "long_clearance_unavailable")
            return

    field_players = [player for player in players if player is not goalkeeper]
    _guard_throw_in_goalkeeper(
        context,
        goalkeeper,
        field_players,
        store,
    )

    kick_direction = angle_to(
        state.origin[0],
        state.origin[1],
        state.pass_target[0],
        state.pass_target[1],
    )
    kicker.walk_to(
        state.kicker_stage_target,
        face=kick_direction,
        avoid_ball=True,
        avoid_robots=True,
        arrive_dist=THROW_IN_KICKER_READY_DISTANCE_M,
    )
    kicker_position_ready = dist(
        kicker.pose.x,
        kicker.pose.y,
        state.kicker_stage_target[0],
        state.kicker_stage_target[1],
    ) <= THROW_IN_KICKER_READY_DISTANCE_M
    kicker_heading_ready = abs(normalize_angle(
        kick_direction - kicker.pose.theta,
    )) <= THROW_IN_KICKER_READY_HEADING_RAD

    receiver_ready = receiver is None or state.long_clearance
    if receiver is not None and state.receiver_target is not None:
        _move_throw_in_receiver_to_target(
            context,
            receiver,
            state.receiver_target,
            "throw_in:receiver:position",
            avoid_ball=True,
        )
        receiver_ready = (
            state.long_clearance
            or dist(
                receiver.pose.x,
                receiver.pose.y,
                state.receiver_target[0],
                state.receiver_target[1],
            ) <= THROW_IN_RECEIVER_READY_DISTANCE_M
        )

    solo = receiver is None
    if kicker_position_ready and kicker_heading_ready and receiver_ready:
        kicker.action = (
            "throw_in:solo:position"
            if solo
            else "throw_in:kicker:wait"
        )
        state.stage = ThrowInStage.KICKING
        state.stage_started_at = context.now
        state.kicking_start_ball_position = (ball.x, ball.y)
        return
    kicker.action = (
        "throw_in:solo:position"
        if solo
        else "throw_in:kicker:position"
    )


def _act_throw_in_kicking(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    kicker: Player,
    receiver: Player | None,
    state: ThrowInTacticState,
    store,
) -> None:
    field_players = [player for player in players if player is not goalkeeper]
    _guard_throw_in_goalkeeper(
        context,
        goalkeeper,
        field_players,
        store,
    )
    if receiver is not None and state.receiver_target is not None:
        _move_throw_in_receiver_to_target(
            context,
            receiver,
            state.receiver_target,
            "throw_in:receiver:position",
            avoid_ball=True,
        )

    ball = context.ball
    if ball is None:
        _abort_throw_in(players, store, "ball_unavailable")
        return
    movement, progress, lateral_deviation = _get_throw_in_ball_progress(
        state,
        ball.x,
        ball.y,
    )
    if not state.kick_command_started:
        if movement > THROW_IN_BALL_ORIGIN_TOLERANCE_M:
            _hold_throw_in_context_for_retry(
                context,
                players,
                goalkeeper,
                store,
                "ball_moved_before_kick",
            )
            return
        kicker.directed_restart_kick(state.pass_target, state.pass_power)
        state.kick_command_started = True
        if state.long_clearance:
            kicker.action = (
                "throw_in:solo:clear"
                if receiver is None
                else "throw_in:kicker:clear"
            )
        elif receiver is None:
            kicker.action = "throw_in:solo:pass"
        else:
            kicker.action = "throw_in:kicker:pass"
        return
    if (
        movement >= THROW_IN_BALL_START_CONFIRM_DISTANCE_M
        and progress <= THROW_IN_WRONG_DIRECTION_PROGRESS_M
    ):
        _abort_throw_in(players, store, "ball_moved_wrong_direction")
        return
    if (
        movement >= THROW_IN_BALL_START_CONFIRM_DISTANCE_M
        and lateral_deviation
        > THROW_IN_PASS_SEVERE_LATERAL_TOLERANCE_M
    ):
        _abort_throw_in(players, store, "initial_pass_severely_deviated")
        return
    if (
        progress >= THROW_IN_BALL_START_CONFIRM_DISTANCE_M
        and lateral_deviation <= THROW_IN_PASS_LATERAL_TOLERANCE_M
    ):
        kicker.stop()
        if receiver is None or state.long_clearance:
            _finish_throw_in_tactic(
                players,
                store,
                ThrowInStage.COMPLETE,
                "restart_kick_confirmed",
            )
            return
        state.stage = ThrowInStage.PASS_IN_FLIGHT
        state.stage_started_at = context.now
        return
    if context.now - state.stage_started_at >= THROW_IN_KICKING_TIMEOUT_SEC:
        _abort_throw_in(players, store, "kick_no_expected_movement")
        return

    kicker.directed_restart_kick(state.pass_target, state.pass_power)
    if state.long_clearance:
        kicker.action = (
            "throw_in:solo:clear"
            if receiver is None
            else "throw_in:kicker:clear"
        )
    elif receiver is None:
        kicker.action = "throw_in:solo:pass"
    else:
        kicker.action = "throw_in:kicker:pass"


def _opponent_has_throw_in_arrival_advantage(
    context: Context,
    receiver: Player,
) -> bool:
    ball = context.ball
    if ball is None or receiver.pose is None:
        return False
    opponent_ball_distance = min(
        (
            dist(
                opponent.pose.x,
                opponent.pose.y,
                ball.x,
                ball.y,
            )
            for opponent in context.opponents.values()
            if opponent.pose is not None
        ),
        default=math.inf,
    )
    receiver_ball_distance = dist(
        receiver.pose.x,
        receiver.pose.y,
        ball.x,
        ball.y,
    )
    return (
        opponent_ball_distance <= THROW_IN_OPPONENT_BALL_DISTANCE_M
        and opponent_ball_distance + THROW_IN_OPPONENT_ARRIVAL_ADVANTAGE_M
        < receiver_ball_distance
    )


def _build_throw_in_follow_up_target(
    context: Context,
    state: ThrowInTacticState,
) -> tuple[float, float] | None:
    ball = context.ball
    if ball is None:
        return None
    half_length = max(
        0.0,
        context.field.length / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    half_width = max(
        0.0,
        context.field.width / 2.0 - THROW_IN_FIELD_MARGIN_M,
    )
    target = (
        clamp(
            ball.x + THROW_IN_FOLLOW_UP_FORWARD_DISTANCE_M,
            -half_length,
            half_length,
        ),
        clamp(
            ball.y + state.infield_y_direction * 0.35,
            -half_width,
            half_width,
        ),
    )
    if target[0] - ball.x < THROW_IN_MIN_FORWARD_PROGRESS_M:
        return None
    return target


def _act_throw_in_pass_in_flight(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    kicker: Player,
    receiver: Player,
    state: ThrowInTacticState,
    store,
) -> None:
    field_players = [player for player in players if player is not goalkeeper]
    _guard_throw_in_goalkeeper(
        context,
        goalkeeper,
        field_players,
        store,
    )
    ball = context.ball
    if ball is None:
        _abort_throw_in(players, store, "ball_unavailable")
        return

    support_target = _get_throw_in_rear_support_target(
        context,
        state,
        receiver,
    )
    kicker.move_to_position(support_target)
    kicker.action = "throw_in:kicker:rear_support"

    movement, progress, lateral_deviation = _get_throw_in_ball_progress(
        state,
        ball.x,
        ball.y,
    )
    receive_target = (
        (ball.x, ball.y)
        if progress >= THROW_IN_BALL_START_CONFIRM_DISTANCE_M
        else state.receiver_target
    )
    if receive_target is not None:
        _move_throw_in_receiver_to_target(
            context,
            receiver,
            receive_target,
            "throw_in:receiver:receive",
            avoid_ball=False,
        )

    if (
        movement >= THROW_IN_BALL_START_CONFIRM_DISTANCE_M
        and progress <= THROW_IN_WRONG_DIRECTION_PROGRESS_M
    ):
        _abort_throw_in(players, store, "pass_reversed")
        return
    if lateral_deviation > THROW_IN_PASS_SEVERE_LATERAL_TOLERANCE_M:
        _abort_throw_in(players, store, "pass_severely_deviated")
        return
    if _opponent_has_throw_in_arrival_advantage(context, receiver):
        _abort_throw_in(players, store, "opponent_arrival_advantage")
        return
    if (
        context.now - state.stage_started_at
        >= THROW_IN_PASS_IN_FLIGHT_TIMEOUT_SEC
    ):
        _abort_throw_in(players, store, "pass_in_flight_timeout")
        return

    receiver_ball_distance = dist(
        receiver.pose.x,
        receiver.pose.y,
        ball.x,
        ball.y,
    )
    kicker_ball_distance = dist(
        kicker.pose.x,
        kicker.pose.y,
        ball.x,
        ball.y,
    )
    receiver_has_geometric_advantage = (
        progress >= THROW_IN_RECEIVE_MIN_PROGRESS_M
        and receiver_ball_distance <= THROW_IN_RECEIVER_BALL_DISTANCE_M
        and receiver_ball_distance + THROW_IN_RECEIVER_CLOSER_MARGIN_M
        < kicker_ball_distance
        and lateral_deviation <= THROW_IN_PASS_LATERAL_TOLERANCE_M
    )
    if not receiver_has_geometric_advantage:
        return

    state.follow_up_target = _build_throw_in_follow_up_target(context, state)
    state.follow_up_start_ball_position = None
    state.follow_up_action_started = False
    state.stage = ThrowInStage.FOLLOW_UP
    state.stage_started_at = context.now


def _act_throw_in_follow_up(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    kicker: Player,
    receiver: Player,
    state: ThrowInTacticState,
    store,
) -> None:
    field_players = [player for player in players if player is not goalkeeper]
    _guard_throw_in_goalkeeper(
        context,
        goalkeeper,
        field_players,
        store,
    )
    ball = context.ball
    if ball is None:
        _abort_throw_in(players, store, "ball_unavailable")
        return

    kicker.move_to_position(
        _get_throw_in_rear_support_target(context, state, receiver),
    )
    kicker.action = "throw_in:kicker:rear_support"

    follow_up_start = state.follow_up_start_ball_position
    follow_up_forward_progress = (
        0.0
        if follow_up_start is None
        else ball.x - follow_up_start[0]
    )
    if (
        state.follow_up_action_started
        and follow_up_forward_progress >= THROW_IN_FOLLOW_UP_PROGRESS_M
    ):
        _finish_throw_in_tactic(
            players,
            store,
            ThrowInStage.COMPLETE,
            "follow_up_advanced",
        )
        return
    if context.now - state.stage_started_at >= THROW_IN_FOLLOW_UP_TIMEOUT_SEC:
        _abort_throw_in(players, store, "follow_up_timeout")
        return

    opponent_goal_target = opponent_goal(context)
    goal_distance = dist(
        ball.x,
        ball.y,
        opponent_goal_target[0],
        opponent_goal_target[1],
    )
    should_shoot = (
        state.region == ThrowInRegion.FRONTFIELD
        and goal_distance <= THROW_IN_FRONT_SHOOT_DISTANCE_M
    )
    if should_shoot:
        receiver.attack()
        receiver.action = "throw_in:receiver:shoot"
        if receiver.is_kicking and not state.follow_up_action_started:
            state.follow_up_action_started = True
            state.follow_up_start_ball_position = (ball.x, ball.y)
        return

    if state.follow_up_target is None:
        state.follow_up_target = _build_throw_in_follow_up_target(
            context,
            state,
        )
    if state.follow_up_target is None:
        _abort_throw_in(players, store, "follow_up_target_unavailable")
        return
    if not state.follow_up_action_started:
        state.follow_up_start_ball_position = (ball.x, ball.y)
        state.follow_up_action_started = True
    receiver.directed_restart_kick(
        state.follow_up_target,
        THROW_IN_FOLLOW_UP_ADVANCE_POWER,
    )
    receiver.action = "throw_in:receiver:advance"


def _act_consumed_throw_in_fallback(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    store,
) -> None:
    """同一界外球已结束时保持安全站位，绝不重新进入固定流程。"""
    _draw_throw_in_status(context, store)
    field_players = [player for player in players if player is not goalkeeper]
    _guard_throw_in_goalkeeper(
        context,
        goalkeeper,
        field_players,
        store,
    )
    reason = getattr(store, "throw_in_last_reason", None) or "unknown"
    for player in field_players:
        player.stop()
        player.action = f"throw_in:consumed:{reason}"


def _act_our_throw_in(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    store,
) -> None:
    """执行我方界外球专用固定战术及同上下文防重启。"""
    _draw_throw_in_status(context, store)
    if not _is_our_throw_in_context(context):
        if getattr(store, "active_tactic", None) == "throw_in":
            _abort_throw_in(players, store, "game_context_changed")
        return

    if getattr(store, "active_tactic", None) != "throw_in":
        if getattr(store, "throw_in_context_consumed", False):
            _act_consumed_throw_in_fallback(
                context,
                players,
                goalkeeper,
                store,
            )
            return

        field_players = [
            player for player in players if player is not goalkeeper
        ]
        if not field_players:
            reason = (
                "zero_available_players"
                if not players
                else "goalkeeper_only_safe_hold"
            )
            _consume_throw_in_context_without_tactic(store, reason)
            _guard_throw_in_goalkeeper(
                context,
                goalkeeper,
                field_players,
                store,
            )
            return
        if context.ball is None:
            _hold_throw_in_context_for_retry(
                context,
                players,
                goalkeeper,
                store,
                "ball_unavailable",
            )
            return

        kicker = _select_closest_player_to_ball(context, field_players)
        receiver = next(
            (player for player in field_players if player is not kicker),
            None,
        )
        roles = ThrowInRoleAssignment(
            goalkeeper_id=(
                goalkeeper.id if goalkeeper is not None else None
            ),
            kicker_id=kicker.id,
            receiver_id=receiver.id if receiver is not None else None,
            available_player_ids_at_entry=tuple(
                player.id for player in players
            ),
        )
        state, planning_error = _build_throw_in_plan(
            context,
            kicker,
            receiver,
        )
        if state is None:
            planning_failure_reason = planning_error or "planning_failed"
            if planning_failure_reason in _THROW_IN_TRANSIENT_INIT_FAILURES:
                _hold_throw_in_context_for_retry(
                    context,
                    players,
                    goalkeeper,
                    store,
                    planning_failure_reason,
                )
            else:
                _consume_throw_in_context_without_tactic(
                    store,
                    planning_failure_reason,
                )
                _act_consumed_throw_in_fallback(
                    context,
                    players,
                    goalkeeper,
                    store,
                )
            return

        store.active_tactic = "throw_in"
        store.tactic_roles = roles
        store.locked_roles = frozenset(
            role_id
            for role_id in (
                roles.goalkeeper_id,
                roles.kicker_id,
                roles.receiver_id,
            )
            if role_id is not None
        )
        store.throw_in_state = state
        store.throw_in_context_consumed = True
        store.throw_in_last_outcome = None
        store.throw_in_last_reason = None

    roles = getattr(store, "tactic_roles", None)
    state = getattr(store, "throw_in_state", None)
    if not isinstance(roles, ThrowInRoleAssignment) or not isinstance(
        state,
        ThrowInTacticState,
    ):
        _abort_throw_in(players, store, "tactic_state_invalid")
        return

    locked_goalkeeper, kicker, receiver, role_error = _get_throw_in_roles(
        players,
        goalkeeper,
        roles,
    )
    if role_error is not None:
        _abort_throw_in(players, store, role_error)
        return
    if kicker is None:
        _abort_throw_in(players, store, "kicker_missing")
        return
    if context.now - state.started_at >= THROW_IN_TOTAL_TIMEOUT_SEC:
        _abort_throw_in(players, store, "total_timeout")
        return

    _draw_throw_in_plan(state)
    if state.stage == ThrowInStage.POSITIONING:
        _act_throw_in_positioning(
            context,
            players,
            locked_goalkeeper,
            kicker,
            receiver,
            state,
            store,
        )
        return
    if state.stage == ThrowInStage.KICKING:
        _act_throw_in_kicking(
            context,
            players,
            locked_goalkeeper,
            kicker,
            receiver,
            state,
            store,
        )
        return
    if state.stage == ThrowInStage.PASS_IN_FLIGHT:
        if receiver is None:
            _abort_throw_in(players, store, "receiver_missing")
            return
        _act_throw_in_pass_in_flight(
            context,
            players,
            locked_goalkeeper,
            kicker,
            receiver,
            state,
            store,
        )
        return
    if state.stage == ThrowInStage.FOLLOW_UP:
        if receiver is None:
            _abort_throw_in(players, store, "receiver_missing")
            return
        _act_throw_in_follow_up(
            context,
            players,
            locked_goalkeeper,
            kicker,
            receiver,
            state,
            store,
        )
        return
    _abort_throw_in(players, store, "unexpected_stage")


def _act_our_set_play(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    store,
) -> None:
    """OUR_SET_PLAY:按定位球类型分派, TODO：加入自己的逻辑。默认为 _act_normal"""
    set_play = get_set_play_type(context)
    if set_play == SetPlay.THROW_IN:
        _act_our_throw_in(
            context,
            players,
            goalkeeper,
            store,
        )
        return

    field_players = [
        player for player in players if player is not goalkeeper
    ]
    if not field_players:
        if goalkeeper is not None:
            _act_goalkeeper_guard(
                context,
                goalkeeper,
                field_players,
                store,
                allow_active_response=False,
            )
        return

    if set_play == SetPlay.CORNER_KICK:
        _act_normal(
            context, players, goalkeeper, store, allow_ball_search=False,
        )
        return
    if set_play == SetPlay.GOAL_KICK:
        _act_normal(
            context, players, goalkeeper, store, allow_ball_search=False,
        )
        return
    _act_normal(
        context, players, goalkeeper, store, allow_ball_search=False,
    )


def _act_opp_set_play(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    _store,
) -> None:
    """对方定位球:守门、封堵和保护均在球的合法距离外执行。"""
    if not players:
        return

    own_goal_x, own_goal_y = own_goal(context)
    if goalkeeper is not None:
        _walk_to_restart_target(
            context,
            goalkeeper,
            own_goal_area_center(context),
            "opp_restart:guard",
        )

    field_players = [
        player for player in players if player is not goalkeeper
    ]
    ball = context.ball
    if ball is None:
        for player in field_players:
            player.action = "opp_restart:stop_no_ball"
            player.stop()
        return

    route_to_goal_x = own_goal_x - ball.x
    route_to_goal_y = own_goal_y - ball.y
    route_length = math.hypot(route_to_goal_x, route_to_goal_y)
    if route_length <= 1e-6:
        route_direction_x, route_direction_y = -1.0, 0.0
    else:
        route_direction_x = route_to_goal_x / route_length
        route_direction_y = route_to_goal_y / route_length

    block_target = _prepare_restart_target(
        context,
        (
            ball.x + route_direction_x * OPPONENT_RESTART_AVOID_M,
            ball.y + route_direction_y * OPPONENT_RESTART_AVOID_M,
        ),
    )
    blocker = min(
        field_players,
        key=lambda player: dist(
            player.pose.x,
            player.pose.y,
            block_target[0],
            block_target[1],
        ),
        default=None,
    )
    if blocker is not None:
        _walk_to_restart_target(
            context,
            blocker,
            block_target,
            "opp_restart:block",
        )

    protecting_players = [
        player for player in field_players if player is not blocker
    ]
    protect_distance = min(
        max(OPPONENT_RESTART_AVOID_M + 0.8, 2.3),
        route_length,
    )
    central_protect_x = ball.x + route_direction_x * protect_distance
    central_protect_y = (
        ball.y + route_direction_y * protect_distance
    ) * 0.35

    for protect_index, player in enumerate(protecting_players):
        if protect_index == 0:
            lateral_offset = 0.0
        else:
            offset_rank = (protect_index + 1) // 2
            offset_direction = 1.0 if protect_index % 2 == 1 else -1.0
            lateral_offset = offset_direction * offset_rank * 0.8

        _walk_to_restart_target(
            context,
            player,
            (central_protect_x, central_protect_y + lateral_offset),
            "opp_restart:protect",
        )


def _act_ready(
    context: Context,
    players: list[Player],
    goalkeeper: Player | None,
    store,
) -> None:
    """READY:当前守门员进门前位置,场上机器人使用既有保守站位。"""
    if not players:
        return

    game = context.game
    our_kickoff = game is not None and game.kicking_team == context.team_id
    field = context.field
    if goalkeeper is not None:
        goalkeeper.action = (
            "ready:temp_goalkeeper"
            if goalkeeper.id == getattr(store, "temporary_goalkeeper_id", None)
            else "ready:goalkeeper"
        )
        goalkeeper.walk_to(
            own_goal_area_center(context),
            face=0.0,
            avoid_ball=True,
            avoid_robots=True,
        )

    field_players = [
        player for player in players if player is not goalkeeper
    ]
    if our_kickoff:
        ready_targets = [
            (-field.circle_radius, 0.0),
            (-0.5, field.circle_radius + 2.0),
        ]
    else:
        ready_targets = [
            (-field.circle_radius - 0.5, 0.0),
            (-field.length / 2.0 + field.penalty_area_length, 0.0),
        ]

    for player, target in zip(field_players, ready_targets):
        player.action = "ready"
        player.walk_to(
            target,
            face=0.0,
            avoid_ball=True,
            avoid_robots=True,
        )

    for player in field_players[len(ready_targets):]:
        player.action = "ready:hold"
        player.stop()



# ======================================================================
# 战场可视化 —— 显示球位置 + 球员到球的距离,画到 ROS 可视化
# ======================================================================

def _draw_teammate_marker(p: Player) -> None:
    """我方队员可视化:红色。踢球中→方块,否则→球体。

    每帧对所有球员统一调用(不受 phase/判罚/就绪影响)。标签两行:
    - 上:编号 + 当前高层动作(``p.action``),踢球中追加 ``[KICK]``。
    - 通过形状(方块 vs 球体)再次区分是否进入 kick 状态。
    """
    from .framework import debugdraw

    if p.pose is None:
        return
    red = (1.0, 0.2, 0.2)
    if p.is_kicking:
        debugdraw.cube(p.pose.x, p.pose.y, rgb=red, scale=0.38, ns="teammate")
    else:
        debugdraw.point(p.pose.x, p.pose.y, rgb=red, scale=0.3, ns="teammate")
    kick_tag = " [KICK]" if p.is_kicking else ""
    label = f"{p.id}:{p.action}{kick_tag}"
    debugdraw.text(p.pose.x, p.pose.y, label, rgb=(1.0, 0.9, 0.6), ns="teammate_id")


def _analyze_and_draw(context: Context, players: list[Player], store) -> None:
    """每帧:计算球员到球的距离,画可视化。

    不再依赖 analysis 模块;距离改为基于球当前位置。
    """
    from .framework import debugdraw

    ball = context.ball

    # 球不可见:无可视化
    if ball is None:
        return

    # 1. 画球当前位置(绿色点)
    debugdraw.point(ball.x, ball.y, rgb=(0.0, 1.0, 0.0), scale=0.2, ns="ball_current")

    # 2. 球员到球的距离:我方(红标签)+ 敌方(蓝标签)
    for p in players:
        if p.pose is None:
            continue
        d = dist(p.pose.x, p.pose.y, ball.x, ball.y)
        debugdraw.text(
            p.pose.x + 0.3, p.pose.y - 0.3, f"{d:.1f}m",
            rgb=(1.0, 0.6, 0.6), ns="dist_ours",
        )
    for r in context.opponents.values():
        if r.pose is None:
            continue
        d = dist(r.pose.x, r.pose.y, ball.x, ball.y)
        debugdraw.text(
            r.pose.x + 0.3, r.pose.y - 0.3, f"{d:.1f}m",
            rgb=(0.6, 0.6, 1.0), ns="dist_opp",
        )
