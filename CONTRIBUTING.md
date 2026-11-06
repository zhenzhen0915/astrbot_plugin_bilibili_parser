# 参与开发

感谢帮助改进 Bilibili 视频解析、动态与直播订阅插件。提交问题前，请先确认使用的是最新版本，并附上 AstrBot 与 NapCat 版本、复现步骤和必要的脱敏日志。请勿上传 Cookie、QQ 登录信息、群成员资料或订阅数据库。

本仓库的推荐结构是 `astrbot_plugin_bilibili_parser/` 插件源码与 `tests/` 测试目录并列。使用 Python 3.12，安装 AstrBot、插件 `requirements.txt` 中的依赖、`ruff` 和 `pytest`。从仓库根目录运行：

```bash
python -m ruff check astrbot_plugin_bilibili_parser tests
python -m ruff format --check astrbot_plugin_bilibili_parser tests
python -m unittest discover -s tests -p 'test_*.py' -q
```

修改 QQ 指令时，请覆盖原始 OneBot `At` 消息段与 AstrBot 的 `GreedyStr` 参数形式。修改日历时，请覆盖北京时间跨日、跨月、30 天窗口和进行中场次。公开测试只使用模拟账号和接口响应，不要调用真实群发送接口。

提交拉取请求时，请说明用户可见的行为、变更原因和验证结果；如有数据库迁移或配置变化，请在说明中明确升级路径。代码以 [GPL-3.0-only](LICENSE) 发布，提交内容也应能按该许可证分发。项目图片素材的公开授权状态需由仓库所有者确认。
