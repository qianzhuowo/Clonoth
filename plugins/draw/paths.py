"""drawtools 文件定位：私有文件优先，模板兜底。

Why: tools/drawtools/ 下的用户配置（settings.yaml、character_tags.yaml、
prompts/novelai/*.md）是 gitignore 的私有文件，仓库只版本化 *.example.* 模板；
tools/drawtools/common.py / config_io.py 用同一条规则读取。插件运行在 engine
进程里，不能 import tools/（工具是独立子进程协议，不是引擎可依赖的包），所以
这里只复刻“目录位置 + 私有优先/模板兜底”这一条规则，且本插件内只此一处。
How: 给定 drawtools 下的相对路径，存在则用私有文件，否则用 ``<stem>.example<suffix>``。
运行期只读，不创建用户文件。
"""
from __future__ import annotations

from pathlib import Path

# 与 tools/drawtools/common.py 的 DRAWTOOLS_DIR 对应（相对工作区根）。
DRAWTOOLS_REL = Path("tools") / "drawtools"


def drawtools_file(workspace_root: Path | str, rel_path: str) -> Path | None:
    """返回 drawtools 下 rel_path 的私有文件；不存在则返回 example 模板；都没有返回 None。"""
    private = Path(workspace_root) / DRAWTOOLS_REL / rel_path
    if private.is_file():
        return private
    example = private.with_name(f"{private.stem}.example{private.suffix}")
    if example.is_file():
        return example
    return None
