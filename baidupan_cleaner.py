#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度网盘视频和谐状态检测与自动清理工具
功能：
1. 递归扫描指定目录视频文件
2. 下载视频缩略图
3. 与 deleted.jpg 进行相似度比对
4. 相似度 > 90% 的视频自动移动到 /rubbish_videos
5. 记录移动历史到 move_history.json，支持回滚
6. 具有较粗粒度的处理记录与增量处理功能

使用方式：
  python baidupan_cleaner.py                       # 执行清理
  python baidupan_cleaner.py --rollback            # 执行回滚
  python baidupan_cleaner.py --path /test          # 指定扫描路径
  python baidupan_cleaner.py --dry-run             # 仅检测，不移动
  python baidupan_cleaner.py --threads 10          # 使用10个线程
  python baidupan_cleaner.py --force               # 强制重新扫描全部
  python baidupan_cleaner.py --rollback --dry-run  # 预览回滚
"""

import json
import os
import sys
import time
import hashlib
import requests
import io
from datetime import datetime
from urllib.parse import urlencode
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# SDK 路径设置 - 将 SDK 添加到 Python 路径
# ============================================================
SDK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pythonsdk_20220616')
if SDK_DIR not in sys.path:
    sys.path.insert(0, SDK_DIR)

# 导入 SDK
import openapi_client
from openapi_client.api import auth_api, fileinfo_api, multimediafile_api, filemanager_api, fileupload_api
from openapi_client.exceptions import ApiException

# 导入图像对比模块
import compare_images


def format_size(size_bytes):
    """
    将字节数转换为人类可读的格式
    """
    if size_bytes < 0:
        return "未知"
    
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    size = float(size_bytes)
    unit_index = 0
    
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    else:
        return f"{size:.2f} {units[unit_index]}"


# ============================================================
# 配置文件加载
# ============================================================
def load_config(config_file="config.json"):
    """从配置文件加载配置，如果不存在则使用默认值"""
    default_config = {
        "APP_KEY": "",
        "SECRET_KEY": "",
        "REDIRECT_URI": "oob",
        "PROBE_PATH": "/my_files",
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
PROBE_PATH = CONFIG.get("PROBE_PATH", "/my_files")

# 和谐视频的暂存目录
RUBBISH_DIR = CONFIG.get("RUBBISH_DIR", "/rubbish_videos")

# 相似度阈值（90%）
SIMILARITY_THRESHOLD = CONFIG.get("SIMILARITY_THRESHOLD", 0.90)

# 是否仅预览模式（不实际移动文件）
DRY_RUN = False

# 缩略图保存临时目录
TEMP_THUMBNAIL_DIR = "temp_thumbnails"

# 移动历史记录文件
MOVE_HISTORY_FILE = "move_history.json"

# 已处理文件夹记录文件（用于增量处理）
PROCESSED_FOLDERS_FILE = "processed_folders.json"

# 参考缩略图文件路径
DELETED_JPG_PATH = "deleted.jpg"

# 默认并发线程数
DEFAULT_THREADS = CONFIG.get("DEFAULT_THREADS", 5)

# OAuth2.0 回调地址
REDIRECT_URI = CONFIG.get("REDIRECT_URI", "oob")

# ============================================================


class BaiduPanCleaner:
    """百度网盘和谐视频清理器 (SDK 版本)"""

    # OAuth2.0 相关 URL
    AUTHORIZE_URL = "https://openapi.baidu.com/oauth/2.0/authorize"
    TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
    DEV_PIN_URL = "https://openapi.baidu.com/device/code"

    def __init__(self, dry_run=False, max_workers=DEFAULT_THREADS, force_rescan=False):
        self.access_token = None
        self.refresh_token = None
        self.dry_run = dry_run
        self.force_rescan = force_rescan  # 强制重新扫描，忽略增量处理记录
        self.move_history = []
        self.remote_dir_exists = False
        
        # 增量处理相关
        self.processed_folders = set()  # 已处理的文件夹集合
        self.probe_base_path = None     # 扫描基准路径，用于计算相对深度
        
        # 多线程相关
        self.max_workers = max_workers
        self.executor = None  # 线程池，在run_cleanup中初始化
        
        # 初始化 SDK Configuration
        self.configuration = openapi_client.Configuration()
        self.api_client = openapi_client.ApiClient(self.configuration)
        
        # 初始化 API 实例
        self.auth_api = auth_api.AuthApi(self.api_client)
        self.fileinfo_api = fileinfo_api.FileinfoApi(self.api_client)
        self.multimedia_api = multimediafile_api.MultimediafileApi(self.api_client)
        self.filemanager_api = filemanager_api.FilemanagerApi(self.api_client)
        self.fileupload_api = fileupload_api.FileuploadApi(self.api_client)
        
        # 确保临时目录存在
        self._ensure_temp_dir()
        
        # 锁用于线程安全地更新历史记录和增量记录
        self.history_lock = threading.Lock()
        self.processed_folders_lock = threading.Lock()
        
        # 视频大小统计
        self.total_size = 0  # 扫描到的视频总大小（字节）
        self.total_size_lock = threading.Lock()
        
        # 从配置加载 API 密钥
        self.APP_KEY = CONFIG.get("APP_KEY", "")
        self.SECRET_KEY = CONFIG.get("SECRET_KEY", "")
        
        if not self.APP_KEY or not self.SECRET_KEY:
            print("[ERROR] 未配置 APP_KEY 或 SECRET_KEY，请在 config.json 中填写")
            raise ValueError("缺少 API 密钥配置")

    def _ensure_temp_dir(self):
        """确保临时缩略图目录存在"""
        if not os.path.exists(TEMP_THUMBNAIL_DIR):
            os.makedirs(TEMP_THUMBNAIL_DIR)
            print(f"[INFO] 创建临时缩略图目录: {TEMP_THUMBNAIL_DIR}")

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
            response = self.auth_api.oauth_token_code2token(
                code=code,
                client_id=self.APP_KEY,
                client_secret=self.SECRET_KEY,
                redirect_uri=REDIRECT_URI
            )

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

        auth_url = self.generate_auth_url()
        print("请访问以下 URL 进行授权：")
        print(f"  {auth_url}\n")

        print("授权完成后，页面会显示或跳转到回调地址")
        print("请在回调地址的 URL 中找到 'code=' 后面的值，或从 URL 中复制 code 值")
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
            response = self.auth_api.oauth_token_refresh_token(
                refresh_token=self.refresh_token,
                client_id=self.APP_KEY,
                client_secret=self.SECRET_KEY
            )

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

            if isinstance(result, dict) and retry_on_expire:
                errno = result.get("errno")
                if errno == -6:  # token 过期
                    print("检测到 token 过期，正在自动刷新...")
                    if self.refresh_access_token():
                        self.save_token()
                        return api_call()
                    else:
                        print("自动刷新失败，需要重新授权")
                        return None
            return result

        except ApiException as e:
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

                error_msg = error_body.get('error_msg', 'unknown')
                print(f"API 调用失败: error_code={errno}, msg={error_msg}")
            except:
                print(f"API 调用失败: {e}")
            return None

    def check_remote_dir_exists(self, path):
        """检查远程目录是否存在"""
        def api_call():
            return self.fileinfo_api.xpanfilelist(
                access_token=self.access_token,
                dir=path,
                web="web"
            )

        result = self._handle_api_call(api_call)
        if result and result.get("errno") == 0:
            return True
        elif result and result.get("errno") == 2:
            # 错误码 2 表示目录不存在
            return False
        else:
            print(f"[WARN] 检查目录 {path} 时出错: {result}")
            return False

    def create_remote_directory(self, path):
        """
        在网盘中创建目录
        使用 xpanfilecreate 接口并设置 isdir=1
        """
        print(f"[INFO] 正在创建远程目录: {path}")
        
        # 准备请求参数
        uploadid = hashlib.md5(f"{path}_{time.time()}".encode()).hexdigest()
        
        def api_call():
            return self.fileupload_api.xpanfilecreate(
                access_token=self.access_token,
                path=path,
                isdir=1,  # 1 表示创建目录
                size=0,   # 目录大小为 0
                uploadid=uploadid,
                block_list='[]'  # 目录不需要 block
            )

        result = self._handle_api_call(api_call)
        if result:
            if result.get("errno") == 0:
                print(f"[OK] 目录创建成功: {path}")
                return True
            elif result.get("errno") in [2, -7]:
                # 2: 目录已存在, -7:文件已存在
                print(f"[INFO] 目录已存在: {path}")
                return True
            else:
                print(f"[FAIL] 创建目录失败: {result}")
                return False
        return False

    def ensure_rubbish_dir(self):
        """确保远程垃圾目录存在"""
        if self.remote_dir_exists:
            return True
            
        exists = self.check_remote_dir_exists(RUBBISH_DIR)
        if exists:
            self.remote_dir_exists = True
            return True
        
        success = self.create_remote_directory(RUBBISH_DIR)
        if success:
            self.remote_dir_exists = True
        return success

    def move_remote_file(self, source_path, dest_dir, filename):
        """
        移动远程文件
        :param source_path: 源文件完整路径
        :param dest_dir: 目标目录
        :param filename: 文件名
        :return: 是否成功
        """
        filelist = json.dumps([{
            "path": source_path,
            "dest": dest_dir,
            "newname": filename,
            "ondup": "overwrite"
        }])

        def api_call():
            return self.filemanager_api.filemanagermove(
                access_token=self.access_token,
                _async=1,
                filelist=filelist
            )

        result = self._handle_api_call(api_call)
        if result and result.get("errno") == 0:
            return True
        else:
            errno = result.get("errno", "unknown") if result else "unknown"
            errmsg = result.get("errmsg", str(result)) if result else "未知错误"
            print(f"[FAIL] 移动文件失败 ({filename}): errno={errno}, msg={errmsg}")
            return False

    def list_files(self, path="/", order="time", desc=1, limit=None):
        """
        获取指定目录下的所有文件列表 (使用 SDK，自动分页)
        
        :param path: 目录路径
        :param order: 排序字段
        :param desc: 是否降序
        :param limit: 最大返回数量，None 表示获取全部（无上限）
        :return: 成功返回文件列表，失败返回 None，空目录返回 []
        """
        all_files = []
        start = 0
        batch_size = 1000  # 百度网盘API单次最多返回1000
        has_error = False

        while True:
            def api_call():
                return self.fileinfo_api.xpanfilelist(
                    access_token=self.access_token,
                    dir=path,
                    order=order,
                    desc=desc,
                    start=str(start),
                    limit=batch_size,
                    web="web",
                    folder="0"
                )

            result = self._handle_api_call(api_call)
            
            # API 调用失败
            if result is None:
                print(f"[ERROR] 获取文件列表失败 (API返回None): {path}")
                has_error = True
                break
            
            # API 返回错误码
            if result.get("errno") != 0:
                errno = result.get("errno", "unknown")
                print(f"[ERROR] 获取文件列表失败: {path}, errno={errno}")
                has_error = True
                break
            
            file_list = result.get("list", [])
            
            # 空列表表示已获取完毕
            if not file_list:
                break
            
            all_files.extend(file_list)
            
            # 如果返回的数量小于batch_size，说明已经获取完所有文件
            if len(file_list) < batch_size:
                break
            
            start += len(file_list)
            
            # 如果指定了 limit 且已获取足够数量，提前退出
            if limit is not None and len(all_files) >= limit:
                all_files = all_files[:limit]
                break
        
        # 如果发生错误，返回 None 让调用者区分"空"和"错误"
        if has_error:
            return None
        
        if len(all_files) > 100:
            print(f"  [INFO] 目录 {path} 共获取 {len(all_files)} 个文件/子目录")
        
        return all_files

    def get_file_metas(self, fsids):
        """
        获取文件详细元数据（包含缩略图等）(使用 SDK)
        """
        if isinstance(fsids, int):
            fsids = [fsids]

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

    def download_thumbnail_to_memory(self, url):
        """
        下载缩略图到内存 (使用 requests)
        返回值: (success: bool, content: bytes)
        """
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://pan.baidu.com/"
            }
            resp = requests.get(url, headers=headers, timeout=30)
            
            if resp.status_code == 200:
                return True, resp.content
            else:
                return False, f"HTTP {resp.status_code}"
                
        except requests.exceptions.Timeout:
            return False, "下载超时"
        except requests.exceptions.ConnectionError:
            return False, "连接错误"
        except Exception as e:
            return False, str(e)

    def save_thumbnail_temp(self, content, filename):
        """将缩略图内容保存到临时文件"""
        save_path = os.path.join(TEMP_THUMBNAIL_DIR, filename)
        try:
            with open(save_path, "wb") as f:
                f.write(content)
            return save_path
        except Exception as e:
            print(f"[FAIL] 保存临时缩略图失败: {e}")
            return None

    def check_single_video(self, video_info):
        """
        检测单个视频是否为和谐视频
        返回: (is_harmonized: bool, similarity: float, thumb_path: str)
        """
        filename = video_info.get("server_filename", "unknown")
        fs_id = video_info.get("fs_id")
        path = video_info.get("path", "unknown")
        
        print(f"  [DEBUG] 开始检测视频: {filename}")
        print(f"  [DEBUG]   fs_id: {fs_id}")
        print(f"  [DEBUG]   path: {path}")
        
        # 获取元数据（包含缩略图）
        metas = self.get_file_metas([fs_id])
        if not metas:
            print(f"  [WARN] 无法获取元数据 - fs_id: {fs_id}")
            print(f"  [DEBUG]   视频信息: filename={filename}, path={path}")
            return False, 0, None
        
        print(f"  [DEBUG] 成功获取元数据，字段: {list(metas[0].keys())}")
            
        meta = metas[0]
        thumbs = meta.get("thumbs", {})
        
        print(f"  [DEBUG] 缩略图信息: {thumbs}")
        
        if not thumbs:
            print(f"  [WARN] 无缩略图 - fs_id: {fs_id}, filename: {filename}")
            print(f"  [DEBUG]   完整元数据字段: {list(meta.keys())}")
            return False, 0, None
        
        # 选择最大的缩略图
        best_url = None
        best_size = None
        for size in ["url3", "url2", "url1"]:
            if size in thumbs:
                best_url = thumbs[size]
                best_size = size
                break
        
        if not best_url:
            print(f"  [WARN] 无可用缩略图 URL - fs_id: {fs_id}, filename: {filename}")
            print(f"  [DEBUG]   可用缩略图字段: {list(thumbs.keys())}")
            return False, 0, None
        
        print(f"  [DEBUG] 选择缩略图: {best_size}, URL长度: {len(best_url)}")
        
        # 下载缩略图到内存
        success, content = self.download_thumbnail_to_memory(best_url)
        if not success:
            print(f"  [WARN] 下载缩略图失败 - fs_id: {fs_id}, filename: {filename}")
            print(f"  [DEBUG]   错误信息: {content}")
            print(f"  [DEBUG]   URL: {best_url[:100]}...")
            return False, 0, None
        
        print(f"  [DEBUG] 缩略图下载成功，大小: {len(content)} bytes")
        
        # 保存到临时文件
        timestamp = int(time.time() * 1000)
        thumb_filename = f"thumb_{fs_id}_{timestamp}.jpg"
        thumb_path = self.save_thumbnail_temp(content, thumb_filename)
        
        if not thumb_path:
            print(f"  [FAIL] 保存临时缩略图失败 - fs_id: {fs_id}, filename: {filename}")
            return False, 0, None
        
        print(f"  [DEBUG] 临时缩略图保存路径: {thumb_path}")
        
        # 检查 deleted.jpg 是否存在
        if not os.path.exists(DELETED_JPG_PATH):
            print(f"  [FAIL] 参考文件 {DELETED_JPG_PATH} 不存在")
            os.remove(thumb_path)
            return False, 0, None
        
        # 计算相似度
        try:
            results = compare_images.compare_images(thumb_path, DELETED_JPG_PATH)
            similarity = results.get("overall_similarity", 0)
            print(f"  [DEBUG] 相似度计算成功: {similarity:.4f}")
            return similarity >= SIMILARITY_THRESHOLD, similarity, thumb_path
        except Exception as e:
            print(f"  [FAIL] 相似度计算失败 - fs_id: {fs_id}, filename: {filename}")
            print(f"  [DEBUG]   错误信息: {type(e).__name__}: {e}")
            import traceback
            print(f"  [DEBUG]   堆栈: {traceback.format_exc()}")
            return False, 0, thumb_path

    def move_harmonized_video(self, video_info):
        """
        将和谐视频移动到 rubbish_videos
        返回: (success: bool, old_path: str, new_path: str)
        """
        filename = video_info.get("server_filename", "")
        old_path = video_info.get("path", "")
        fs_id = video_info.get("fs_id", "")
        
        if self.dry_run:
            print(f"  [DRY-RUN] 将移动: {old_path} -> {RUBBISH_DIR}/{filename}")
            return True, old_path, f"{RUBBISH_DIR}/{filename}"
        
        # 先确保目录存在
        if not self.ensure_rubbish_dir():
            print(f"  [FAIL] 无法确保目标目录存在")
            return False, old_path, ""
        
        # 执行移动
        success = self.move_remote_file(old_path, RUBBISH_DIR, filename)
        if success:
            new_path = f"{RUBBISH_DIR}/{filename}"
            print(f"  [OK] 已移动")
            return True, old_path, new_path
        else:
            return False, old_path, ""

    def add_move_record(self, fs_id, filename, old_path, new_path, similarity):
        """添加移动记录"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "fs_id": fs_id,
            "filename": filename,
            "old_path": old_path,
            "new_path": new_path,
            "similarity": similarity,
            "recovered": False
        }
        
        with self.history_lock:
            self.move_history.append(record)
            self._save_move_history()

    def _save_move_history(self):
        """保存移动历史到文件"""
        try:
            with open(MOVE_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.move_history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[WARN] 保存历史记录失败: {e}")

    def load_move_history(self):
        """从文件加载移动历史"""
        if os.path.exists(MOVE_HISTORY_FILE):
            try:
                with open(MOVE_HISTORY_FILE, "r", encoding="utf-8") as f:
                    self.move_history = json.load(f)
                print(f"[INFO] 已加载历史记录: {len(self.move_history)} 条")
            except Exception as e:
                print(f"[WARN] 加载历史记录失败: {e}")
                self.move_history = []
        else:
            self.move_history = []

    def load_processed_folders(self):
        """从文件加载已处理的文件夹记录"""
        if os.path.exists(PROCESSED_FOLDERS_FILE):
            try:
                with open(PROCESSED_FOLDERS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 支持两种格式：列表或字典（带时间戳）
                    if isinstance(data, list):
                        self.processed_folders = set(data)
                    elif isinstance(data, dict):
                        self.processed_folders = set(data.get("folders", []))
                    else:
                        self.processed_folders = set()
                print(f"[INFO] 已加载已处理文件夹记录: {len(self.processed_folders)} 个")
            except Exception as e:
                print(f"[WARN] 加载已处理文件夹记录失败: {e}")
                self.processed_folders = set()
        else:
            self.processed_folders = set()

    def save_processed_folders(self):
        """保存已处理的文件夹记录到文件"""
        try:
            # 使用带时间戳的字典格式，便于后续扩展
            data = {
                "folders": list(self.processed_folders),
                "last_updated": datetime.now().isoformat(),
                "base_path": self.probe_base_path
            }
            with open(PROCESSED_FOLDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[WARN] 保存已处理文件夹记录失败: {e}")

    def get_relative_depth(self, path):
        """
        计算相对于扫描基准路径的深度
        返回: (相对深度, 相对路径)
        """
        if not self.probe_base_path or path == self.probe_base_path:
            return 0, path
        
        # 确保路径以 / 开头并去除末尾的 /
        base = self.probe_base_path.rstrip('/')
        current = path.rstrip('/')
        
        if not current.startswith(base):
            return -1, path  # 不在基准路径下
        
        # 计算相对路径
        relative = current[len(base):].lstrip('/')
        if not relative:
            return 0, path
        
        # 计算深度（通过 / 的数量）
        depth = relative.count('/') + 1
        return depth, relative

    def process_video(self, video_info):
        """处理单个视频文件"""
        filename = video_info.get("server_filename", "unknown")
        path = video_info.get("path", "")
        fs_id = video_info.get("fs_id", "")
        
        print(f"\n【处理】{filename}")
        print(f"  路径: {path}")
        
        # 检测是否为和谐视频
        is_harmonized, similarity, thumb_path = self.check_single_video(video_info)
        
        if similarity > 0:
            print(f"  相似度: {similarity:.4f} ({similarity*100:.2f}%)")
            
            if is_harmonized:
                print(f"  [ALERT] 检测到和谐视频！相似度 {similarity*100:.2f}% >= 90%")
                
                # 执行移动
                success, old_path, new_path = self.move_harmonized_video(video_info)
                
                if success:
                    self.add_move_record(fs_id, filename, old_path, new_path, similarity)
                    return True, similarity
                else:
                    print(f"  [FAIL] 移动失败，跳过")
                    return False, similarity
            else:
                if similarity >= 0.75:
                    print(f"  [WARN] 比较相似 ({similarity*100:.1f}%)，建议关注")
                else:
                    print(f"  [OK] 正常视频 ({similarity*100:.1f}%)")
                return False, similarity
        else:
            print(f"  [SKIP] 无法确定相似度")
            return False, 0

    def walk_and_process(self, path="/", visited=None, depth=0):
        """
        递归遍历目录并处理视频文件
        使用线程池并发处理视频，边扫描边处理
        返回: (和谐视频数, 总视频数, 视频总大小, 是否完成)
        """
        if visited is None:
            visited = set()
        
        # 避免循环
        if path in visited:
            return 0, 0, 0, True
        visited.add(path)
        
        # 计算相对深度
        rel_depth, rel_path = self.get_relative_depth(path)
        
        # 检查是否已处理（仅对前三层深度进行检查，force_rescan 时跳过此检查）
        if not self.force_rescan and rel_depth <= 3 and path in self.processed_folders:
            indent = "  " * depth
            print(f"{indent}[跳过] {path} (已处理)")
            return 0, 0, 0, True
        
        # 输出当前扫描的目录
        indent = "  " * depth
        print(f"{indent}[扫描] {path}")
        
        files = self.list_files(path)
        
        # 区分"空目录"和"读取失败"
        if files is None:
            # 读取失败，不标记为已处理，返回失败状态以便下次重试
            indent = "  " * depth
            print(f"{indent}[ERROR] 目录读取失败，跳过并保留重试机会: {path}")
            return 0, 0, 0, False  # 返回 False 表示未完成
        
        if not files:
            # 空目录，标记为完成（如果是前两层）
            if rel_depth <= 2:
                with self.processed_folders_lock:
                    self.processed_folders.add(path)
                    self.save_processed_folders()
            return 0, 0, 0, True
        
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
        
        # 计算当前目录视频文件的总大小
        current_dir_size = 0
        for v in video_files:
            file_size = v.get("size", 0)
            if file_size:
                current_dir_size += file_size
        
        # 累加到全局总大小（线程安全）
        if current_dir_size > 0:
            with self.total_size_lock:
                self.total_size += current_dir_size
        
        # 处理找到的视频文件（使用线程池并发处理）
        harmonized_count = 0
        total_count = len(video_files)
        video_tasks_completed = True
        
        if video_files:
            print(f"{indent}  发现 {total_count} 个视频文件 ({format_size(current_dir_size)})，使用 {self.max_workers} 线程并发检测...")
            
            # 为视频补充路径信息
            for v in video_files:
                if not v.get("path"):
                    v["path"] = f"{path}/{v.get('server_filename')}"
            
            # 使用线程池并发处理视频
            futures = {}
            for video in video_files:
                future = self.executor.submit(self.process_video, video)
                futures[future] = video
            
            # 收集结果
            for future in as_completed(futures):
                video = futures[future]
                try:
                    is_moved, similarity = future.result()
                    if is_moved:
                        harmonized_count += 1
                except Exception as e:
                    print(f"{indent}  [ERROR] 处理视频失败 {video.get('server_filename')}: {e}")
                    video_tasks_completed = False
        
        # 递归处理子目录
        sub_harmonized = 0
        sub_total = 0
        sub_size = 0
        all_subdirs_completed = True
        
        for subdir in subdirs:
            subdir_path = subdir.get("path", f"{path}/{subdir.get('server_filename')}")
            h, t, s, completed = self.walk_and_process(subdir_path, visited, depth + 1)
            sub_harmonized += h
            sub_total += t
            sub_size += s
            if not completed:
                all_subdirs_completed = False
        
        # 判断是否完成：所有视频处理完 + 所有子目录处理完
        is_completed = video_tasks_completed and all_subdirs_completed
        
        # 如果完成且深度 <= 2，记录到已处理文件夹（线程安全）
        if is_completed and rel_depth <= 2:
            with self.processed_folders_lock:
                self.processed_folders.add(path)
                self.save_processed_folders()
            print(f"{indent}[完成] {path} 已记录")
        
        return harmonized_count + sub_harmonized, total_count + sub_total, current_dir_size + sub_size, is_completed

    def run_cleanup(self, probe_path):
        """执行清理任务"""
        # 设置扫描基准路径
        self.probe_base_path = probe_path.rstrip('/')
        
        # 加载已处理的文件夹记录
        self.load_processed_folders()
        
        print("=" * 60)
        print("百度网盘和谐视频清理工具")
        print("=" * 60)
        print(f"扫描路径: {probe_path}")
        print(f"目标目录: {RUBBISH_DIR}")
        print(f"相似度阈值: {SIMILARITY_THRESHOLD * 100}%")
        print(f"并发线程数: {self.max_workers}")
        print(f"预览模式: {'是' if self.dry_run else '否'}")
        print(f"已处理文件夹: {len(self.processed_folders)} 个")
        print("=" * 60 + "\n")
        
        if not os.path.exists(DELETED_JPG_PATH):
            print(f"[ERROR] 参考文件 {DELETED_JPG_PATH} 不存在！")
            print("请将和谐视频的缩略图保存为 deleted.jpg")
            return False
        
        # 确保远程目录存在（非预览模式下）
        if not self.dry_run:
            print("[INFO] 检查目标目录...")
            if not self.ensure_rubbish_dir():
                print("[ERROR] 无法确保目标目录存在，退出")
                return False
        
        # 初始化线程池
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        
        # 开始扫描和处理
        print(f"\n[INFO] 开始扫描和处理视频文件（{self.max_workers}线程并发模式）...\n")
        start_time = time.time()
        
        try:
            harmonized, total, total_size, _ = self.walk_and_process(probe_path)
        finally:
            # 关闭线程池
            self.executor.shutdown(wait=True)
            self.executor = None
        
        elapsed = time.time() - start_time
        
        print("\n" + "=" * 60)
        print("清理完成！")
        print("=" * 60)
        print(f"扫描视频总数: {total}")
        print(f"扫描视频总大小: {format_size(total_size)}")
        print(f"识别和谐视频: {harmonized}")
        print(f"移动到: {RUBBISH_DIR}")
        print(f"已处理文件夹: {len(self.processed_folders)} 个")
        if self.dry_run:
            print(f"预览模式：未实际移动文件")
        print(f"记录文件: {MOVE_HISTORY_FILE}")
        print(f"耗时: {elapsed:.2f} 秒")
        print("=" * 60)
        
        return True

    def rollback_moves(self):
        """
        回滚操作：将移动过的文件移回原位
        """
        self.load_move_history()
        
        if not self.move_history:
            print("[INFO] 没有历史记录需要回滚")
            return
        
        # 筛选出未恢复的记录
        to_recover = [r for r in self.move_history if not r.get("recovered", False)]
        
        if not to_recover:
            print("[INFO] 没有需要回滚的记录")
            return
        
        print("=" * 60)
        print("执行回滚操作")
        print("=" * 60)
        print(f"需要回滚的记录数: {len(to_recover)}")
        print()
        
        success_count = 0
        
        for record in to_recover:
            fs_id = record.get("fs_id")
            filename = record.get("filename")
            old_path = record.get("old_path")
            
            # 从 old_path 中提取原目录
            old_dir = os.path.dirname(old_path)
            current_path = f"{RUBBISH_DIR}/{filename}"
            
            print(f"【回滚】{filename}")
            if self.dry_run:
                print(f"  源路径: {current_path}")
                print(f"  目标: {old_dir}")
                print(f"  [DRY-RUN] 将回滚文件")
                record["recovered"] = True
                success_count += 1
                continue
            
            print(f"  源路径: {current_path}")
            print(f"  目标: {old_dir}")
            
            # 检查原目录是否存在，不存在则创建
            if not self.check_remote_dir_exists(old_dir):
                self.create_remote_directory(old_dir)
            
            # 执行移动
            success = self.move_remote_file(current_path, old_dir, filename)
            
            if success:
                print(f"  [OK] 回滚成功")
                record["recovered"] = True
                record["recovery_timestamp"] = datetime.now().isoformat()
                success_count += 1
            else:
                print(f"  [FAIL] 回滚失败")
        
        # 保存更新后的历史记录
        self._save_move_history()
        
        print("\n" + "=" * 60)
        print("回滚完成！")
        print(f"成功回滚: {success_count}/{len(to_recover)}")
        print("=" * 60)


def print_usage():
    """打印使用说明"""
    print("""
百度网盘和谐视频清理工具

用法:
  python baidupan_cleaner.py [选项]

选项:
  --path <路径>          指定扫描路径 (默认: /my_files)
  --threads <数量>       并发线程数 (默认: 5)
  --rollback             执行回滚操作
  --dry-run              预览模式，不实际移动文件
  --force                强制重新扫描，忽略已处理文件夹记录
  --help                 显示此帮助

示例:
  python baidupan_cleaner.py                       # 扫描 /my_files，5线程
  python baidupan_cleaner.py --path /videos        # 扫描 /videos
  python baidupan_cleaner.py --threads 10          # 使用10个线程
  python baidupan_cleaner.py --dry-run             # 预览模式
  python baidupan_cleaner.py --force               # 强制重新扫描全部
  python baidupan_cleaner.py --rollback            # 执行回滚
  python baidupan_cleaner.py --rollback --dry-run  # 预览回滚
""")


def main():
    """主函数"""
    # 解析命令行参数
    probe_path = PROBE_PATH
    do_rollback = False
    dry_run = False
    force_rescan = False
    max_workers = DEFAULT_THREADS
    
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--help" or arg == "-h":
            print_usage()
            sys.exit(0)
        elif arg == "--rollback":
            do_rollback = True
        elif arg == "--dry-run":
            dry_run = True
        elif arg == "--force":
            force_rescan = True
        elif arg == "--path":
            if i + 1 < len(args):
                probe_path = args[i + 1]
                i += 1
            else:
                print("[ERROR] --path 需要指定一个路径")
                sys.exit(1)
        elif arg == "--threads":
            if i + 1 < len(args):
                try:
                    max_workers = int(args[i + 1])
                    if max_workers < 1 or max_workers > 50:
                        print("[ERROR] 线程数必须在 1-50 之间")
                        sys.exit(1)
                except ValueError:
                    print("[ERROR] --threads 需要指定一个整数")
                    sys.exit(1)
                i += 1
            else:
                print("[ERROR] --threads 需要指定线程数量")
                sys.exit(1)
        i += 1
    
    # 创建清理器实例
    cleaner = BaiduPanCleaner(dry_run=dry_run, max_workers=max_workers, force_rescan=force_rescan)
    
    print("=" * 60)
    print("百度网盘视频和谐状态检测与清理工具")
    print("=" * 60)
    
    # Token 管理
    cleaner.load_token()
    
    if not cleaner.access_token:
        if not cleaner.auth_flow():
            print("授权失败，程序退出")
            sys.exit(1)
        cleaner.save_token()
    
    if do_rollback:
        # 执行回滚
        cleaner.rollback_moves()
    else:
        # 执行清理
        success = cleaner.run_cleanup(probe_path)
        if not success:
            sys.exit(1)
    
    print("\n程序结束！")


if __name__ == "__main__":
    main()