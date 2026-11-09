# 创作间：Web 交互原型

这是一版独立的 HTML/CSS/JavaScript 设计原型，不依赖 Node 构建。直接用浏览器打开 `index.html`，或在项目根目录执行：

```powershell
python -m http.server 8080 --bind 127.0.0.1 --directory web/prototype
```

然后访问 `http://127.0.0.1:8080`。

页面包括创作工作台、内容归档、创作画像、发布账号、技能与模型。可以操作新建任务、选择选题、保存新版本、批准、模拟提交、人工登记回执、查看历史和修改画像。所有任务都是演示数据，只保存在当前浏览器的 `xhs-studio-prototype-v1` localStorage 中。

原型不会调用模型、不访问真实账号、不上传素材、不向平台发布，也不会修改 Python 服务或 `data/store.db`。图片只用于当前页面预览，刷新后需重新选择。模型设置只保存服务商、模型 ID 和密钥环境变量名称，不接收真实密钥。右上角按钮可重置演示。

视觉采用暖白纸面、深绿导航和朱红主操作，中文衬线标题搭配正文。Google Fonts 可用时加载 Noto 字体，断网时使用本地中文字体；封面图由内联 SVG 绘制。

正式接入方案、API 和状态设计见 [Web 前端设计](../../docs/web-frontend.md)。
