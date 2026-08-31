# WorkAMA Skills 集成说明

> 生成于 2026-08-28，适配 `mattpocock/skills` + `nexu-io/open-design` 至 WorkAMA 基线

## 已安装清单

### 项目级 `.agents/skills/` (19 项，已恢复 + 新增)

| 技能 | 来源 | 用途 |
|------|------|------|
| brand-guidelines | open-design (anthropic 官方) | 品牌色/字体一致性 |
| design-review | open-design | 设计走查与反 AI-slop 检查 |
| frontend-design | open-design / anthropics | 生产级前端界面与交互状态 |
| platform-design | open-design | 控制台/平台设计规范 |
| redesign-skill | taste-skill (Leonxlnx) | 存量 UI 审计与高级化改造 |
| canvas-design | open-design | 画布/编辑器类设计 |
| artifacts-builder | open-design | Web artifacts 构建 |
| shadcn-ui | open-design | shadcn 组件体系 |
| design-md | open-design | DESIGN.md 设计契约 |
| impeccable-design-polish | open-design | 细节打磨 |
| mp-tdd | mattpocock/engineering/tdd | 测试驱动 |
| mp-diagnosing-bugs | mattpocock 诊断回路 | 难修 bug/性能回归 |
| mp-code-review | mattpocock/code-review | Standards+Spec 双轴评审 |
| mp-implement | mattpocock/implement | 基于 spec/tickets 实现 |
| mp-research | mattpocock/research | 高可信源调研并落盘 |
| mp-wayfinder | mattpocock/wayfinder | 大型任务的 issue 地图 |
| mp-to-spec | mattpocock/to-spec | 需求转规范 |
| mp-prototype | mattpocock/prototype | 快速原型 |
| mp-domain-modeling | mattpocock/domain-modeling | 领域建模与深模块设计 |

### 全局 `$CODEX_HOME/skills/` (38 项)

- 包含上述 open-design 核心 6 项（frontend-design/brand-guidelines/design-review/platform-design/canvas-design/redesign-skill 等）
- 包含 mattpocock 工程化 12 项（tdd/code-review/diagnosing-bugs/implement/research/wayfinder/to-spec/prototype 等）
- 包含 openai curated: figma, pdf, playwright, playwright-interactive, screenshot, security-* , gh-* 等

## 与 WorkAMA 的适配点

- **DESIGN.md 已就绪**: 根 `DESIGN.md` 与 `apps/web/DESIGN.md` 为 Precision Operational 方向（ink-on-stone, accent #6d5efc），所有 frontend-design/open-design 工作流以此为 brand contract，禁止紫蓝渐变/glass-card 滥用。
- **端口/容器铁律**: 20200-20299 / workama-* 前缀，不受技能引入影响。
- **配置中心**: 可运维配置走 `/admin/platform-config` 热生效，非 `.env`，技能新增配置需同步 SCHEMA+前端+测试（AGENTS §10.17）。
- **前端端点运行时注入**: web/miniapp 经 `/config.js` 注入，同镜像跨环境（AGENTS §10.19），技能生成的前端制品不得硬编码端点。

## 建议使用方式

- 新界面/落地页: `frontend-design` + `brand-guidelines` + `DESIGN.md`
- 存量控制台打磨: `redesign-skill` + `design-review` + `impeccable-design-polish`
- 复杂后端/网关: `mp-domain-modeling` + `mp-tdd` + `mp-diagnosing-bugs`
- 大型需求: `mp-to-spec` -> `mp-wayfinder` -> `mp-implement` -> `mp-code-review`

## 验证

- `make docs-check` / `make contract-check` 仍通过（设计契约一致性）
- `pnpm --filter @workama/web test && tsc --noEmit` 前端门禁不受影响
