# 进攻射门动态调整偏球 - Implementation Plan

## Checklist

1. 阅读任务和相关规范。
2. 定位进攻射门目标计算与小力带球分支。
3. 新增进攻专用偏球调整阈值。
4. 增加进攻动态目标计算：正球瞄门，稍偏向场内调整，极偏保持快速处理。
5. 将动态目标接入 `Player.attack()` 和 `plan_offensive_shot()`。
6. 人工检查 diff，确认未修改防守 presser、defensive clear、守门员逻辑。
7. 按项目规则不运行 build/test/lint/type-check/format/仿真/IDE diagnostics。

## Manual Verification Suggestions

- 中路或较正位置：观察是否仍快速射门。
- 靠边但未到角球区：观察是否向场内带正/调整，减少直接出界。
- 对方角球区附近：观察是否不长时间纠结调整。
- 防守抢断、解围、守门员行为应无变化。
