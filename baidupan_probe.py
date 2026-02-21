#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度网盘视频和谐状态探测工具 (使用 SDK 版本)
用于分析正常视频与被和谐视频在 API 返回数据上的差异
"""

import json
import os
import sys
from urllib.parse import urlencode

# ============================================================
# SDK 路径设置 - 将 SDK 添加到 Python 路径
# ============================================================
SDK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pythonsdk_20220616')
if SDK_DIR not in sys.path:
    sys.path.insert(0, SDK_DIR)

# 导入 SDK
import openapi_client
from openapi_client.api import auth_api, fileinfo_api, multimediafile_api
from openapi_client.exceptions import ApiException

# 导入 requests 用于下载测试
import requests

# 导入图像对比模块
import compare_images


# ============================================================
# 配置文件加载
# ============================================================
def load_config(config_file="config.json"):
    """从配置文件加载配置，如果不存在则使用默认值"""
    default_config = {
        "APP_KEY": "",
        "SECRET_KEY": "",
        "REDIRECT_URI": "oob",
        "PROBE_PATH": "/test_videos",
        "RUBBISH_DIR": "/rubbish_videos",
        "SIMILARITY_THRESHOLD": 0.90,
        "DEFAULT_THREADS": 5
    }
    
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                user_config = json.load(f)
                default_config.update(user_config)
                print(f"[INFO] 已从 {config_file} 加载配置")
        except Exception as e:
            print(f"[WARN] 加载配置文件失败: {e}，使用默认配置")
    else:
        print(f"[WARN] 配置文件 {config_file} 不存在，请复制 config.example.json 为 config.json 并填写配置")
    
    return default_config


# 加载全局配置
CONFIG = load_config()

# ============================================================
# 用户配置区域 - 从配置文件读取
# ============================================================

# 要探测的网盘路径
PROBE_PATH = CONFIG.get("PROBE_PATH", "/test_videos")

# 是否保存缩略图到本地
SAVE_THUMBNAILS = True

# 缩略图保存目录
THUMBNAIL_DIR = "thumbnails"

# 当前使用的授权方式: "device_code" 或 "authorization_code"
AUTH_TYPE = "authorization_code"

# OAuth2.0 回调地址（在百度开放平台 -> 应用信息 -> 安全设置 中配置）
REDIRECT_URI = CONFIG.get("REDIRECT_URI", "oob")

# ============================================================


class BaiduPanProbe:
    """百度网盘 API 探测类 (SDK 版本)"""

    # OAuth2.0 相关 URL
    AUTHORIZE_URL = "https://openapi.baidu.com/oauth/2.0/authorize"
    TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
    DEV_PIN_URL = "https://openapi.baidu.com/device/code"

    def __init__(self):
        self.access_token = None
        self.refresh_token = None
        # 初始化 SDK Configuration
        self.configuration = openapi_client.Configuration()
        self.api_client = openapi_client.ApiClient(self.configuration)
        # 初始化 API 实例
        self.auth_api = auth_api.AuthApi(self.api_client)
        self.fileinfo_api = fileinfo_api.FileinfoApi(self.api_client)
        self.multimedia_api = multimediafile_api.MultimediafileApi(self.api_client)
        
        # 从配置加载 API 密钥
        self.APP_KEY = CONFIG.get("APP_KEY", "")
        self.SECRET_KEY = CONFIG.get("SECRET_KEY", "")
        
        if not self.APP_KEY or not self.SECRET_KEY:
            print("[ERROR] 未配置 APP_KEY 或 SECRET_KEY，请在 config.json 中填写")
            raise ValueError("缺少 API 密钥配置")

    def generate_auth_url(self):
        """生成授权 URL"""
        params = {
            "client_id": self.APP_KEY,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": "basic,netdisk",
            "display": "page"
        }
        auth_url = f"{self.AUTHORIZE_URL}?{urlencode(params)}"
        return auth_url

    def exchange_code_for_token(self, code):
        """用授权码换取 Access Token (使用 SDK)"""
        try:
            # 使用 SDK 调用换码接口
            response = self.auth_api.oauth_token_code2token(
                code=code,
                client_id=self.APP_KEY,
                client_secret=self.SECRET_KEY,
                redirect_uri=REDIRECT_URI
            )

            # 提取 token 信息
            self.access_token = response.access_token
            self.refresh_token = response.refresh_token

            print(f"[OK] 授权成功！")
            print(f"  Access Token: {self.access_token[:20]}...")
            print(f"  有效期: {response.expires_in} 秒")
            return True

        except ApiException as e:
            print(f"授权失败: {e}")
            return False

    def auth_flow(self):
        """授权流程"""
        print("=== 开始 OAuth2.0 授权流程 ===\n")

        # 生成授权 URL
        auth_url = self.generate_auth_url()
        print("请访问以下 URL 进行授权：")
        print(f"  {auth_url}\n")

        # 等待用户输入 code
        print("授权完成后，页面会显示或跳转到回调地址")
        print("请在回调地址的 URL 中找到 'code=' 后面的值")
        print("\n请输入 code: ", end="")
        code = input().strip()

        if not code:
            print("错误: 未输入 code")
            return False

        return self.exchange_code_for_token(code)

    def save_token(self, config_file="config.json"):
        """保存 token 到配置文件"""
        global CONFIG
        if self.access_token:
            CONFIG["access_token"] = self.access_token
            CONFIG["refresh_token"] = self.refresh_token
            try:
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(CONFIG, f, indent=4, ensure_ascii=False)
                print(f"Token 已保存到 {config_file}")
            except Exception as e:
                print(f"[WARN] 保存 token 失败: {e}")

    def load_token(self, config_file="config.json"):
        """从配置文件加载 token"""
        global CONFIG
        # 优先从全局 CONFIG 中读取（已由 load_config 加载）
        self.access_token = CONFIG.get("access_token")
        self.refresh_token = CONFIG.get("refresh_token")
        if self.access_token:
            print(f"已从 {config_file} 加载 token")
            return True
        return False

    def refresh_access_token(self):
        """使用 refresh_token 刷新 access_token (使用 SDK)"""
        if not self.refresh_token:
            print("没有 refresh_token，无法自动刷新")
            return False

        try:
            # 使用 SDK 调用刷新接口
            response = self.auth_api.oauth_token_refresh_token(
                refresh_token=self.refresh_token,
                client_id=self.APP_KEY,
                client_secret=self.SECRET_KEY
            )

            # 更新 token
            self.access_token = response.access_token
            self.refresh_token = response.refresh_token or self.refresh_token

            print(f"[OK] 自动刷新 token 成功！")
            print(f"  Access Token: {self.access_token[:20]}...")
            print(f"  新有效期: {response.expires_in} 秒")
            return True

        except ApiException as e:
            print(f"刷新 token 失败: {e}")
            return False

    def _handle_api_call(self, api_call, retry_on_expire=True):
        """
        包装 API 调用，支持自动刷新 token
        :param api_call: 返回调用结果的字典
        :param retry_on_expire: 是否在 token 过期时自动刷新重试
        """
        try:
            result = api_call()

            # SDK 返回的是 dict 类型，检查 errno
            if isinstance(result, dict) and retry_on_expire:
                errno = result.get("errno")
                if errno == -6:  # token 过期
                    print("检测到 token 过期，正在自动刷新...")
                    if self.refresh_access_token():
                        self.save_token()
                        # 使用新 token 重新调用
                        return api_call()
                    else:
                        print("自动刷新失败，需要重新授权")
                        return None
            return result

        except ApiException as e:
            # 解析错误响应
            try:
                error_body = json.loads(e.body) if e.body else {}
                errno = error_body.get("error_code") or error_body.get("errno")

                if errno == -6 and retry_on_expire:
                    print("检测到 token 过期，正在自动刷新...")
                    if self.refresh_access_token():
                        self.save_token()
                        return api_call()
                    else:
                        print("自动刷新失败，需要重新授权")
                        return None

                print(f"API 调用失败: error_code={errno}, msg={error_body.get('error_msg', 'unknown')}")
            except:
                print(f"API 调用失败: {e}")
            return None

    def list_files(self, path="/", order="time", desc=1, limit=100):
        """
        获取指定目录下的文件列表 (使用 SDK)
        """
        def api_call():
            return self.fileinfo_api.xpanfilelist(
                access_token=self.access_token,
                dir=path,
                order=order,
                desc=desc,
                start="0",
                limit=limit,
                web="web",
                folder="0"
            )

        result = self._handle_api_call(api_call)
        if result and result.get("errno") == 0:
            return result.get("list", [])
        else:
            if result:
                print(f"获取文件列表失败: {result}")
            return []

    def get_file_metas(self, fsids):
        """
        获取文件详细元数据（包含缩略图等）(使用 SDK)
        """
        if isinstance(fsids, int):
            fsids = [fsids]

        # fsids 需要是 JSON 字符串格式
        fsids_json = json.dumps(fsids)

        def api_call():
            return self.multimedia_api.xpanmultimediafilemetas(
                access_token=self.access_token,
                fsids=fsids_json,
                dlink="1",
                thumb="1",
                extra="1",
                needmedia=1
            )

        result = self._handle_api_call(api_call)
        if result and result.get("errno") == 0:
            return result.get("list", [])
        else:
            if result:
                print(f"获取文件元数据失败: {result}")
            return []

    def walk_directory(self, path="/", visited=None, depth=0):
        """
        递归遍历目录，收集所有视频文件
        返回: (video_files, total_dirs) - 视频文件列表和访问的目录数量
        """
        if visited is None:
            visited = set()
        
        # 避免循环（虽然网盘通常不会有）
        if path in visited:
            return [], 0
        visited.add(path)
        
        # 输出当前扫描的目录
        indent = "  " * depth
        print(f"{indent}[扫描] {path}")
        
        files = self.list_files(path)
        if not files:
            return [], 1
        
        video_extensions = ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.rmvb', '.mpeg', '.m4v']
        video_files = []
        subdirs = []
        
        for f in files:
            filename = f.get("server_filename", "")
            is_dir = f.get("isdir") == 1
            
            if is_dir:
                subdirs.append(f)
            elif any(filename.lower().endswith(ext) for ext in video_extensions):
                video_files.append(f)
        
        total_dirs = 1  # 当前目录
        
        # 递归处理子目录
        for subdir in subdirs:
            subdir_path = subdir.get("path", f"{path}/{subdir.get('server_filename')}")
            sub_videos, sub_dirs = self.walk_directory(subdir_path, visited, depth + 1)
            video_files.extend(sub_videos)
            total_dirs += sub_dirs
        
        return video_files, total_dirs

    def probe_directory(self, path="/"):
        """
        探测指定目录及其所有子目录下的所有视频文件
        """
        print(f"\n=== 开始递归探测: {path} ===")
        
        # 递归收集所有视频文件
        video_files, total_dirs = self.walk_directory(path)
        
        print(f"\n总计扫描目录: {total_dirs} 个，发现视频文件: {len(video_files)} 个")
        
        if not video_files:
            return []
        
        # 创建 fs_id 到文件基本信息的映射，用于后续补充文件名
        file_info_map = {f["fs_id"]: f for f in video_files}
        
        # 分批获取元数据（每次最多100个）
        all_metas = []
        fsids = [f["fs_id"] for f in video_files]
        batch_size = 100
        
        for i in range(0, len(fsids), batch_size):
            batch = fsids[i:i + batch_size]
            print(f"  获取元数据批次 {i//batch_size + 1}/{(len(fsids)-1)//batch_size + 1} ({len(batch)} 个文件)")
            metas = self.get_file_metas(batch)
            all_metas.extend(metas)
        
        # 将文件名从 file_info_map 合并到 metas 中
        for meta in all_metas:
            fs_id = meta.get("fs_id")
            if fs_id and fs_id in file_info_map:
                # 如果 metas 中没有 server_filename，则从 file_info_map 中获取
                if not meta.get("server_filename"):
                    meta["server_filename"] = file_info_map[fs_id].get("server_filename", "unknown")
                # 同时补充 path 字段（如果元数据中没有）
                if not meta.get("path") and file_info_map[fs_id].get("path"):
                    meta["path"] = file_info_map[fs_id].get("path")
        
        return all_metas

    def analyze_videos(self, videos, thumbnail_map=None):
        """
        分析视频列表，通过缩略图与 deleted.jpg 的相似度判断和谐状态
        :param thumbnail_map: dict - {fs_id: thumbnail_local_path, ...}
        """
        if thumbnail_map is None:
            thumbnail_map = {}

        print("\n" + "=" * 80)
        print("视频文件相似度分析报告 (实验阶段)")
        print("=" * 80)

        for idx, video in enumerate(videos, 1):
            print(f"\n【视频 {idx}】{video.get('server_filename', 'Unknown')}")
            print("-" * 60)

            fs_id = video.get("fs_id")
            size = video.get("size", 0)
            size_mb = size / (1024 * 1024) if size else 0

            print(f"  fs_id:          {fs_id}")
            print(f"  文件大小:       {size} bytes ({size_mb:.2f} MB)")

            # 检查是否有本地缩略图
            thumb_path = thumbnail_map.get(fs_id)
            if not thumb_path:
                print(f"  [WARN] 无法获取缩略图路径，跳过相似度对比")
                continue

            if not os.path.exists(thumb_path):
                print(f"  [WARN] 缩略图文件不存在: {thumb_path}")
                continue

            # 检查 deleted.jpg 是否存在
            deleted_jpg_path = "deleted.jpg"
            if not os.path.exists(deleted_jpg_path):
                print(f"  [FAIL] 参考文件 deleted.jpg 不存在于当前目录")
                print(f"        请将和谐视频的缩略图保存为 deleted.jpg")
                continue

            # 计算相似度
            try:
                results = compare_images.compare_images(thumb_path, deleted_jpg_path)
                similarity = results.get("overall_similarity", 0)
                similarity_percent = similarity * 100

                print(f"  [INFO] 缩略图: {os.path.basename(thumb_path)}")
                print(f"  [INFO] 与 deleted.jpg 相似度: {similarity:.4f} ({similarity_percent:.2f}%)")

                # 实验阶段：输出详细相似度指标供观察
                print(f"    - 直方图相关性: {results.get('histogram_correlation', 0):.4f}")
                print(f"    - 直方图巴氏相似度: {results.get('histogram_bhattacharyya_similarity', 0):.4f}")
                if results.get('ssim') is not None:
                    print(f"    - SSIM结构相似度: {results.get('ssim', 0):.4f}")

                # 提供初步判断（基于相似度，但不作为最终结论）
                if similarity >= 0.90:
                    print(f"  [ALERT] 高度相似 (>90%)，极有可能是被和谐的视频")
                elif similarity >= 0.75:
                    print(f"  [WARN] 比较相似 (75%-90%)，可能是被和谐的视频")
                elif similarity >= 0.50:
                    print(f"  [NOTE] 中等相似度 (50%-75%)")
                else:
                    print(f"  [OK] 相似度较低 (<50%)，大概率是正常视频")

            except Exception as e:
                print(f"  [FAIL] 相似度计算失败: {e}")

        print("\n" + "=" * 80)
        print("提示: 请观察上述相似度数值，确定合适的阈值设定")
        print("=" * 80)

    def download_thumbnail(self, url, save_path):
        """
        下载缩略图并保存 (使用 requests)
        """
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://pan.baidu.com/"
            }
            resp = requests.get(url, headers=headers, timeout=30)

            if resp.status_code == 200:
                with open(save_path, "wb") as f:
                    f.write(resp.content)
                return True, None
            else:
                return False, f"HTTP {resp.status_code}"

        except requests.exceptions.Timeout:
            return False, "下载超时"
        except requests.exceptions.ConnectionError:
            return False, "连接错误"
        except Exception as e:
            return False, str(e)

    def save_thumbnails(self, videos):
        """
        保存所有视频的缩略图
        返回: dict - {fs_id: thumbnail_local_path, ...}
        """
        thumbnail_map = {}

        if not SAVE_THUMBNAILS:
            return thumbnail_map

        # 创建缩略图目录
        if not os.path.exists(THUMBNAIL_DIR):
            os.makedirs(THUMBNAIL_DIR)
            print(f"\n创建缩略图目录: {THUMBNAIL_DIR}")

        print(f"\n=== 正在下载缩略图 ===")

        saved_count = 0
        for video in videos:
            fs_id = video.get("fs_id")
            filename = video.get("server_filename", "unknown")
            thumbs = video.get("thumbs", {})

            if not thumbs:
                print(f"  [FAIL] {filename}: 无缩略图")
                continue

            # 选择最大的缩略图保存
            best_size = None
            best_url = None
            for size in ["url3", "url2", "url1"]:
                if size in thumbs:
                    best_size = size
                    best_url = thumbs[size]
                    break

            if best_url:
                # 构建保存文件名
                base_name = os.path.splitext(filename)[0]
                ext = ".jpg"  # 缩略图通常是 jpg
                save_name = f"{base_name}_{fs_id}_{best_size}{ext}"
                save_path = os.path.join(THUMBNAIL_DIR, save_name)

                success, error = self.download_thumbnail(best_url, save_path)
                if success:
                    print(f"  [OK] {filename}: 已保存为 {save_name}")
                    thumbnail_map[fs_id] = save_path
                    saved_count += 1
                else:
                    print(f"  [FAIL] {filename}: 下载失败 - {error}")

        print(f"\n缩略图下载完成: {saved_count}/{len(videos)} 个成功")
        return thumbnail_map

    def save_report(self, videos, filepath="probe_report.json"):
        """保存探测结果到文件"""
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(videos, f, indent=2, ensure_ascii=False)
        print(f"\n探测报告已保存到: {filepath}")


def main():
    """主函数"""
    probe = BaiduPanProbe()

    print("=" * 60)
    print("百度网盘视频和谐状态探测工具 (SDK 版本)")
    print("=" * 60)

    # 显示配置
    print(f"\n【当前配置】")
    print(f"  探测路径: {PROBE_PATH}")
    print(f"  保存缩略图: {'是' if SAVE_THUMBNAILS else '否'}")
    if SAVE_THUMBNAILS:
        print(f"  缩略图目录: {THUMBNAIL_DIR}")

    # 尝试加载已有 token（默认使用，不再每次询问）
    probe.load_token()

    # 如果没有 token，进行授权流程
    if not probe.access_token:
        if not probe.auth_flow():
            print("授权失败，程序退出")
            sys.exit(1)
        probe.save_token()

    # 执行探测（使用配置的路径）
    videos = probe.probe_directory(PROBE_PATH)

    if videos:
        # 1. 先下载缩略图（返回缩略图路径映射）
        thumbnail_map = probe.save_thumbnails(videos)

        # 2. 基于缩略图与 deleted.jpg 的相似度分析视频
        probe.analyze_videos(videos, thumbnail_map)

        # 3. 保存完整报告
        probe.save_report(videos)
    else:
        # 如果是因为 token 无效导致获取失败，尝试刷新后重试一次
        if not probe.access_token:
            print("\n检测到授权已失效，尝试自动刷新...")
            if probe.refresh_access_token():
                probe.save_token()
                print("刷新成功，重新尝试探测...")
                videos = probe.probe_directory(PROBE_PATH)
                if videos:
                    thumbnail_map = probe.save_thumbnails(videos)
                    probe.analyze_videos(videos, thumbnail_map)
                    probe.save_report(videos)
                else:
                    print("未找到视频文件")
            else:
                print("自动刷新失败，需要进行重新授权")
                if probe.auth_flow():
                    probe.save_token()
                    # 重新探测
                    videos = probe.probe_directory(PROBE_PATH)
                    if videos:
                        thumbnail_map = probe.save_thumbnails(videos)
                        probe.analyze_videos(videos, thumbnail_map)
                        probe.save_report(videos)
                    else:
                        print("未找到视频文件")
                else:
                    print("授权失败，程序退出")
                    sys.exit(1)
        else:
            print("未找到视频文件")

    print("\n探测完成！")


if __name__ == "__main__":
    main()