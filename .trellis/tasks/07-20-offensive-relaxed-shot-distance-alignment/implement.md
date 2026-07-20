# 放宽进攻射门条件与远距离调整 - Implementation Plan

## Checklist

1. 阅读任务和相关规范。
2. 定位 `Player.attack()` 中射门触发距离和对齐角度条件。
3. 新增进攻专用近/中距离宽松角度和远距离保守角度参数。
4. 在进攻逻辑中按球场 X 位置选择射门对齐阈值。
5. 人工检查 diff，确认未修改防守 presser、defensive clear、守门员逻辑。
6. 按项目规则不运行 build/test/lint/type-check/format/仿真/IDE diagnostics。

## Manual Verification Suggestions

- 中场和前场近球时，观察是否更容易直接大脚。
- 后场或远距离球路明显不正时，观察是否仍先调整。
- 稍偏球仍应沿动态目标向场内修正。
- 防守抢断、解围、守门员行为应无变化。
