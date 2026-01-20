# Proactive Renewal System

轻量级服务订阅续费提醒工具，支持多币种、剩余价值计算、TG/邮件提醒与 Docker 一键部署。

## 主要功能

- 订阅管理：名称、分类、金额、货币、到期日、续费地址、提醒天数、备注
- 续费周期：天/周/季/半年/月/年/两年/三年/四年/五年
- 汇率转换：多币种金额自动折合人民币显示
- 到期提醒：提前 N 天提醒，支持 TG/邮件通道
- 自动顺延：到期后按续费周期自动延后下一到期日
- 密码访问：首次访问需要密码，登录后 Cookie 保存 30 天
- 导入导出：支持 CSV 批量导入与导出

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app
```

访问 `http://localhost:8000`，默认密码 `123456`，进入后请在“设置”里修改。

## Docker 一键部署

```bash
docker compose up -d --build
```

访问 `http://localhost:8000`。

数据默认保存到 `./data`。

## 提醒通道配置

### Telegram

- 创建 Bot：@BotFather
- 获取 Chat ID：通过 @userinfobot 或在群内添加 bot 后读取更新
- 设置完成后点击“测试 TG”验证

### 邮件 (SMTP)

- 填写 SMTP Host / Port / User / Password / Sender
- 勾选 STARTTLS
- 点击“测试邮件”验证

## 汇率服务

默认使用 `https://open.er-api.com/v6/latest/CNY`。

如需更换 API，可在“设置”中更新地址。系统每天 03:00 自动更新汇率，也可以手动更新。

## 说明

- 提醒任务每天 09:00 执行一次（服务内置调度器）。
- 如果使用多进程部署，请确保只启用一个调度器实例。
