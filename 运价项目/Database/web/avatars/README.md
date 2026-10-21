# 账号头像预设

`av2-w01-1.png` 至 `av2-m05-3.png` 是使用 react-nice-avatar 1.5.0 离线生成的 30 张 256px 静态 PNG。按女/男各五个人物、每人日常/眼镜/出行三种造型组织；同一人物保留脸型、五官、发色和背景。`av-01.png` 至 `av-30.png` 是旧版资源，仅供已选用户继续使用，不再列为新预设。设计素材来源为 Nice Avatar / Micah 的 Avatar Illustration System；软件 MIT 授权文本见 `LICENSE.react-nice-avatar.txt`。项目运行不加载 React、生成器或外部头像服务。

生成配置在 `Database/ops/build_avatar_presets.cjs`；它是制作工具，不是服务器依赖。需在隔离的临时构建目录安装 `react-nice-avatar@1.5.0`、`react@18.3.1`、`react-dom@18.3.1`，并提供 Playwright 路径和 Chrome 路径后运行。脚本只生成 `av2-` 新版资源，不覆盖旧版编号；今后修改现有图像前应检查已选账号的视觉变化。

数据库只在本人 `preferences_json` 保存 `avatar_id` 或 `null`；图片文件不写入数据库。静态资源地址为 `/assets/avatars/{avatar_id}.png`，服务端只允许新旧共 60 个精确文件名。未选择头像时继续显示用户名首字。男女标签只是选图分类，不推断用户的实际性别、民族、岗位或业务能力。
