"""Built-in prompt presets, seeded on first run.

These are ``builtin=True`` and cannot be deleted - only reset or duplicated.
The copy below matches the product brief verbatim so the shipped defaults are
the reviewed ones.
"""

from __future__ import annotations

from app.prompts.models import PromptPreset

CONTINUE_DEVELOPMENT = """继续执行当前已经确定的目标和计划。

先检查当前对话、现有项目文件、Git 状态、任务清单和最近工作结果，准确确认上一轮已经完成到哪里。

不要重新规划已经确定的方案。

不要重复已经完成的工作。

从上一次因为额度、会话或执行中断的位置继续。

优先完成当前仍未完成的：

- 代码修改
- 调试
- 测试
- 构建
- 部署
- 验收
- 文档收尾

发现普通技术问题时自行排查、修复并继续推进。

只有遇到必须由用户决定的问题、账号权限、安全授权或无法替用户选择的产品决策时才暂停。

当前阶段完成后，继续执行既定后续计划。"""

CONTINUE_AND_FIX = """继续当前开发任务。

先定位上一轮真实停止位置。

随后继续实现尚未完成的功能。

开发过程中：

发现报错 → 定位根因
发现测试失败 → 修复并重新测试
发现回归 → 添加回归测试
发现实现与需求不一致 → 直接修正

不要因为普通报错停止等待用户。

每完成一个阶段必须运行相应测试。

只有达到当前阶段验收标准以后才能进入下一阶段。"""

TEST_AND_DELIVER = """继续当前项目，但本轮重点不是新增功能，而是完成测试、修复和交付验收。

检查：

- 未完成测试
- failing tests
- E2E
- 配置
- 打包
- Windows 启动
- 日志
- 安全状态
- 回归问题

任何验收项失败：

继续修复。

全部关键验收通过之后才允许宣布完成。"""

AUTONOMOUS = """继续当前项目，并尽可能自主推进。

不要因为普通技术选择频繁询问用户。

存在多个合理实现时，优先选择：

稳定
简单
可维护
低依赖
可测试
兼容现有架构

自行完成：

分析
修改
测试
修复
重新测试
提交前检查

除非出现真正需要用户决策的阻塞，否则持续推进。"""

CURRENT_STAGE_ONLY = """只继续当前已经开始的阶段。

不要扩大范围。

不要增加新的非必要功能。

先完成当前阶段剩余任务。

然后执行当前阶段全部验收标准。

如果有失败：

继续修复。

当前阶段全部通过后暂停，并输出简洁验收报告。"""


def builtin_presets() -> list[PromptPreset]:
    now = ""
    return [
        PromptPreset(
            id="continue-default",
            name="继续既定开发计划",
            description="从额度中断的位置继续开发",
            content=CONTINUE_DEVELOPMENT,
            builtin=True,
            favorite=True,
        ),
        PromptPreset(
            id="continue-fix",
            name="继续开发并自动修复",
            description="继续实现并自动修复报错、测试与回归",
            content=CONTINUE_AND_FIX,
            builtin=True,
        ),
        PromptPreset(
            id="test-deliver",
            name="测试与验收收尾",
            description="聚焦测试、修复与交付验收",
            content=TEST_AND_DELIVER,
            builtin=True,
        ),
        PromptPreset(
            id="autonomous",
            name="自主持续推进",
            description="在稳定/简单/可维护前提下尽量自主推进",
            content=AUTONOMOUS,
            builtin=True,
        ),
        PromptPreset(
            id="current-stage",
            name="只继续当前阶段",
            description="只完成当前阶段，不做范围扩张",
            content=CURRENT_STAGE_ONLY,
            builtin=True,
        ),
    ]


DEFAULT_PRESET_ID = "continue-default"
