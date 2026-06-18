"""
数据目录解析（单一事实来源）

LOCAL 模式下所有持久化数据(config.toml / token.json / api_keys.json /
缓存 / 统计 / IP 黑名单等)都落在 data 目录下。该目录默认是 `<项目根>/data`,
可通过环境变量 `SERVER_DATA_DIR` 自定义。

为什么用环境变量而非配置项:storage 必须先知道 data 目录才能定位
`config.toml`——配置文件本身就住在 data 目录里。这是 bootstrap 阶段的问题,
必须在配置加载之前解析,因此与 `SERVER_STORAGE_TYPE` 同层走环境变量。

注意:`main.py` 在导入任何 app 模块之前已执行 `load_dotenv()`,
因此本模块在 import 时即可读到 `SERVER_DATA_DIR`。
"""

import os
from pathlib import Path

# core -> app -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_data_dir() -> Path:
    """解析 data 目录。

    - 未设置 `SERVER_DATA_DIR`:返回 `<项目根>/data`(向后兼容)。
    - 绝对路径:原样使用。
    - 相对路径:锚定到项目根(与默认值一致,避免 cwd 歧义,Docker 友好)。
    """
    custom = os.getenv("SERVER_DATA_DIR", "").strip()
    if not custom:
        return PROJECT_ROOT / "data"
    p = Path(custom).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p)


# 模块级常量:在首次 import 时解析一次(此时 .env 已加载)。
DATA_DIR = get_data_dir()
