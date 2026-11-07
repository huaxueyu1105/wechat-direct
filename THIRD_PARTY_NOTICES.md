# 第三方组件与素材

本项目自有代码采用 MIT。以下组件与素材保留各自权利，根目录 LICENSE 不改变其许可。

- `plugin/runtime/vendor/wechat_db.py` 来自 [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)，采用 Apache License 2.0。完整许可证保存在同目录 `LICENSE.wechatauto`。本地修改把上游包级日志替换为标准 Python logging；仅使用只读数据库模块。
- Python 依赖通过 requirements 文件安装，其许可证和元数据随构建产物中的 `*.dist-info` 保留；包括 cryptography、cffi、pycparser、pefile、zstandard、Pillow，以及可选分析依赖 capstone。源码仓库不收录其二进制包。
- 微信名称、标识及内置表情编码属于其各自权利人；`plugin/runtime/builtin-expressions.json` 保存用于互操作的名称与编码映射。项目未附带微信客户端程序、表情图片或用户添加的私人素材。
- 原生驱动由本项目 `native/direct_bridge.c` 编译，版本适配配置位于 `native/targets/`。Zig 工具链需自行安装，不随源码发布。
