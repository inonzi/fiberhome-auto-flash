# an758x-stock2ubi 接口契约与自动化开发指南

## 一、 前置环境与启动命令
1. **前置检查**：光猫内核必须是 5.4.55（通过 `cat /proc/sys/kernel/osrelease` 查看）。如果不是，启动时需加 `--ignore-kernel-version` 参数。
2. **启动命令**：需通过 Telnet 执行 `nohup /tmp/an758x-stock2ubi > /tmp/ubi.log 2>&1 &`。
3. **基础信息**：监听目标 IP 为 `192.168.1.1`，端口为 `3333` (HTTP)。
4. **文件命名约定**：基于本地目录下的 `*-preloader.bin` (BL2) 和 `*-bl31-u-boot.fip` (FIP) 匹配。

## 二、 HTTP 接口契约
### 1. 获取分区表（网页）
- **方法**：`GET /`
- **返回**：HTML 页面。脚本中可通过正则 `mtd(\d+)` 和 `href="/backup/(\d+)"` 抓取分区索引。

### 2. 下载分区备份
- **方法**：`GET /backup/{index}`
- **参数**：`{index}` 为整数（如 0 对应 mtd0）。
- **返回**：流式二进制数据（`application/octet-stream`）。
- **要求**：Python 端需用 `while True: chunk = resp.read(65536)` 分块读取落盘。

### 3. 触发刷写
- **方法**：`POST /api/flash`
- **请求头**：`Content-Type: multipart/form-data; boundary=xxx`
- **表单字段**：`bl2` (二进制文件)，`fip` (二进制文件)。
- **限制**：单文件不能超过 **4MB**。

## 三、 状态码与日志处理逻辑
- **200 OK**：刷写成功！JSON 返回中 `message` 包含 `readback verified`。
- **400 Bad Request**：刷写失败（文件格式不对或写保护未解除），打印 `message`。
- **409 Conflict**：已有刷写任务在跑。
- **413 Payload Too Large**：上传超 4MB。

## 四、 Python 开发的三大致命坑点（AI 必须处理）
1. **绑定网卡支持**：如果使用 `http.client.HTTPConnection`，必须注入 `source_address=(BOUND_LOCAL_IP, 0)`，否则在多网卡环境下请求会走错路由。
2. **HTTP 断连防误判**：设备刷写成功后 **800ms 内会强制重启**，导致 TCP 断开。Python 端 `resp.read()` 极大概率报 `Connection reset` 异常。
   - **解决**：必须先拿 `resp.status`。如果 `status == 200`，即使读 body 报错，也必须捕获异常并判定为**成功**。
3. **Telnet 必须保活**：为防止光猫杀后台进程，HTTP 刷写/下载期间，需开启子线程，每 20 秒向 Telnet 发送 `\n`。