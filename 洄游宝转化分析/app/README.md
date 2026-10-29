# 洄游宝三层经营工作台

Python 3.12 + Django 5.2 + Waitress，SQLite 本地持久化。包含商机到报价、注册到首交两阶段诊断；运营、业务、CEO三视角；核实、部门权限、资源请求、实际介入与双轮复盘；CRM只读提供方入口及原文件导入。

## 安装与启动（Windows）

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe manage.py migrate --noinput
.venv\Scripts\python.exe manage.py bootstrap
.venv\Scripts\python.exe manage.py load_business_cases
.venv\Scripts\python.exe run_local.py
```

访问 http://127.0.0.1:8765/ 。首次随机账号凭据位于本机 runtime/本地登录凭据.json；不要上传。以后可双击 start_local.cmd，运行期间保留启动窗口。

## 数据边界

本仓库是公开源码副本。core/data 内10家全部为合成演示企业，不是真实客户；不包含原CRM案例、业务数据库、导出文件、截图、登录凭据或密钥。原本机应用和真实数据保持原样，未被演示数据覆盖。演示案例不证明真实接入或业务改善。

真实CRM适配尚未连接。可通过 HUILAN_CRM_PROVIDER 绑定内部只读模块，导出 health() 和 fetch_snapshot()；未配置时明确显示未连接，不会自动抓取或写入CRM、外发消息。

## 检查

```bat
.venv\Scripts\python.exe manage.py check
.venv\Scripts\python.exe manage.py test core.test_workbench core.test_crm core.tests
```

测试使用隔离数据库和受控提供方。正式数据仍由运营导入或已安装的内部只读提供方返回。两轮复盘只记录真实发生的协助和客户反馈，不承诺因果贡献。
