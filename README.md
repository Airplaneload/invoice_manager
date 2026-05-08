# 发票自动下载管理器

一款基于 Python + Tkinter 开发的 Windows 桌面应用程序，用于自动从 QQ 邮箱扫描和下载发票附件。

## 主要功能

- **邮件扫描**：自动扫描 QQ 邮箱中的发票邮件
- **选择性下载**：支持预览邮件内容，选择性下载发票附件
- **发票识别**：自动识别发票信息（金额、日期、税额等）
- **商品明细提取**：支持从发票中提取商品明细
- **自动监控**：支持定时自动检查新发票邮件
- **系统托盘**：支持最小化到系统托盘
- **数据导出**：支持导出发票汇总信息

## 系统要求

- Windows 10/11 操作系统
- 无需安装 Python（已打包为 exe 可执行文件）

## 快速开始

1. 下载本仓库中的 `invoice_manager_gui` 文件夹
2. 进入文件夹，双击 `invoice_manager_gui.exe` 运行程序
3. 输入 QQ 邮箱地址和授权码
4. 点击"连接"按钮连接邮箱
5. 点击"扫描"按钮开始扫描发票邮件

## 配置说明

### QQ 邮箱授权码获取

1. 登录 [QQ 邮箱](https://mail.qq.com/)
2. 进入 设置 → 账户
3. 找到 POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务
4. 开启 IMAP/SMTP 服务
5. 按提示发送短信获取授权码

### config.json 配置

程序目录下的 `config.json` 文件包含以下配置：

```json
{
    "email": "your_email@qq.com",
    "auth_code": "your_auth_code",
    "download_dir": "./invoices",
    "auto_download": false,
    "check_interval": 300
}
```

## 更新日志

### v1.0
- 初始版本发布
- 支持 QQ 邮箱 IMAP 连接
- 支持发票附件扫描和下载
- 支持发票信息自动识别
- 支持系统托盘功能

## 许可证

MIT License

## 问题反馈

如遇问题或有任何建议，请通过 GitHub Issues 反馈。
