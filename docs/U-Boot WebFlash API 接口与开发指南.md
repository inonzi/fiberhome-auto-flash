# U-Boot WebFlash 完整 API 与自动化开发指南

> 本文档基于 `pbs05/uboot-an758x` 项目的 Web UI 源码（`index.html`）逆向提取。100% 覆盖网页端所有的刷机、备份、高级操作接口，可作为开发自动化脚本的唯一协议参考。

## 1. 基础通讯规范
*   **传输协议**：HTTP/1.1
*   **数据格式**：
    *   文件上传采用纯二进制流（`application/octet-stream`）。
    *   指令交互与状态返回采用 JSON 格式（`application/json`）。
*   **缓存控制**：建议请求头加入 `Cache-Control: no-store`。
*   **并发限制**：U-Boot 为单线程处理，若设备正在执行任务，再次下发任务会返回 HTTP `409 Conflict`（设备忙）。

---

## 2. 初始化与信息获取

在开始操作前，必须通过以下接口拉取设备的真实状态，用于动态渲染脚本 UI 或校验参数：
*   **`GET /api/device`**：获取设备信息。
    *   返回关键字段：`model`（机型）、`ram_bytes`、`flash_bytes`、`version`。
    *   功能标志：`ubi`（是否支持 UBI 操作）、`rebuild`（是否允许重建）、`board_data`（板级数据数组，如 `[{target: "factory", size: ...}]`）。
*   **`GET /api/storage`**：获取所有存储目标（用于备份、分区写入）。
    *   返回 `targets` 数组：`[{name: "bl2", kind: "mtd", size: ...}, ...]`。
*   **`GET /api/env`**：获取当前 U-Boot 的完整环境变量（用于高级选项）。

---

## 3. 文件上传接口

### 3.1 普通上传
*   **`POST /api/upload`**
    *   **Body**：文件的原始二进制数据。
    *   **Response**：`{"upload_id": <int>}`。
    *   **适用**：BL2、FIP、系统固件、板级数据、内存启动文件、分区写入文件。

### 3.2 整片 Flash 镜像上传
*   **`POST /api/upload-all-flash`**
    *   **Body**：文件的原始二进制数据。
    *   **Response**：`{"upload_id": <int>}`。
    *   **限制**：前端会校验文件大小不能超过 `flash_bytes`。

---

## 4. 任务下发与状态轮询

### 4.1 下发任务 (`POST /api/task`)
*   **Body**：`{"operation": "指令", ...附加参数}`。
*   **Response**：`{"id": "任务ID"}`。

### 4.2 轮询状态 (`GET /api/task`)
*   **频率**：前端设置为 **700ms** 一次。
*   **返回结构**：`id`（任务ID）、`busy`（是否忙碌）、`state`（如 working, done）、`phase`（当前阶段）、`output`（全量日志）、`code`（0 为成功）。
*   **日志提取**：计算 `current_output` 与 `last_output` 的差异，只打印增量部分。
*   **重启断网处理**：当指令为 `reset`、`boot-production` 时，设备会断网。应捕获网络异常，连续丢失 3 次后判定重启成功并安全退出轮询。

---

## 5. 完整指令字典 (`operation` 字段)

### 5.1 刷机选项卡 (Flash)
| 功能说明 | operation 值 | 必需参数 | 备注 |
| :--- | :--- | :--- | :--- |
| 重建 UBI | `ubi-rebuild` | 无 | 清空 UBI 全部数据 |
| 刷写 BL2 | `flash-bl2` | `upload_id` | 写入 preloader.bin |
| 刷写 U-Boot | `flash-uboot` | `upload_id` | 写入 bl31-uboot.fip |
| 刷写系统 | `flash` | `upload_id` | 写入 sysupgrade 固件 |
| 刷写板级数据 | `flash-board-data`| `upload_id`, `target`| `target` 从 `/api/device` 获取 |
| 启动系统 | `boot-production` | 无 | 引导系统，**设备断网重启** |
| 重启设备 | `reset` | 无 | 重启，**设备断网重启** |

### 5.2 备份选项卡 (Backup)
| 功能说明 | operation 值 | 必需参数 | 备注 |
| :--- | :--- | :--- | :--- |
| 执行备份 | `backup` | `kind`, `target` | 从 `/api/storage` 获取参数 |

*   **💡 备份下载逻辑（两步走）**：
    1. 下发 `backup` 任务后，轮询直到 `state == "done"`。此时返回的 JSON 中会携带 `download_id` 和 `download_size`。
    2. 前端采用 **分块下载**（每块 4MB）。请求接口为：
       **`GET /api/download/<download_id>/<offset>/<length>`**
    3. 脚本需循环请求此接口，将返回的二进制流拼接，保存为 `.bin`。

### 5.3 高级选项卡 (Advanced)
| 功能说明 | operation 值 | 必需参数 | 备注 |
| :--- | :--- | :--- | :--- |
| 内存启动 | `boot-upload` | `upload_id` | 上传文件并直接内存启动 |
| 分区写入 | `write` | `upload_id`, `kind`, `target` | 参数从 `/api/storage` 获取 |
| 恢复整片 Flash | `flash-all` | `upload_id` | 需配合 `/api/upload-all-flash` |
| 清除坏块 | `nand-scrub` | 无 | 擦除整片 NAND，包含出厂坏块 |
| 执行命令 | `command` | `command` | 字符串，如 `"mtd list"` |
| 设置环境变量 | `env-set` | `name`, `value` | 修改 U-Boot 环境变量 |
| 返回串口 | `exit` | 无 | 退出 Web 服务 |

---

## 6. 状态机字典 (`phase`)
用于在终端/GUI 展示当前处理进度：
*   `upload`：上传中
*   `download`：下载中（备份时）
*   `queued`：等待执行
*   `working`：处理中
*   `erase`：擦除中
*   `create`：创建卷
*   `verify`：校验中
*   `write`：写入中
*   `waiting`：等待设备响应（通信超时）
*   `done`：完成

---

## 7. 标准刷机执行顺序（首次安装）
1.  **下发 `ubi-rebuild`**（等待擦除完成）。
2.  **上传 `preloader.bin`** -> 下发 `flash-bl2`。
3.  **上传 `bl31-uboot.fip`** -> 下发 `flash-uboot`。
4.  **上传 `sysupgrade.bin`** -> 下发 `flash`。
5.  **上传板级数据** -> 下发 `flash-board-data`（附带动态获取的 `target`）。
6.  **下发 `boot-production`**（设备重启进入系统，断开网络）。