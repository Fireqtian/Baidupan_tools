# BaiduNetdiskTools

百度网盘视频和谐状态检测与自动清理工具集。

## 功能简介

本工具用于检测百度网盘中被"和谐"（即无法播放，缩略图变为统一提示图）的视频文件，并支持自动移动到指定目录。基于图像相似度算法，通过对比视频缩略图与参考图片来判断视频是否被和谐。

### 核心功能

- **视频和谐状态检测**：下载视频缩略图，通过图像相似度算法判断是否为和谐视频
- **自动清理**：将检测到的和谐视频自动移动到指定目录（默认 `/rubbish_videos`）
- **操作回滚**：支持将移动的文件恢复到原位置
- **多线程并发**：支持多线程并发处理，提高扫描效率
- **增量处理**：记录已处理的文件夹，支持断点续扫

## 项目结构

```
BaiduNetdiskTools/
├── baidupan_cleaner.py      # 主清理工具
├── baidupan_probe.py        # 探测与分析工具
├── compare_images.py        # 图像相似度对比模块
├── config.example.json      # 配置文件示例
├── config.json              # 用户配置文件（包含 API 密钥和 OAuth 令牌，需自行创建，已被 gitignore）
├── deleted.jpg              # 和谐视频的参考缩略图
├── move_history.json        # 移动历史记录（自动生成）
├── processed_folders.json   # 已处理文件夹记录（自动生成）
├── probe_report.json        # 探测报告（自动生成）
└── pythonsdk_20220616/      # 百度网盘官方 Python SDK
```

## 环境准备

### 依赖安装

```bash
pip install opencv-python numpy scikit-image requests
```

### 配置步骤

1. **创建配置文件**

   复制 `config.example.json` 为 `config.json`，并填写以下配置：

   ```json
   {
       "APP_KEY": "你的百度应用API Key",
       "SECRET_KEY": "你的百度应用Secret Key",
       "REDIRECT_URI": "oob",
       "access_token": "",
       "refresh_token": "",
       "PROBE_PATH": "/全部资源",
       "RUBBISH_DIR": "/rubbish_videos",
       "SIMILARITY_THRESHOLD": 0.90,
       "DEFAULT_THREADS": 5
   }
   ```

   配置项说明：
   - `APP_KEY` 和 `SECRET_KEY`：百度网盘开放平台申请的应用密钥（必填）
   - `REDIRECT_URI`：OAuth 回调地址，默认 `oob` 表示手动授权
   - `access_token` 和 `refresh_token`：OAuth 令牌，首次授权后自动保存到配置文件（无需手动填写）
   - `PROBE_PATH`：默认扫描路径
   - `RUBBISH_DIR`：和谐视频暂存目录
   - `SIMILARITY_THRESHOLD`：相似度阈值
   - `DEFAULT_THREADS`：默认并发线程数

   > **注意**：`access_token` 和 `refresh_token` 在首次授权后会自动保存，无需手动填写。

2. **获取百度网盘 API 密钥**

   - 访问 [百度网盘开放平台](https://pan.baidu.com/union/doc/)
   - 创建应用，获取 `APP_KEY` 和 `SECRET_KEY`
   - 在应用设置中配置回调地址（默认 `oob` 表示手动授权）

3. **准备参考图片**

   将一张和谐视频的缩略图保存为 `deleted.jpg` 放在项目根目录。可以通过 `baidupan_probe.py` 工具获取。

## 使用指南

### 1. 探测工具 (baidupan_probe.py)

用于扫描指定目录，下载视频缩略图，分析相似度，确定合适的阈值。

```bash
# 基本使用
python baidupan_probe.py

# 修改 config.json 中的 PROBE_PATH 来指定扫描路径
```

**输出内容：**
- 下载所有视频的缩略图到 `thumbnails/` 目录
- 输出每个视频与 `deleted.jpg` 的相似度分析报告
- 生成 `probe_report.json` 完整报告

### 2. 清理工具 (baidupan_cleaner.py)

主清理工具，自动检测并移动和谐视频。

```bash
# 执行清理（使用 config.json 中的配置）
python baidupan_cleaner.py

# 指定扫描路径
python baidupan_cleaner.py --path /videos

# 指定并发线程数
python baidupan_cleaner.py --threads 10

# 预览模式（不实际移动文件）
python baidupan_cleaner.py --dry-run

# 强制重新扫描（忽略增量处理记录）
python baidupan_cleaner.py --force

# 执行回滚（恢复已移动的文件）
python baidupan_cleaner.py --rollback

# 预览回滚
python baidupan_cleaner.py --rollback --dry-run

# 显示帮助
python baidupan_cleaner.py --help
```

### 3. 图像对比工具 (compare_images.py)

独立的图片对比工具，可用于手动测试。

```bash
python compare_images.py <图片1路径> <图片2路径>
```

## 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `APP_KEY` | 百度网盘应用 API Key | 必填 |
| `SECRET_KEY` | 百度网盘应用 Secret Key | 必填 |
| `REDIRECT_URI` | OAuth 回调地址 | `oob` |
| `PROBE_PATH` | 默认扫描路径 | `/全部资源` |
| `RUBBISH_DIR` | 和谐视频暂存目录 | `/rubbish_videos` |
| `SIMILARITY_THRESHOLD` | 相似度阈值 | `0.90` |
| `DEFAULT_THREADS` | 默认并发线程数 | `5` |

## 相似度算法说明

本工具使用多维度图像相似度算法，综合考虑以下指标：

- **直方图相关性**：颜色分布的相似程度
- **直方图巴氏距离**：颜色分布差异
- **均方误差 (MSE)**：像素级差异
- **特征点匹配**：ORB 特征点匹配率
- **结构相似度 (SSIM)**：图像结构相似程度

最终的综合相似度为加权平均值，当相似度 ≥ 90% 时判定为和谐视频。

## 授权流程

首次运行时，程序会引导进行 OAuth 授权：

1. 程序输出授权 URL
2. 浏览器访问该 URL，登录百度账号并授权
3. 授权后页面显示或跳转到包含 `code` 的 URL
4. 复制 `code` 值，粘贴到程序中
5. 程序自动保存令牌到 `config.json`

令牌过期时会自动刷新，无需重新授权。

## 注意事项

1. **数据安全**：请妥善保管 `config.json`，不要提交到公开仓库（该文件已添加到 `.gitignore`）
2. **API 限制**：百度网盘 API 有调用频率限制，建议合理设置线程数
3. **误判风险**：相似度算法可能存在误判，建议先使用 `--dry-run` 预览
4. **备份建议**：清理前建议备份重要文件列表

## 许可证

MIT License