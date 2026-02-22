#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度网盘目录扁平化工具
功能：
1. 递归扫描指定目录
2. 将超过 n 级子目录的文件全部移动到第 n 级目录
3. 文件名冲突时自动添加序号（如 电影.mp4 -> 电影_1.mp4）
4. 记录移动历史到 flatten_history.json，支持回滚

使用方式：
  python baidupan_flatten.py                       # 执行扁平化（默认深度2）
  python baidupan_flatten.py --rollback            # 执行回滚
  python baidupan_flatten.py --path /test          # 指定扫描路径
  python baidupan_flatten.py --depth 3             # 指定目标深度为3
  python baidupan_flatten.py --dry-run             # 仅预览，不移动
  python baidupan_flatten.py --force               # 强制重新扫描全部
"""

import json
import os
import sys
import time
import hashlib
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
from openapi_client.api import auth_api, fileinfo_api, filemanager_api, fileupload_api
from openapi_client.exceptions import ApiException


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
        "DEFAULT_THREADS": 5,
        "DEFAULT_DEPTH": 2
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

# 默认目标深度
DEFAULT_DEPTH = CONFIG.get("DEFAULT_DEPTH", 2)

# 是否仅预览模式（不实际移动文件）
DRY_RUN = False

# 移动历史记录文件
MOVE_HISTORY_FILE = "flatten_history.json"

# 已处理文件夹记录文件（用于增量处理）
PROCESSED_FOLDERS_FILE = "flatten_processed.json"

# 默认并发线程数
DEFAULT_THREADS = CONFIG.get("DEFAULT_THREADS", 5)

# OAuth2.0 回调地址
REDIRECT_URI = CONFIG.get("REDIRECT_URI", "oob")

# ============================================================


class BaiduPanFlattener:
    """百度网盘目录扁平化工具 (SDK 版本)"""

    # OAuth2.0 相关 URL
    AUTHORIZE_URL = "https://openapi.baidu.com/oauth/2.0/authorize"
    TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
    DEV_PIN_URL = "https://openapi.baidu.com/device/code"

    def __init__(self, dry_run=False, max_workers=DEFAULT_THREADS, force_rescan=False, target_depth=DEFAULT_DEPTH):
        self.access_token = None
        self.refresh_token = None
        self.dry_run = dry_run
        self.force_rescan = force_rescan  # 强制重新扫描，忽略增量处理记录
        self.target_depth = target_depth  # 目标深度
        self.move_history = []
        
        # 增量处理相关
        self.processed_folders = set()  # 已处理的文件夹集合
        self.probe_base_path = None     # 扫描基准路径
        
        # 多线程相关
        self.max_workers = max_workers
        self.executor = None  # 线程池，在run_flatten中初始化
        
        # 文件名冲突跟踪：{目标目录: {文件名: 计数}}
        self.filename_conflict_tracker = {}
        self.conflict_lock = threading.Lock()
        
        # 初始化 SDK Configuration
        self.configuration = openapi_client.Configuration()
        self.api_client = openapi_client.ApiClient(self.configuration)
        
        # 初始化 API 实例
        self.auth_api = auth_api.AuthApi(self.api_client)
        self.fileinfo_api = fileinfo_api.FileinfoApi(self.api_client)
        self.filemanager_api = filemanager_api.FilemanagerApi(self.api_client)
        self.fileupload_api = fileupload_api.FileuploadApi(self.api_client)
        
        # 锁用于线程安全地更新历史记录和增量记录
        self.history_lock = threading.Lock()
        self.processed_folders_lock = threading.Lock()
        
        # 文件统计
        self.total_files = 0
        self.moved_files = 0
        self.total_size = 0
        self.stats_lock = threading.Lock()
        
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

    def move_remote_file(self, source_path, dest_dir, new_filename):
        """
        移动远程文件
        :param source_path: 源文件完整路径
        :param dest_dir: 目标目录
        :param new_filename: 新文件名
        :return: 是否成功
        """
        filelist = json.dumps([{
            "path": source_path,
            "dest": dest_dir,
            "newname": new_filename,
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
            print(f"[FAIL] 移动文件失败 ({new_filename}): errno={errno}, msg={errmsg}")
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

    def get_path_depth(self, path):
        """
        计算路径相对于扫描基准路径的深度
        扫描路径本身为深度0
        
        :param path: 文件或目录的完整路径
        :return: 深度值，如果不在基准路径下返回 -1
        """
        if not self.probe_base_path:
            return -1
        
        # 标准化路径
        base = self.probe_base_path.rstrip('/')
        current = path.rstrip('/')
        
        if not current.startswith(base):
            return -1
        
        # 如果是基准路径本身，深度为0
        if current == base:
            return 0
        
        # 计算相对路径部分
        relative = current[len(base):].lstrip('/')
        if not relative:
            return 0
        
        # 深度 = 路径分隔符数量 + 1
        depth = relative.count('/') + 1
        return depth

    def get_target_dir_for_file(self, file_path):
        """
        根据文件路径计算目标目录（第n级目录）
        
        :param file_path: 文件的完整路径
        :return: 目标目录路径，如果在目标深度内返回 None
        """
        depth = self.get_path_depth(file_path)
        
        # 如果文件深度 <= 目标深度，不需要移动
        if depth <= self.target_depth:
            return None
        
        # 构建目标目录路径
        base = self.probe_base_path.rstrip('/')
        relative = file_path[len(base):].lstrip('/')
        
        # 分割路径
        parts = relative.split('/')
        
        # 目标目录：保留前 self.target_depth 个路径部分
        if self.target_depth == 0:
            target_dir = base
        else:
            target_parts = parts[:self.target_depth]
            target_dir = base + '/' + '/'.join(target_parts)
        
        return target_dir

    def get_unique_filename(self, dest_dir, original_filename):
        """
        获取唯一文件名，处理冲突时添加序号
        
        :param dest_dir: 目标目录
        :param original_filename: 原始文件名
        :return: 唯一的文件名
        """
        with self.conflict_lock:
            # 初始化该目录的跟踪器
            if dest_dir not in self.filename_conflict_tracker:
                self.filename_conflict_tracker[dest_dir] = {}
            
            tracker = self.filename_conflict_tracker[dest_dir]
            
            # 如果文件名未被使用，直接返回
            if original_filename not in tracker:
                tracker[original_filename] = 0
                return original_filename
            
            # 文件名已被使用，生成带序号的新文件名
            tracker[original_filename] += 1
            count = tracker[original_filename]
            
            # 分离文件名和扩展名
            name_parts = original_filename.rsplit('.', 1)
            if len(name_parts) == 2:
                name, ext = name_parts
                new_filename = f"{name}_{count}.{ext}"
            else:
                new_filename = f"{original_filename}_{count}"
            
            return new_filename

    def add_move_record(self, fs_id, filename, old_path, new_path, old_dir, new_filename):
        """添加移动记录"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "fs_id": fs_id,
            "filename": filename,
            "old_path": old_path,
            "new_path": new_path,
            "old_dir": old_dir,
            "new_filename": new_filename,
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
            data = {
                "folders": list(self.processed_folders),
                "last_updated": datetime.now().isoformat(),
                "base_path": self.probe_base_path,
                "target_depth": self.target_depth
            }
            with open(PROCESSED_FOLDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[WARN] 保存已处理文件夹记录失败: {e}")

    def process_file(self, file_info):
        """
        处理单个文件
        
        :param file_info: 文件信息字典
        :return: (是否移动, 原路径, 新路径)
        """
        filename = file_info.get("server_filename", "unknown")
        old_path = file_info.get("path", "")
        fs_id = file_info.get("fs_id", "")
        is_dir = file_info.get("isdir") == 1
        
        # 跳过目录
        if is_dir:
            return False, old_path, ""
        
        # 计算目标目录
        target_dir = self.get_target_dir_for_file(old_path)
        
        if target_dir is None:
            # 文件在目标深度内，不需要移动
            return False, old_path, ""
        
        # 获取唯一文件名
        new_filename = self.get_unique_filename(target_dir, filename)
        new_path = f"{target_dir}/{new_filename}"
        
        print(f"  [移动] {old_path}")
        print(f"       -> {new_path}")
        
        if self.dry_run:
            print(f"       [DRY-RUN] 预览模式，不实际移动")
            return True, old_path, new_path
        
        # 确保目标目录存在
        if not self.check_remote_dir_exists(target_dir):
            if not self.create_remote_directory(target_dir):
                print(f"  [FAIL] 无法创建目标目录: {target_dir}")
                return False, old_path, ""
        
        # 执行移动
        success = self.move_remote_file(old_path, target_dir, new_filename)
        
        if success:
            self.add_move_record(fs_id, filename, old_path, new_path, 
                               os.path.dirname(old_path), new_filename)
            with self.stats_lock:
                self.moved_files += 1
            return True, old_path, new_path
        else:
            return False, old_path, ""

    def walk_and_process(self, path="/", visited=None, depth=0):
        """
        递归遍历目录并处理文件
        返回: (是否完成)
        """
        if visited is None:
            visited = set()
        
        # 避免循环
        if path in visited:
            return True
        visited.add(path)
        
        # 计算当前深度
        current_depth = self.get_path_depth(path)
        
        # 检查是否已处理（force_rescan 时跳过此检查）
        if not self.force_rescan and current_depth <= self.target_depth and path in self.processed_folders:
            indent = "  " * current_depth
            print(f"{indent}[跳过] {path} (已处理)")
            return True
        
        # 输出当前扫描的目录
        indent = "  " * (current_depth if current_depth >= 0 else 0)
        print(f"{indent}[扫描] {path} (深度: {current_depth})")
        
        files = self.list_files(path)
        
        # 区分"空目录"和"读取失败"
        if files is None:
            print(f"{indent}[ERROR] 目录读取失败: {path}")
            return False
        
        if not files:
            # 空目录，标记为完成
            if current_depth <= self.target_depth:
                with self.processed_folders_lock:
                    self.processed_folders.add(path)
                    self.save_processed_folders()
            return True
        
        # 分离文件和目录
        regular_files = []
        subdirs = []
        
        for f in files:
            is_dir = f.get("isdir") == 1
            if is_dir:
                subdirs.append(f)
            else:
                regular_files.append(f)
        
        # 更新统计
        if regular_files:
            with self.stats_lock:
                self.total_files += len(regular_files)
                for f in regular_files:
                    self.total_size += f.get("size", 0)
        
        # 处理文件（仅处理深度超过目标的文件）
        files_to_move = []
        for f in regular_files:
            file_path = f.get("path", "")
            if self.get_path_depth(file_path) > self.target_depth:
                files_to_move.append(f)
        
        if files_to_move:
            print(f"{indent}  发现 {len(files_to_move)} 个文件需要移动")
            
            # 使用线程池并发处理
            futures = {}
            for file_info in files_to_move:
                future = self.executor.submit(self.process_file, file_info)
                futures[future] = file_info
            
            # 收集结果
            for future in as_completed(futures):
                file_info = futures[future]
                try:
                    moved, old_path, new_path = future.result()
                except Exception as e:
                    print(f"{indent}  [ERROR] 处理文件失败 {file_info.get('server_filename')}: {e}")
        
        # 递归处理子目录
        all_subdirs_completed = True
        for subdir in subdirs:
            subdir_path = subdir.get("path", f"{path}/{subdir.get('server_filename')}")
            completed = self.walk_and_process(subdir_path, visited, depth + 1)
            if not completed:
                all_subdirs_completed = False
        
        # 如果完成且深度在目标范围内，记录到已处理文件夹
        if all_subdirs_completed and current_depth <= self.target_depth:
            with self.processed_folders_lock:
                self.processed_folders.add(path)
                self.save_processed_folders()
            print(f"{indent}[完成] {path} 已记录")
        
        return all_subdirs_completed

    def run_flatten(self, probe_path):
        """执行扁平化任务"""
        # 设置扫描基准路径
        self.probe_base_path = probe_path.rstrip('/')
        
        # 加载已处理的文件夹记录
        self.load_processed_folders()
        
        print("=" * 60)
        print("百度网盘目录扁平化工具")
        print("=" * 60)
        print(f"扫描路径: {probe_path}")
        print(f"目标深度: {self.target_depth}")
        print(f"并发线程数: {self.max_workers}")
        print(f"预览模式: {'是' if self.dry_run else '否'}")
        print(f"已处理文件夹: {len(self.processed_folders)} 个")
        print("=" * 60 + "\n")
        
        # 初始化线程池
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        
        # 开始扫描和处理
        print(f"\n[INFO] 开始扫描和处理文件（{self.max_workers}线程并发模式）...\n")
        start_time = time.time()
        
        try:
            self.walk_and_process(probe_path)
        finally:
            # 关闭线程池
            self.executor.shutdown(wait=True)
            self.executor = None
        
        elapsed = time.time() - start_time
        
        print("\n" + "=" * 60)
        print("扁平化完成！")
        print("=" * 60)
        print(f"扫描文件总数: {self.total_files}")
        print(f"扫描文件总大小: {format_size(self.total_size)}")
        print(f"已移动文件数: {self.moved_files}")
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
            filename = record.get("filename")
            old_path = record.get("old_path")
            old_dir = record.get("old_dir")
            new_filename = record.get("new_filename", filename)
            current_path = record.get("new_path", f"{os.path.dirname(old_path)}/{new_filename}")
            
            print(f"【回滚】{new_filename}")
            if self.dry_run:
                print(f"  源路径: {current_path}")
                print(f"  目标: {old_dir}")
                print(f"  原文件名: {filename}")
                print(f"  [DRY-RUN] 将回滚文件")
                record["recovered"] = True
                success_count += 1
                continue
            
            print(f"  源路径: {current_path}")
            print(f"  目标: {old_dir}")
            print(f"  原文件名: {filename}")
            
            # 检查原目录是否存在，不存在则创建
            if not self.check_remote_dir_exists(old_dir):
                self.create_remote_directory(old_dir)
            
            # 执行移动，恢复原文件名
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
百度网盘目录扁平化工具

用法:
  python baidupan_flatten.py [选项]

选项:
  --path <路径>          指定扫描路径 (默认: /my_files)
  --depth <数量>         目标深度 (默认: 2)
  --threads <数量>       并发线程数 (默认: 5)
  --rollback             执行回滚操作
  --dry-run              预览模式，不实际移动文件
  --force                强制重新扫描，忽略已处理文件夹记录
  --help                 显示此帮助

示例:
  python baidupan_flatten.py                       # 扫描 /my_files，扁平化到第2层
  python baidupan_flatten.py --path /videos        # 扫描 /videos
  python baidupan_flatten.py --depth 3             # 扁平化到第3层
  python baidupan_flatten.py --threads 10          # 使用10个线程
  python baidupan_flatten.py --dry-run             # 预览模式
  python baidupan_flatten.py --force               # 强制重新扫描全部
  python baidupan_flatten.py --rollback            # 执行回滚
  python baidupan_flatten.py --rollback --dry-run  # 预览回滚
""")


def main():
    """主函数"""
    # 解析命令行参数
    probe_path = PROBE_PATH
    do_rollback = False
    dry_run = False
    force_rescan = False
    max_workers = DEFAULT_THREADS
    target_depth = DEFAULT_DEPTH
    
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
        elif arg == "--depth":
            if i + 1 < len(args):
                try:
                    target_depth = int(args[i + 1])
                    if target_depth < 0:
                        print("[ERROR] 目标深度不能为负数")
                        sys.exit(1)
                except ValueError:
                    print("[ERROR] --depth 需要指定一个非负整数")
                    sys.exit(1)
                i += 1
            else:
                print("[ERROR] --depth 需要指定深度值")
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
    
    # 创建扁平化工具实例
    flattener = BaiduPanFlattener(
        dry_run=dry_run, 
        max_workers=max_workers, 
        force_rescan=force_rescan,
        target_depth=target_depth
    )
    
    print("=" * 60)
    print("百度网盘目录扁平化工具")
    print("=" * 60)
    
    # Token 管理
    flattener.load_token()
    
    if not flattener.access_token:
        if not flattener.auth_flow():
            print("授权失败，程序退出")
            sys.exit(1)
        flattener.save_token()
    
    if do_rollback:
        # 执行回滚
        flattener.rollback_moves()
    else:
        # 执行扁平化
        success = flattener.run_flatten(probe_path)
        if not success:
            sys.exit(1)
    
    print("\n程序结束！")


if __name__ == "__main__":
    main()