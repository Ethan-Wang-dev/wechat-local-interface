# Skill Gallery

这里收集可以使用本地微信数据完成具体任务的 Skill。Skill 可以是一个 `SKILL.md` 工作流、一个脚本、一个本地模型流程，也可以是它们的组合。

当前 Gallery 仍在搭建中。提交新的 Skill 前，请先复制 [`_template`](./_template/) 并填写说明。暂时不要求使用固定分类，也不要求所有 Skill 使用同一种输出格式。

## Skill 需要说明什么

每个 Skill 至少应该说明：

- 它解决什么问题；
- 用户可以怎样触发它；
- 它需要读取哪些数据或文件；
- 它可能写入哪些位置；
- 它会生成什么结果或文件；
- 是否需要模型、网络或额外依赖；
- 一个不包含真实隐私数据的示例。
- 如果读取微信数据，必须声明并执行刷新前置步骤；离线快照需要声明跳过原因。

可选的 `skill.yaml` 用于声明这些能力，字段规范见 [`docs/skill-architecture.md`](../docs/skill-architecture.md)。能力清单用于发现和提示，不限制 Skill 的实现方式。

### 统一刷新规则

所有读取 `local_wechat_interface` 的 Skill 默认都要先执行一次 Mac 增量解密，确认
快照稳定后再查询。新 Skill 的 `skill.yaml` 应包含 `refresh_before_read: true`，
`SKILL.md` 应说明刷新命令、失败处理和快照版本记录方式。只处理既有产物的渲染
Skill 可以设置为 `false`。

## 组合方式

Skill 可以直接给用户最终结果，也可以生成 [`wechat.content.v1`](../schemas/wechat.content.v1.json) 内容封装，再交给 HTML、图片、Markdown 或 PDF 渲染 Skill。

```text
数据查询 → 分析 Skill → 内容或文件 → 渲染 / 导出 Skill
```

渲染器只是可复用工具。分析 Skill 也可以直接生成自己需要的 HTML、图片或其他文件。

## 当前目录

`_template/` 是提交模板。新的 Skill 放在这里后，Gallery 会逐步补充示例、预览和自动检查。
