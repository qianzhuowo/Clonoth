## Output rule

Generate a single valid YAML object with two root-level keys:
- `mindful_prelude`: brief visual planning notes
- `images`: complete TAG descriptors for scene/characters/actions

Do not wrap YAML in Markdown fences. Do not add commentary before or after YAML.

```yaml
mindful_prelude:
  visual_plan:
    reasoning: 识别了几个视觉核心时刻，以及为什么这样拆分
    moments:
      - moment: 1
        char_count: Xgirls, Yboys
        known_chars:
          - 已知角色名
        unknown_chars:
          - 未知角色说明
        composition: 构图类型/氛围/光影
images:
  - index: 1
    size_label: 竖图   # 只能是：横图 / 竖图 / 方图
    anchor: 用户请求或剧情中的5-20字关键短语；简单请求可直接摘取核心名词
    scene: (sfw/nsfw), (角色关系+位置), (视角构图), (背景+光影)
    characters:
      - name: 角色名
        danbooru: character_name_(series) 或 name_(original) 或 ""
        type: girl|boy|woman|man|other      # 仅未知角色必须写；已知角色可省略
        appear: 发长, 发色, 瞳色, 身体特征Tags # 仅未知角色必须写；已知角色可省略
        costume: 完整服装/配饰Tags
        action: 姿态, 动作, 表情Tags
        interact: source#动作 或 target#动作 或 mutual#动作；没有互动写 ""
        uc: 只对该角色生效的排除Tags；不要写通用质量负面
        center: A1~E5 网格坐标，默认 C3
```

## Scene Composition 规则
- 分级：sfw / `0.5::nsfw::` / nsfw。
- 数量关系：solo, duo, hetero, yuri, trio, group。
- 视角：third-person view, pov, from front, from behind, from above, from below, from side。
- 区域：upper body, lower body, full body, cowboy shot, portrait。
- 远近：close-up, mid shot, wide shot。
- 焦点：face focus, depth of field, blurry background。
- 光影：warm lighting, backlighting, rim lighting, sidelighting, dramatic shadows。

## Character Prompt 规则
- 主角详述，配角简化。
- 无角色时，物品/服装/建筑等作为主体详述，独立使用一个 Character 槽，type 写 other。
- 已知角色：输出 name + danbooru + costume + action + interact + uc + center；不要输出 type/appear，除非需要覆盖默认外貌。
- 未知角色：必须输出 type + appear + costume + action + interact + uc + center。
- 服装必须描述款式、颜色、材质/细节、穿着状态。
- 动作必须是静态瞬间，避免连续动作堆叠。

## Per-character UC 规则
`uc` 字段只写该角色的互斥/不可见/不要出现的 tag，例如：
- 不戴眼镜：`glasses`
- 摘帽：`hat`
- from behind 时不可见正脸：`face, eye contact`
不要写 `bad anatomy`, `worst quality`, `lowres` 等通用质量负面。

## 5×5 网格坐标
画面分为 5×5 网格，列 A-E（左→右），行 1-5（上→下）：
- C3 = 画面中心（默认/单人位置）
- 坐标可重叠（拥抱/亲吻）
- 坐标应反映角色在画面中的实际位置

## Tag 配额
每张图总计约 70~100 个 tag。Scene 约 25 个，主角 Character 约 45 个，配角更少。
