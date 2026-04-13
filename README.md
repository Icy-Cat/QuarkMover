# 夸克转存助手 (QuarkMover)

一键转存夸克网盘分享文件到你自己的网盘，生成属于你的新分享链接。

## 下载

前往 [Releases](../../releases) 页面，根据你的系统下载对应版本：

| 系统 | 下载文件 |
|------|---------|
| Windows | `QuarkMover-Windows.exe` |
| macOS (Intel) | `QuarkMover-macOS-Intel` |
| macOS (Apple Silicon) | `QuarkMover-macOS-ARM` |
| Linux | `QuarkMover-Linux` |

## 使用方法

### 第一步：打开工具

- **Windows**：双击 `QuarkMover-Windows.exe`
- **macOS**：终端运行 `chmod +x QuarkMover-macOS-* && ./QuarkMover-macOS-*`
- **Linux**：终端运行 `chmod +x QuarkMover-Linux && ./QuarkMover-Linux`

工具会自动在浏览器中打开操作页面。

### 第二步：扫码登录

首次使用会自动弹出登录窗口，用夸克 App 扫描二维码即可。登录状态会保存，下次打开无需重新登录（约几天后过期需重新扫码）。

### 第三步：开始转存

1. 复制别人的夸克分享链接（如 `pan.quark.cn/s/xxxxx`）
2. 粘贴到输入框
3. 点击「一键生成」
4. 右侧会显示你自己的新分享链接，点「一键复制」即可

## 进阶功能

- **二创模式**：切换到「完整模式」，粘贴推文内容 + 夸克链接，工具会用 AI 改写文案（需在设置中填写 DeepSeek API Key）
- **提取码**：在设置中开启「随机提取码」，生成的分享链接会带 4 位提取码
- **有效期**：可选永久 / 1天 / 7天 / 30天
- **主题**：支持深色/浅色/跟随系统

## 系统要求

- 需要本机安装 Chrome 或 Edge 浏览器（用于扫码登录）
- Windows 10+、macOS 10.15+、Ubuntu 20.04+

## 常见问题

**Q: 提示"未找到浏览器"？**
A: 请安装 [Google Chrome](https://www.google.com/chrome/) 或使用系统自带的 Edge 浏览器。

**Q: 转存失败，提示"未登录"？**
A: 登录态已过期，点击右上角「扫码登录夸克」重新登录。

**Q: 二创功能报错？**
A: 需要在设置中填写有效的 DeepSeek API Key（[获取地址](https://platform.deepseek.com/)）。

## 公众号与交流群

想获取最新教程、工具更新和使用交流，可以扫码：

### 公众号

![公众号二维码](./static/qr_gongzhonghao.webp)

### 交流群

![交流群二维码](./static/qr_group.webp)
