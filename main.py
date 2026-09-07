"""
astrbot_plugin_qlpanel —— QLpanel-pro v1.0.0 —— 青龙面板对接·通用签到平台

架构（v1.0.0 安全重构）：
    - 账号数据只存储在插件缓存目录 users.json（本地文件），青龙面板不再持久存储任何账号
    - 脚本运行时按用户临时注入运行数据（用完即删），其他用户的数据不会被运行，数据严格隔离
    - 迁移：/ql backup 导出账号文件，拷贝到新服务器后 /ql restore 一键恢复
    - 每个用户可自定义签到时间与推送开关，定时到点按用户单独触发
    - 定时触发带模块级去重（90 秒窗口），防止 AstrBot 多实例残留导致重复触发刷屏
    - 每条命令回复末尾附随机每日一言；帮助分区：/ql help、/ql help 3d、/ql help zibi、/ql admin

青龙 OpenAPI（v2.10+ / v3.x，仅 Client ID / Client Secret 认证）：
    GET    /open/auth/token       获取 OpenAPI token
    GET    /open/envs             获取环境变量列表
    POST   /open/envs             添加环境变量
    PUT    /open/envs             更新环境变量
    DELETE /open/envs             删除环境变量
    GET    /open/crons            获取定时任务列表
    PUT    /open/crons/run        运行任务
    GET    /open/crons/{id}/log   获取任务日志路径
    GET    /open/logs/{path}      获取日志内容
"""

import asyncio
import json
import os
import random
import re
import shlex
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, register

PLUGIN_ID = "astrbot_plugin_qlpanel"
PLUGIN_NAME = "QLpanel-pro"
PLUGIN_VERSION = "1.0.0"
PLUGIN_AUTHOR = "请叫我大王"
PLUGIN_REPO = "https://github.com/725361764/astrbot_plugin_qlpanel"

# 脚本运行时临时注入的环境变量名（用完即删，青龙不持久存储）
RUN_ENV_3D = "BIZHI_RUN_DATA"
RUN_ENV_ZIBI = "ZIBI_RUN_DATA"
# 旧版持久化环境变量（v2.x 遗留），init 时检测并提示清理
LEGACY_ENV_3D = "BIZHI_COOKIE"
LEGACY_ENV_ZIBI = "ZIBI_CONFIG"

# 上传文件大小上限（字节）
MAX_UPLOAD_SIZE = 2 * 1024 * 1024

# 每日一言（每条命令回复末尾随机附一句）
DAILY_QUOTES = [
    "今天也要元气满满地生活呀～",
    "好运藏在努力里，加油！",
    "保持热爱，奔赴山海。",
    "生活明朗，万物可爱。",
    "慢慢来，比较快。",
    "所有的美好都值得等待。",
    "心怀浪漫宇宙，也珍惜人间日常。",
    "愿你眼里有光，心中有暖。",
    "生活不止眼前的苟且，还有诗和远方。",
    "凡是过往，皆为序章。",
    "心之所向，素履以往。",
    "温柔半两，从容一生。",
    "把日子过成诗，简单而精致。",
    "你现在的努力，是未来的底气。",
    "热爱可抵岁月漫长。",
    "星光不问赶路人，时光不负有心人。",
    "每天都是新的一天，别辜负好时光。",
    "愿你所求皆如愿，所行化坦途。",
    "笑一个吧，功成名就不是目的。",
    "趁年轻，多折腾，少留遗憾。",
    "答案都在时间里，慢慢来。",
    "心若向阳，无畏悲伤。",
    "越努力，越幸运。",
    "世界很大，请带着好奇心出发。",
]

# 定时触发去重：key=f"{YYYY-MM-DD HH:MM}" -> 最近触发时间戳。
# AstrBot 热更新/重复加载插件时可能残留多个实例，模块级变量在同一进程内共享，
# 用 90 秒窗口防止同一时间点被多个实例重复触发（修复定时重复刷屏）。
_TRIGGER_LOG: Dict[str, float] = {}

try:
    from astrbot.api.star import StarTools

    def _get_plugin_data_dir() -> Path:
        return Path(StarTools.get_data_dir(PLUGIN_ID))
except Exception:  # 兼容旧版本 AstrBot
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        def _get_plugin_data_dir() -> Path:
            return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_ID
    except Exception:

        def _get_plugin_data_dir() -> Path:
            return Path("data") / "plugin_data" / PLUGIN_ID


def _mask(value: str, keep: int = 6) -> str:
    """脱敏显示长字符串：保留前 keep 位，其余打码"""
    value = value or ""
    if len(value) <= keep + 3:
        return value
    return value[:keep] + "******"


# ---------------------------------------------------------------------------
# 青龙面板 API 客户端
# ---------------------------------------------------------------------------
class QinglongClient:
    """封装青龙面板 OpenAPI 的异步客户端。

    仅支持 OpenAPI 认证（Client ID / Client Secret）：
    - 接口前缀固定 /open，登录 GET /open/auth/token
    自动处理 token 失效（401/403）时重新登录重试。
    """

    def __init__(
        self,
        base_url: str,
        api_prefix: str = "/open",
        client_id: str = "",
        client_secret: str = "",
        token: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_prefix = api_prefix.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = token

    @staticmethod
    def env_id(env: dict):
        """兼容新版（数字 id）与旧版（字符串 _id）环境变量主键"""
        return env.get("_id") if env.get("_id") is not None else env.get("id")

    async def _login(self, session: aiohttp.ClientSession) -> str:
        try:
            if not self.client_id or not self.client_secret:
                raise RuntimeError("未配置 Client ID / Client Secret，请在 WebUI 插件配置中填写")
            # OpenAPI 认证
            async with session.get(
                f"{self.base_url}/open/auth/token",
                params={"client_id": self.client_id, "client_secret": self.client_secret},
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except Exception:
                    raise RuntimeError(f"OpenAPI 登录返回异常: HTTP {resp.status}，{_mask(text, 60)}")
                code = data.get("code")
                token = (data.get("data") or {}).get("token") if isinstance(data.get("data"), dict) else None
                if resp.status in (401, 403) or (code is not None and code not in (200, 0)) or not token:
                    raise RuntimeError(
                        f"OpenAPI 登录失败: {data.get('message') or data.get('msg') or data.get('code') or resp.status}"
                    )
                self.token = token
                return token
        except aiohttp.ClientError as e:
            raise RuntimeError(f"无法连接青龙面板({self.base_url}): {e.__class__.__name__}: {e}")

    async def request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{self.api_prefix}{path}"
        return await self._request_raw(method, url, path, **kwargs)

    async def _request_raw(self, method: str, url: str, path_label: str = "", **kwargs) -> dict:
        """直接请求指定 URL（不走 api_prefix），复用 Token 与错误处理。path_label 仅用于日志。"""
        headers = dict(kwargs.pop("headers", {}))
        kwargs.setdefault("timeout", aiohttp.ClientTimeout(total=30))
        label = path_label or url

        async with aiohttp.ClientSession() as session:
            if not self.token:
                self.token = await self._login(session)
            headers["Authorization"] = f"Bearer {self.token}"

            for attempt in range(2):
                try:
                    async with session.request(method, url, headers=headers, **kwargs) as resp:
                        text = await resp.text()
                        try:
                            data = json.loads(text)
                        except Exception:
                            raise RuntimeError(f"接口 {label} 返回非 JSON: HTTP {resp.status}，{_mask(text, 120)}")
                        if resp.status in (401, 403) and attempt == 0:
                            self.token = await self._login(session)
                            headers["Authorization"] = f"Bearer {self.token}"
                            continue
                        code = data.get("code")
                        if resp.status >= 400 or (code is not None and code not in (200, 0)):
                            if resp.status == 400:
                                logger.warning(
                                    f"[{PLUGIN_ID}] {method} {label} 400 完整响应: {_mask(text, 800)}"
                                )
                            raise RuntimeError(
                                f"接口 {label} 调用失败: HTTP {resp.status}，"
                                f"{data.get('message') or data.get('msg') or data}"
                            )
                        return data
                except aiohttp.ClientError as e:
                    raise RuntimeError(f"请求 {label} 网络错误: {e.__class__.__name__}: {e}")
                except asyncio.TimeoutError:
                    raise RuntimeError(f"请求 {label} 超时")
            raise RuntimeError(f"请求 {label} 失败")

    # ---- 环境变量 ----
    async def get_envs(self) -> List[dict]:
        data = await self.request("GET", "/envs")
        payload = data.get("data") or []
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("items") or payload.get("list") or []
        return [e for e in payload if isinstance(e, dict)]

    async def add_env(self, name: str, value: str, remarks: str = "") -> dict:
        """新增环境变量。POST /envs 统一用数组（OpenAPI 也要求数组）。"""
        return await self.request("POST", "/envs", json=[{"name": name, "value": value, "remarks": remarks}])

    async def update_env(self, env: dict, name: str, value: str, remarks: str = "") -> dict:
        """更新环境变量。OpenAPI（/open）期望单个对象且不允许 status 字段。"""
        eid = self.env_id(env)
        body = {"name": name, "value": value, "remarks": remarks}
        if env.get("_id") is not None:
            body["_id"] = eid
        else:
            body["id"] = eid
        if self.api_prefix == "/open":
            return await self.request("PUT", "/envs", json=body)
        try:
            return await self.request("PUT", "/envs", json=[body])
        except RuntimeError as e:
            if "HTTP 400" in str(e):
                logger.info(f"[{PLUGIN_ID}] PUT /envs 数组格式 400，降级为单个对象重试")
                return await self.request("PUT", "/envs", json=body)
            raise

    async def delete_envs(self, env_ids: List) -> dict:
        return await self.request("DELETE", "/envs", json=env_ids)

    async def find_envs_by_name(self, name: str) -> List[dict]:
        envs = await self.get_envs()
        return [e for e in envs if e.get("name") == name]

    # ---- 定时任务 ----
    async def get_crons(self, search: str = "") -> List[dict]:
        params = {"searchValue": search, "pageSize": 100, "page": 1}
        data = await self.request("GET", "/crons", params=params)
        payload = data.get("data") or []
        # 兼容嵌套分页结构 {"data": {"data": [...]}}（部分版本/OpenAPI）
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("items") or payload.get("list") or []
        # 只保留字典元素，跳过字符串等异常类型
        return [c for c in payload if isinstance(c, dict)]

    async def run_cron(self, cron_id: int) -> dict:
        """触发任务运行。先试数组 [id]，400 时降级单个对象（兼容 OpenAPI 不同版本）。"""
        try:
            return await self.request("PUT", "/crons/run", json=[cron_id])
        except RuntimeError as e:
            if "HTTP 400" in str(e) and self.api_prefix == "/open":
                logger.info(f"[{PLUGIN_ID}] /crons/run 数组格式 400，降级为单个对象重试")
                return await self.request("PUT", "/crons/run", json={"id": cron_id})
            raise

    async def update_cron(self, cron_id, name=None, command=None, schedule=None,
                          remarks: str = "") -> dict:
        """更新定时任务（PUT 单个对象，OpenAPI 不接受 status 字段）。"""
        body = {"id": cron_id}
        if name is not None:
            body["name"] = name
        if command is not None:
            body["command"] = command
        if schedule is not None:
            body["schedule"] = schedule
        if remarks:
            body["remarks"] = remarks
        try:
            return await self.request("PUT", "/crons", json=body)
        except RuntimeError as e:
            if "HTTP 400" in str(e) and self.api_prefix == "/open":
                logger.info(f"[{PLUGIN_ID}] PUT /crons 带 id 400，去掉 id 重试")
                body.pop("id", None)
                return await self.request("PUT", "/crons", json=body)
            raise

    async def delete_cron(self, cron_id) -> dict:
        """删除定时任务。OpenAPI 先试数组 [id]，400 时降级单个对象。"""
        try:
            return await self.request("DELETE", "/crons", json=[cron_id])
        except RuntimeError as e:
            if "HTTP 400" in str(e) and self.api_prefix == "/open":
                logger.info(f"[{PLUGIN_ID}] DELETE /crons 数组 400，降级单个对象重试")
                return await self.request("DELETE", "/crons", json=cron_id)
            raise

    async def get_cron_log(self, cron_id: int) -> str:
        """获取任务日志内容，兼容不同版本/OpenAPI 返回结构"""
        data = await self.request("GET", f"/crons/{cron_id}/log")
        payload = data.get("data")
        if payload is None:
            return ""
        # 直接返回字符串的情况
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            return str(payload)
        # 嵌套结构 {"data": {"data": "..."}} 或 {"data": {"content": "..."}}
        if isinstance(payload.get("data"), str):
            return payload["data"]
        log_path = payload.get("logPath") or payload.get("log_path") or ""
        content = payload.get("content") or ""
        if log_path and not content:
            from urllib.parse import quote

            safe_path = quote(str(log_path), safe="/")
            for candidate in (f"/logs/{safe_path}", "/logs"):
                try:
                    resp = await self.request(
                        "GET", candidate, params={"path": str(log_path)} if candidate == "/logs" else {}
                    )
                    c = resp.get("data")
                    if isinstance(c, str):
                        return c
                    if isinstance(c, dict):
                        c = c.get("content") or c.get("data") or ""
                        if c:
                            return c
                except RuntimeError:
                    continue
        return content

    # ---- 脚本管理 ----
    async def get_scripts(self, search: str = "") -> List[dict]:
        params = {"searchValue": search, "pageSize": 100, "page": 1} if search else {}
        data = await self.request("GET", "/scripts", params=params)
        payload = data.get("data") or []
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("items") or []
        return [s for s in payload if isinstance(s, dict)]

    async def add_script(self, name: str, content: str, directory: str = "/") -> dict:
        """新增脚本（OpenAPI multipart 上传）。"""
        form = aiohttp.FormData()
        form.add_field("filename", name)
        form.add_field("content", content)
        form.add_field("path", directory)
        logger.info(f"[{PLUGIN_ID}] 上传脚本: filename={name}, path={directory}, content长度={len(content)}")
        return await self.request("POST", "/scripts/", data=form)

    async def update_script(self, name: str, content: str, directory: str = "/") -> dict:
        """更新脚本（OpenAPI multipart 上传）。"""
        form = aiohttp.FormData()
        form.add_field("filename", name)
        form.add_field("content", content)
        form.add_field("path", directory)
        return await self.request("PUT", "/scripts/", data=form)

    # ---- 定时任务创建 ----
    async def add_cron(self, name: str, command: str, schedule: str = "0 0 0 1 1 *") -> dict:
        """创建定时任务。OpenAPI 要求 schedule 必填（6位cron：秒分时日月周），默认每年1月1日（几乎不会自动运行，由插件触发）。"""
        body = {"name": name, "command": command, "schedule": schedule}
        return await self.request("POST", "/crons", json=body)

    # ---- 订阅管理 ----
    async def get_subscriptions(self) -> List[dict]:
        data = await self.request("GET", "/subscriptions")
        payload = data.get("data") or []
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("items") or []
        return [s for s in payload if isinstance(s, dict)]

    async def add_subscription(self, name: str, url: str, branch: str = "master",
                               schedule: str = "0 0 * * *",
                               whitelist: str = "", auto_add_cron: bool = True) -> dict:
        """新增订阅。type=public-repo, schedule_type=crontab, alias 必填。
        whitelist: 白名单（文件路径包含的字符串，竖线分隔），匹配的脚本会被同步到脚本管理；
        autoAddCron: 自动添加定时任务（否则青龙只检测不添加）。"""
        body = {
            "name": name,
            "type": "public-repo",
            "schedule_type": "crontab",
            "schedule": schedule,
            "url": url,
            "branch": branch,
            "alias": name,
            "autoAddCron": auto_add_cron,
        }
        if whitelist:
            body["whitelist"] = whitelist
        return await self.request("POST", "/subscriptions", json=body)

    async def update_subscription(self, sub_id, **fields) -> dict:
        """更新订阅。OpenAPI 用 PUT /subscriptions 单个对象（含 id），不接受 status 字段。"""
        body = {"id": sub_id}
        body.update(fields)
        try:
            return await self.request("PUT", "/subscriptions", json=body)
        except RuntimeError as e:
            if "HTTP 400" in str(e) and self.api_prefix == "/open":
                logger.info(f"[{PLUGIN_ID}] PUT /subscriptions 带 id 400，去掉 id 重试")
                body.pop("id", None)
                return await self.request("PUT", "/subscriptions", json=body)
            raise

    async def run_subscription(self, sub_id) -> dict:
        """触发订阅拉取。"""
        try:
            return await self.request("PUT", "/subscriptions/run", json=[sub_id])
        except RuntimeError as e:
            if "HTTP 400" in str(e) and self.api_prefix == "/open":
                logger.info(f"[{PLUGIN_ID}] /subscriptions/run 数组 400，降级单个对象重试")
                return await self.request("PUT", "/subscriptions/run", json={"id": sub_id})
            raise


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------
@register(PLUGIN_ID, PLUGIN_AUTHOR, PLUGIN_NAME, PLUGIN_VERSION, PLUGIN_REPO)
class QLPanelPlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = _get_plugin_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.panel_file = self.data_dir / "panel.json"    # 面板绑定（全局唯一）
        self.users_file = self.data_dir / "users.json"    # 签到账号数据（唯一持久存储）
        self.backup_file = self.data_dir / "accounts_backup.json"  # 迁移备份文件

        # 配置项
        self.daily_time = "09:00"          # 定时签到时间（HH:MM，空串禁用定时）
        self.push_after_run = True         # 运行后是否推送日志
        self.task_name = "3gbizhi签到"     # 3D壁纸签到任务名（固定）
        self.zibi_task_name = "zibi签到"   # 子比签到任务名（固定）
        if config:
            self.daily_time = (config.get("daily_time") or "09:00").strip()
            self.push_after_run = config.get("push_after_run", True)

        self._panel: Optional[dict] = self._load_panel()
        self._users: List[dict] = self._load_users()
        self._save_lock = asyncio.Lock()
        # 运行锁：同一时刻只允许一个用户的一次运行（防止临时运行数据互相覆盖）
        self._run_lock = asyncio.Lock()

        # 定时签到任务
        if self.daily_time:
            asyncio.create_task(self._daily_scheduler())

        logger.info(f"[{PLUGIN_ID}] v{PLUGIN_VERSION} 加载完成，数据目录: {self.data_dir}")

    # ---------------- 每日一言 ----------------
    def _with_quote(self, text: str) -> str:
        """在回复末尾追加一条随机每日一言"""
        text = (text or "").rstrip()
        if not text:
            return text
        quote = random.choice(DAILY_QUOTES)
        return f"{text}\n\n💬 {quote}"

    def _patch_plain_result(self, event: AstrMessageEvent):
        """把 event.plain_result 替换为自动追加每日一言的版本。
        只在这三个命令入口调用一次，下游所有 _cmd_* 的 plain_result 都会自动带一言。"""
        orig = event.plain_result

        def _pr(text):
            return orig(self._with_quote(text))

        event.plain_result = _pr

    # ---------------- 数据持久化 ----------------
    def _load_panel(self) -> Optional[dict]:
        try:
            if self.panel_file.exists():
                data = json.loads(self.panel_file.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else None
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 读取面板配置失败: {e}")
        return None

    def _load_users(self) -> List[dict]:
        try:
            if self.users_file.exists():
                data = json.loads(self.users_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    users = [u for u in data if isinstance(u, dict) and u.get("uid")]
                    self._migrate_users(users)
                    return users
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 读取用户数据失败: {e}")
        return []

    @staticmethod
    def _migrate_users(users: List[dict]):
        """兼容旧版单账号结构：cookie -> cookies[]，zibi_host/zibi_cookie -> zibi_sites[]"""
        for u in users:
            if "cookies" not in u or not isinstance(u.get("cookies"), list):
                old_cookie = u.get("cookie", "")
                u["cookies"] = [old_cookie] if old_cookie else []
            if "zibi_sites" not in u or not isinstance(u.get("zibi_sites"), list):
                host = (u.get("zibi_host") or "").strip()
                zcookie = (u.get("zibi_cookie") or "").strip()
                u["zibi_sites"] = [{"host": host, "cookie": zcookie}] if host and zcookie else []

    # 账号平铺辅助：返回 [(user, 全局序号, 账号信息)]，序号从 1 开始
    def _all_3d_accounts(self) -> List[Tuple[dict, int, str]]:
        out = []
        for u in self._users:
            for ck in u.get("cookies") or []:
                if ck:
                    out.append((u, len(out) + 1, ck))
        return out

    def _all_zibi_accounts(self) -> List[Tuple[dict, int, dict]]:
        out = []
        for u in self._users:
            for site in u.get("zibi_sites") or []:
                if (site.get("host") or "").strip() and (site.get("cookie") or "").strip():
                    out.append((u, len(out) + 1, site))
        return out

    async def _save_panel(self):
        async with self._save_lock:
            tmp = self.panel_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._panel, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.panel_file)

    async def _save_users(self):
        async with self._save_lock:
            tmp = self.users_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._users, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.users_file)

    def _new_client(self) -> Optional[QinglongClient]:
        cfg = self._get_panel_config()
        if not cfg:
            return None
        return QinglongClient(
            base_url=cfg["url"],
            api_prefix="/open",
            client_id=cfg.get("client_id", ""),
            client_secret=cfg.get("client_secret", ""),
            token=cfg.get("token", ""),
        )

    def _get_panel_config(self) -> Optional[dict]:
        """面板配置来源：优先 WebUI 配置（ql_url + ql_client_id + ql_client_secret），
        其次 /ql bind 命令绑定的 panel.json。仅支持 OpenAPI 认证。"""
        if self.config:
            url = (self.config.get("ql_url") or "").strip()
            if url:
                client_id = (self.config.get("ql_client_id") or "").strip()
                return {
                    "url": self._normalize_base_url(url),
                    "token": "",
                    "api_prefix": "/open",
                    "client_id": client_id,
                    "client_secret": (self.config.get("ql_client_secret") or "").strip(),
                    "auth_mode": "OpenAPI",
                    "source": "WebUI配置",
                }
        if self._panel:
            cfg = dict(self._panel)
            cfg.setdefault("source", "命令绑定")
            cfg.setdefault("api_prefix", "/open")
            cfg.setdefault("auth_mode", "OpenAPI")
            return cfg
        return None

    def _find_user(self, uid: str) -> Optional[dict]:
        for u in self._users:
            if u["uid"] == uid:
                return u
        return None

    # ---------------- 工具方法 ----------------
    def _get_user_id(self, event: AstrMessageEvent) -> str:
        try:
            message_obj = getattr(event, "message_obj", None)
            sender = getattr(message_obj, "sender", None) if message_obj else None
            uid = getattr(sender, "user_id", None)
            if uid:
                return str(uid)
        except Exception:
            pass
        name = event.get_sender_name() if hasattr(event, "get_sender_name") else "unknown"
        platform = event.get_platform_name() if hasattr(event, "get_platform_name") else "unknown"
        return f"{platform}_{name}"

    def _get_origin(self, event: AstrMessageEvent) -> str:
        """获取会话来源（用于定时推送）"""
        return getattr(event, "unified_msg_origin", "") or ""

    def _parse_args(self, event: AstrMessageEvent) -> Tuple[List[str], str]:
        """解析命令参数。兼容 message_str 含/不含命令前缀两种情况（/ql 与 /3d）。"""
        text = (event.message_str or "").strip()
        try:
            parts = shlex.split(text)
        except Exception:
            parts = text.split()
        if parts and parts[0].lower() in ("/ql", "ql", "/3d", "3d", "/zibi", "zibi"):
            parts = parts[1:]
        return parts, text

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        url = url.strip().rstrip("/")
        if url.endswith("/api"):
            url = url[:-4]
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url
        return url.rstrip("/")

    @staticmethod
    def _normalize_site_url(url: str) -> str:
        """规范化网站地址：域名部分小写、去末尾斜杠。
        例: HTTPS://WWW.ABC.COM/ -> https://www.abc.com（防同站重复绑定）"""
        url = (url or "").strip().rstrip("/")
        m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/]+)(.*)$", url)
        if m:
            scheme, host, rest = m.group(1), m.group(2), m.group(3)
            scheme = scheme.lower()
            host = host.lower()
            url = f"{scheme}{host}{rest}"
        return url.rstrip("/")

    async def _extract_file_component(self, event: AstrMessageEvent):
        message_obj = getattr(event, "message_obj", None)
        message = getattr(message_obj, "message", None) if message_obj else None
        if not message:
            return None
        for component in message:
            name = component.__class__.__name__ if hasattr(component, "__class__") else ""
            if "file" in name.lower():
                return component
        return None

    async def _download_uploaded_file(self, file_component) -> Tuple[str, str]:
        """下载/读取用户上传的文件，返回 (文件名, 文本内容)。"""
        attrs = {}
        for attr in dir(file_component):
            if attr.startswith("_"):
                continue
            try:
                value = getattr(file_component, attr)
                if isinstance(value, (str, int, float, bool, type(None))):
                    attrs[attr] = value
            except Exception:
                pass

        file_url = (
            attrs.get("url") or attrs.get("file_url") or attrs.get("path") or attrs.get("file_path") or ""
        )
        file_name = attrs.get("name") or attrs.get("filename") or "upload.txt"

        local_path = file_url
        if local_path.startswith("file://"):
            local_path = local_path[7:]
        if local_path.startswith("/") and os.path.exists(local_path):
            raw = Path(local_path).read_bytes()
            return str(file_name), self._decode_text(raw)

        if file_url.startswith("http://") or file_url.startswith("https://"):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.get(file_url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"文件下载失败: HTTP {resp.status}")
                    raw = await resp.read()
                    return str(file_name), self._decode_text(raw)

        raise RuntimeError("无法定位上传的文件资源")

    @staticmethod
    def _decode_text(raw: bytes) -> str:
        for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                if text and sum(1 for ch in text if ord(ch) < 9 or 14 <= ord(ch) < 32) / max(len(text), 1) < 0.05:
                    return text
            except (UnicodeDecodeError, ValueError):
                continue
        raise RuntimeError("文件不是可读文本（可能为二进制），请上传 txt 文本文件")

    @staticmethod
    def _clean_cookie(raw: str) -> str:
        """清洗用户上传的 cookie：去 BOM/首尾空白，压缩多余空白"""
        raw = (raw or "").strip().strip("\ufeff")
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw

    # ---------------- 账号数据（本地文件唯一存储） ----------------
    async def _write_env(self, client: QinglongClient, name: str, value: str, remarks: str = "") -> str:
        """写入/更新环境变量（仅管理员 /ql env 手动管理用），返回结果文本"""
        matches = await client.find_envs_by_name(name)
        if matches:
            first = matches[0]
            await client.update_env(first, name, value, remarks or first.get("remarks", ""))
            return f"已更新环境变量 {name}"
        await client.add_env(name, value, remarks)
        return f"已创建环境变量 {name}"

    def _find_user(self, uid: str) -> Optional[dict]:
        for u in self._users:
            if u["uid"] == uid:
                return u
        return None

    # 账号平铺辅助：返回 [(user, 全局序号, 账号信息)]，序号从 1 开始
    def _all_3d_accounts(self) -> List[Tuple[dict, int, str]]:
        out = []
        for u in self._users:
            for ck in u.get("cookies") or []:
                if ck:
                    out.append((u, len(out) + 1, ck))
        return out

    def _all_zibi_accounts(self) -> List[Tuple[dict, int, dict]]:
        out = []
        for u in self._users:
            for site in u.get("zibi_sites") or []:
                if (site.get("host") or "").strip() and (site.get("cookie") or "").strip():
                    out.append((u, len(out) + 1, site))
        return out

    # ---------------- 运行数据注入（临时，用完即删） ----------------
    async def _inject_run_env(self, client: QinglongClient, env_name: str, payload: str) -> str:
        """把本次运行所需的账号数据临时写入青龙环境变量。
        先删除同名旧值再写入，确保脚本读到的只有本次用户的数据。"""
        try:
            old = await client.find_envs_by_name(env_name)
            ids = [QinglongClient.env_id(e) for e in old if QinglongClient.env_id(e) is not None]
            if ids:
                await client.delete_envs(ids)
        except RuntimeError as e:
            logger.warning(f"[{PLUGIN_ID}] 清理旧运行数据失败: {e}")
        await client.add_env(env_name, payload, "临时运行数据，运行后自动删除")
        return env_name

    async def _remove_run_env(self, client: QinglongClient, env_name: str):
        """运行结束后删除临时运行数据环境变量（青龙不持久存储账号）。"""
        try:
            old = await client.find_envs_by_name(env_name)
            ids = [QinglongClient.env_id(e) for e in old if QinglongClient.env_id(e) is not None]
            if ids:
                await client.delete_envs(ids)
                logger.info(f"[{PLUGIN_ID}] 已清理临时运行数据 {env_name}")
        except RuntimeError as e:
            logger.warning(f"[{PLUGIN_ID}] 清理运行数据失败（不影响结果）: {e}")

    @staticmethod
    def _estimate_wait(n: int) -> Tuple[int, int]:
        """按账号数量估算等待时间（秒）：每个账号随机 3~10 秒 + 签到请求耗时"""
        if n <= 0:
            return 5, 10
        return n * 5, n * 12

    # ---------------- 备份与恢复（迁移） ----------------
    async def _cmd_backup(self, event: AstrMessageEvent):
        """导出账号数据到 accounts_backup.json，便于拷贝到新服务器迁移"""
        if not self._users:
            return event.plain_result("⚠️ 当前没有账号数据，无需备份。")
        try:
            tmp = self.backup_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._users, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.backup_file)
        except Exception as e:
            return event.plain_result(f"❌ 备份失败: {e}")
        n3d = len(self._all_3d_accounts())
        nz = len(self._all_zibi_accounts())
        return event.plain_result(
            f"✅ 已导出账号备份文件:\n{self.backup_file}\n\n"
            f"包含 {len(self._users)} 位用户（3D账号 {n3d} 个、子比账号 {nz} 个）。\n"
            f"迁移步骤:\n"
            f"1. 拷贝 accounts_backup.json 到新服务器插件数据目录（覆盖同名文件）\n"
            f"2. 在新服务器执行 /ql restore 恢复账号"
        )

    async def _cmd_restore(self, event: AstrMessageEvent):
        """从 accounts_backup.json 恢复账号数据（迁移后使用）"""
        if not self.backup_file.exists():
            return event.plain_result(
                f"⚠️ 未找到备份文件:\n{self.backup_file}\n\n"
                f"请先把旧服务器的 accounts_backup.json 拷贝到本服务器插件数据目录，再执行 /ql restore。"
            )
        try:
            data = json.loads(self.backup_file.read_text(encoding="utf-8"))
        except Exception as e:
            return event.plain_result(f"❌ 备份文件读取失败: {e}")
        if not isinstance(data, list):
            return event.plain_result("❌ 备份文件格式错误（应为 JSON 数组）。")
        users = [u for u in data if isinstance(u, dict) and u.get("uid")]
        if not users:
            return event.plain_result("⚠️ 备份文件中没有有效账号数据。")
        self._migrate_users(users)
        self._users = users
        await self._save_users()
        n3d = len(self._all_3d_accounts())
        nz = len(self._all_zibi_accounts())
        return event.plain_result(
            f"✅ 账号数据已恢复！\n"
            f"共 {len(users)} 位用户（3D账号 {n3d} 个、子比账号 {nz} 个）。\n\n"
            f"接下来:\n"
            f"1. 确认 /ql status 面板连接正常\n"
            f"2. 执行 /ql init 确保任务与订阅就绪\n"
            f"3. 用户可正常 /3d run 签到"
        )

    # ---------------- 日志解析与推送 ----------------
    @staticmethod
    def _parse_log_by_account(log: str) -> Dict[int, str]:
        """从脚本日志中提取各账号结果行：{账号序号: 结果文本}。
        支持两种格式：
        - 多账号：【账号N】状态:xxx 连续N天 ...
        - 单账号：【签到结果】状态:xxx 连续N天 ...（映射为账号1）
        """
        results: Dict[int, str] = {}
        for ln in (log or "").splitlines():
            m = re.search(r"【账号(\d+)】\s*(.+)", ln)
            if m:
                results[int(m.group(1))] = m.group(2).strip()
                continue
            m = re.search(r"【签到结果】\s*(.+)", ln)
            if m and 1 not in results:
                results[1] = m.group(1).strip()
        return results

    async def _push(self, origin: str, text: str):
        if not origin or not self.context:
            return
        try:
            from astrbot.api.event import MessageChain

            chain = MessageChain().message(text)
            await self.context.send_message(origin, chain)
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 推送失败 origin={origin}: {e}")

    async def _wait_cron_log(self, client: QinglongClient, cron_id: int, timeout_s: int = 600, poll_interval: float = 15) -> str:
        """触发后轮询日志直到出现结束标记或超时。首次等待 5 秒，之后每 poll_interval 秒一次。"""
        last = ""
        first_wait = min(5, poll_interval)
        await asyncio.sleep(first_wait)
        steps = max(1, int(timeout_s // poll_interval))
        for i in range(steps):
            try:
                content = await client.get_cron_log(cron_id)
            except RuntimeError as e:
                logger.warning(f"[{PLUGIN_ID}] 拉取日志失败(第{i+1}次): {e}")
                await asyncio.sleep(poll_interval)
                continue
            if content and content != last:
                last = content
                logger.info(f"[{PLUGIN_ID}] 日志轮询(第{i+1}次): 长度={len(content)}，含结束标记={'任务结束' in content}")
                if "任务结束" in content:
                    break
            elif not content:
                logger.info(f"[{PLUGIN_ID}] 日志轮询(第{i+1}次): 暂无日志输出")
            await asyncio.sleep(poll_interval)
        if not last:
            logger.warning(f"[{PLUGIN_ID}] 轮询 {steps} 次仍未获取到日志，请检查青龙任务是否正常执行")
        return last

    # ---------------- 核心：运行签到并推送（按用户隔离） ----------------
    async def _find_task_by_script(self, client: QinglongClient,
                                   script_name: str, task_name: str) -> Optional[dict]:
        """按脚本文件名（命令含 xxx.py，兼容订阅自动添加的任务）或任务名查找定时任务。
        优先匹配命令含脚本文件名（订阅 autoAddCron 添加的任务命令形如 task repo/3gbizhi.py）。"""
        try:
            crons = await client.get_crons()
        except RuntimeError as e:
            raise RuntimeError(f"获取定时任务失败: {e}")
        for c in crons:
            if script_name in (c.get("command") or ""):
                return c
        for c in crons:
            if task_name.lower() in (c.get("name") or "").lower():
                return c
        return None

    async def _run_sign_and_push(self, event: Optional[AstrMessageEvent] = None,
                                 uid: Optional[str] = None, origin: str = "") -> str:
        """为指定用户执行一次 3D壁纸签到：
        只注入该用户自己的 Cookie -> 触发任务 -> 轮询日志 -> 推送给该用户。
        event 非空 = 用户手动触发（回复本人结果）；否则为定时触发。"""
        if not self._get_panel_config():
            return "⚠️ 管理员还未绑定青龙面板，请在 WebUI 插件配置中填写面板地址/Client ID/Client Secret，或执行 /ql bind。"
        client = self._new_client()
        if not client:
            return "⚠️ 面板配置异常，请检查 WebUI 配置或重新 /ql bind。"

        # 确定目标用户
        target_uid = uid
        if event is not None:
            target_uid = self._get_user_id(event)
        user = self._find_user(target_uid) if target_uid else None
        cookies = (user or {}).get("cookies") or []
        cookies = [ck for ck in cookies if ck]
        if not cookies:
            if event is not None:
                return "⚠️ 你还没有注册 Cookie。\n用法: /3d set <Cookie>（可多次 set 绑定多个账号）"
            return "（跳过：该用户没有 3D 账号）"
        target_origin = origin or (user.get("origin") if user else "") or ""

        async with self._run_lock:
            try:
                # 1. 注入该用户自己的账号数据（临时，用完即删）
                payload = json.dumps(cookies, ensure_ascii=False)
                await self._inject_run_env(client, RUN_ENV_3D, payload)
                logger.info(f"[{PLUGIN_ID}] 已注入运行数据: {RUN_ENV_3D}（{len(cookies)} 个账号）")

                # 2. 查找签到任务（兼容手动创建与订阅自动添加）
                cron = await self._find_task_by_script(client, "3gbizhi.py", self.task_name)
                if not cron:
                    return (
                        f"⚠️ 未在面板找到签到任务「3gbizhi.py」。\n"
                        f"请管理员先 /ql init 创建任务（或青龙手动创建：命令 task fiezhu_astrbot_plugin_qlpanel-sql_master/3gbizhi.py）。"
                    )
                logger.info(f"[{PLUGIN_ID}] 找到签到任务: id={cron.get('id')} name={cron.get('name')}")

                # 3. 触发运行
                await client.run_cron(cron["id"])
                logger.info(f"[{PLUGIN_ID}] 已触发任务运行，开始轮询日志...")

                # 3.5 手动触发时按账号数量告知预计等待时间
                est_min, est_max = self._estimate_wait(len(cookies))
                if event is not None and target_origin:
                    await self._push(
                        target_origin,
                        f"⏳ 签到任务已触发，正在运行你的 {len(cookies)} 个账号"
                        f"（每个账号间隔 3~10 秒，预计约 {est_min}~{est_max} 秒）……",
                    )

                # 4. 轮询日志
                log = await self._wait_cron_log(client, cron["id"])
                logger.info(f"[{PLUGIN_ID}] 日志轮询结束，长度={len(log)}，含结束标记={'任务结束' in log}")

                # 5. 解析结果：本次日志只包含该用户的账号
                results = self._parse_log_by_account(log)
                lines = []
                for i, ck in enumerate(cookies, 1):
                    line = results.get(i) or "未获取到结果，请检查 Cookie 是否有效"
                    lines.append(f"【账号{i}】{line}")
                result_text = "\n".join(lines) if lines else "（日志中未解析到账号结果）"

                # 6. 推送（用户开启推送且全局开启时）
                pushed = 0
                if self.push_after_run and target_origin and user.get("push_enabled", True):
                    await self._push(target_origin, "📱 3D壁纸签到结果\n" + result_text)
                    pushed += 1

                # 触发者回复
                if event is not None:
                    reply = f"✅ 签到完成\n{result_text}"
                    if pushed:
                        reply += "\n📤 结果已推送给你。"
                    return reply
                return f"✅ 3D壁纸签到完成（{len(cookies)} 个账号）"
            except RuntimeError as e:
                return f"❌ 签到执行失败: {e}"
            finally:
                # 7. 无论成功失败都清理临时运行数据（青龙不持久存储账号）
                try:
                    await self._remove_run_env(client, RUN_ENV_3D)
                except Exception:
                    pass

    # ---------------- 核心：子比签到运行并推送（按用户隔离） ----------------
    async def _run_zibi_and_push(self, event: Optional[AstrMessageEvent] = None,
                                 uid: Optional[str] = None, origin: str = "") -> str:
        """为指定用户执行一次子比签到：只注入该用户自己的网站配置 -> 触发 -> 轮询 -> 推送。"""
        if not self._get_panel_config():
            return "⚠️ 管理员还未绑定青龙面板，请在 WebUI 插件配置中填写面板地址/Client ID/Client Secret，或执行 /ql bind。"
        client = self._new_client()
        if not client:
            return "⚠️ 面板配置异常，请检查 WebUI 配置或重新 /ql bind。"

        target_uid = uid
        if event is not None:
            target_uid = self._get_user_id(event)
        user = self._find_user(target_uid) if target_uid else None
        sites = []
        for s in (user or {}).get("zibi_sites") or []:
            if (s.get("host") or "").strip() and (s.get("cookie") or "").strip():
                sites.append({"host": s["host"].strip(), "cookie": s["cookie"]})
        if not sites:
            if event is not None:
                return "⚠️ 你还没有绑定子比网站。\n用法: /zibi set <网站地址> <Cookie>（可多次 set 绑定多个网站）"
            return "（跳过：该用户没有子比网站）"
        target_origin = origin or (user.get("origin") if user else "") or ""

        async with self._run_lock:
            try:
                # 1. 注入该用户自己的网站配置（临时，用完即删）
                payload = json.dumps(sites, ensure_ascii=False)
                await self._inject_run_env(client, RUN_ENV_ZIBI, payload)
                logger.info(f"[{PLUGIN_ID}] 已注入运行数据: {RUN_ENV_ZIBI}（{len(sites)} 个网站）")

                # 2. 查找子比签到任务
                cron = await self._find_task_by_script(client, "zibi_check_in.py", self.zibi_task_name)
                if not cron:
                    return (
                        f"⚠️ 未在面板找到子比签到任务「zibi_check_in.py」。\n"
                        f"请管理员先 /ql init 创建任务（或青龙手动创建：命令 task fiezhu_astrbot_plugin_qlpanel-sql_master/zibi_check_in.py）。"
                    )
                logger.info(f"[{PLUGIN_ID}] 找到子比签到任务: id={cron.get('id')} name={cron.get('name')}")

                # 3. 触发运行
                await client.run_cron(cron["id"])
                logger.info(f"[{PLUGIN_ID}] 已触发子比任务运行，开始轮询日志...")

                # 3.5 按账号数量告知预计等待时间
                est_min, est_max = self._estimate_wait(len(sites))
                if event is not None and target_origin:
                    await self._push(
                        target_origin,
                        f"⏳ 子比签到任务已触发，正在运行你的 {len(sites)} 个网站"
                        f"（每个网站间隔 3~10 秒，预计约 {est_min}~{est_max} 秒）……",
                    )

                # 4. 轮询日志
                log = await self._wait_cron_log(client, cron["id"])
                logger.info(f"[{PLUGIN_ID}] 子比日志轮询结束，长度={len(log)}，含结束标记={'任务结束' in log}")

                # 5. 解析结果（日志只含该用户的网站）
                results = self._parse_log_by_account(log)
                lines = []
                for i, site in enumerate(sites, 1):
                    raw = results.get(i) or ""
                    # 剥离日志行中的 host 前缀（脚本输出形如"【账号N】host -> 今日已签到"），
                    # 避免推送里网站地址出现两次
                    for prefix in (f"{site['host']} ->", f"{site['host']}：", f"{site['host']}:",
                                   f"{site['host']} "):
                        if raw.startswith(prefix):
                            raw = raw[len(prefix):].lstrip("：:-> ")
                            break
                    raw = raw.strip()
                    line = raw or "未获取到结果，请检查 Cookie 是否有效"
                    lines.append(f"【账号{i}】{site['host']}：{line}")
                result_text = "\n".join(lines) if lines else "（日志中未解析到网站结果）"

                # 6. 推送
                pushed = 0
                if self.push_after_run and target_origin and user.get("push_enabled", True):
                    await self._push(target_origin, "🌐 子比网站签到结果\n" + result_text)
                    pushed += 1

                if event is not None:
                    reply = f"✅ 子比签到完成\n{result_text}"
                    if pushed:
                        reply += "\n📤 结果已推送给你。"
                    return reply
                return f"✅ 子比签到完成（{len(sites)} 个网站）"
            except RuntimeError as e:
                return f"❌ 子比签到执行失败: {e}"
            finally:
                try:
                    await self._remove_run_env(client, RUN_ENV_ZIBI)
                except Exception:
                    pass

    # ---------------- 定时调度 ----------------
    async def _daily_scheduler(self):
        """每天定时触发签到。

        3D 与子比的时间相互独立：
          - 全局默认时间（daily_time）同时作用于两个功能
          - 用户 /3d time HH:MM 设置后，3D 只在个人时间触发（不再跟随全局）
          - 用户 /zibi time HH:MM 设置后，子比只在个人时间触发（不再跟随全局）
        触发时间点 = 全局时间 ∪ 所有用户 daily_time_3d ∪ 所有用户 daily_time_zibi；
        到点后按「该时间点是否命中某功能的全局/个人时间」逐个用户运行对应功能。

        防重复：模块级 _TRIGGER_LOG 90 秒窗口，防止 AstrBot 多实例残留重复触发刷屏。"""
        while True:
            try:
                global_t = (self.daily_time or "").strip()
                if global_t and not re.match(r"^\d{1,2}:\d{2}$", global_t):
                    global_t = ""
                # 收集触发时间点 -> 该时间点启用的功能集合
                triggers: Dict[str, set] = {}
                if global_t:
                    triggers.setdefault(global_t, set()).update(("3d", "zibi"))
                for u in self._users:
                    # 兼容旧字段 daily_time（旧版 3D/子比共用）
                    legacy = u.get("daily_time") or ""
                    for key, feat in (("daily_time_3d", "3d"), ("daily_time_zibi", "zibi")):
                        t = u.get(key) or legacy or ""
                        t = t.strip()
                        if t and re.match(r"^\d{1,2}:\d{2}$", t):
                            triggers.setdefault(t, set()).add(feat)
                if not triggers:
                    await asyncio.sleep(60)
                    continue

                # 计算下一个触发时间
                now = datetime.now()
                next_trigger = None
                for t in triggers:
                    hh, mm = int(t.split(":")[0]), int(t.split(":")[1])
                    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if target <= now:
                        target += timedelta(days=1)
                    if next_trigger is None or target < next_trigger:
                        next_trigger = target

                wait_s = (next_trigger - now).total_seconds()
                logger.info(
                    f"[{PLUGIN_ID}] 下一次定时签到: {next_trigger.strftime('%Y-%m-%d %H:%M')} "
                    f"（{len(triggers)} 个时间点: {', '.join(sorted(triggers))}）"
                )
                await asyncio.sleep(max(1, wait_s))

                trigger_key = next_trigger.strftime("%Y-%m-%d %H:%M")
                trigger_t = next_trigger.strftime("%H:%M")
                now_ts = time.time()
                if _TRIGGER_LOG.get(trigger_key, 0) and now_ts - _TRIGGER_LOG.get(trigger_key, 0) < 90:
                    # 同一时间点 90 秒内已有实例触发过：跳过，避免重复运行/重复推送
                    logger.info(
                        f"[{PLUGIN_ID}] 定时触发跳过（{trigger_key} 已触发过，防止重复）"
                    )
                    await asyncio.sleep(91)
                    continue
                _TRIGGER_LOG[trigger_key] = now_ts

                feats = triggers.get(trigger_t, set())
                logger.info(f"[{PLUGIN_ID}] 定时签到触发（{trigger_t}，功能: {','.join(sorted(feats)) or '无'}）")
                # 定时到点：逐用户判断该时间点是否命中其 3D/子比 的全局或个人时间
                for u in self._users:
                    u_origin = u.get("origin") or ""
                    legacy = u.get("daily_time") or ""
                    u3 = (u.get("daily_time_3d") or legacy or "").strip()
                    uz = (u.get("daily_time_zibi") or legacy or "").strip()
                    try:
                        has_3d = bool(u.get("cookies") and any(u["cookies"]))
                        run_3d = (
                            has_3d
                            and "3d" in feats
                            and (u3 == trigger_t or (not u3 and global_t == trigger_t))
                        )
                        if run_3d:
                            await self._run_sign_and_push(uid=u["uid"], origin=u_origin)
                    except Exception as e:
                        logger.error(f"[{PLUGIN_ID}] 定时签到失败(用户 {u['uid']}): {e}")
                    try:
                        has_zibi = bool(u.get("zibi_sites") and any(
                            s.get("host") and s.get("cookie") for s in u["zibi_sites"]))
                        run_zibi = (
                            has_zibi
                            and "zibi" in feats
                            and (uz == trigger_t or (not uz and global_t == trigger_t))
                        )
                        if run_zibi:
                            await self._run_zibi_and_push(uid=u["uid"], origin=u_origin)
                    except Exception as e:
                        logger.error(f"[{PLUGIN_ID}] 定时子比签到失败(用户 {u['uid']}): {e}")
                # 触发后等 61 秒，避免同一分钟内重复触发
                await asyncio.sleep(61)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"[{PLUGIN_ID}] 定时调度异常: {e}")
                await asyncio.sleep(60)

    # ---------------- 帮助文本（分区） ----------------
    @staticmethod
    def _help_menu() -> str:
        return (
            "📖 QLpanel-pro 功能菜单\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🎮 签到功能\n"
            "  /3d     3D壁纸签到\n"
            "  /zibi   子比网站签到\n"
            "  → 查看详细用法: /ql help 3d 或 /ql help zibi\n"
            "  → 也可直接发 /3d help、/zibi help\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 管理员后台\n"
            "  /ql admin   管理员功能（仅管理员可触发）\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💬 每条回复末尾都会附上一句每日一言～"
        )

    @staticmethod
    def _help_3d() -> str:
        return (
            "📱 3D壁纸签到 · 使用说明\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎这是什么\n"
            "  3D壁纸(3gbizhi) 网站的每日签到。\n"
            "  每个 Cookie 是一个签到账号，机器人通过青龙面板脚本帮你自动签到。\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎绑定账号\n"
            "  /3d set <Cookie>          追加一个签到账号\n"
            "                             （可多次 set，绑定多个账号）\n"
            "  /3d set + 发送txt文件      把 Cookie 写在文件里自动读取\n"
            "  /3d me                    查看我绑定的所有账号\n"
            "  /3d del <序号>            删除我的指定账号\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎运行签到\n"
            "  /3d run                   立即签到（只运行你自己的账号）\n"
            "  运行后机器人会先提示预计等待时间，完成后推送结果\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎定时与推送\n"
            "  /3d time <HH:MM>          设置我的个人签到时间（如 08:30）\n"
            "  /3d time reset            恢复跟随管理员全局时间\n"
            "  /3d push on|off           开启/关闭结果推送\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎其他\n"
            "  /3d list                  查看全部账号（管理员）\n"
            "  /3d help                  本帮助\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔒 安全说明\n"
            "  账号只存插件数据目录；运行时才临时注入青龙、用完即删；\n"
            "  每个账号间隔 3~10 秒防风控；推送只含你自己的结果。"
        )

    @staticmethod
    def _help_zibi() -> str:
        return (
            "🌐 子比网站签到 · 使用说明\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎这是什么\n"
            "  使用子比主题(Zibll) 的网站通用每日签到。\n"
            "  每个网站 + Cookie 是一个签到账号，可绑定多个。\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎绑定账号\n"
            "  /zibi set <网站地址> <Cookie>   追加一个网站账号\n"
            "     例: /zibi set https://www.725361764.cn \"wordpress_logged_in_xxx=abc; ...\"\n"
            "  /zibi set <网站地址> + 发送文件  Cookie 写在文件里自动读取\n"
            "  /zibi me                    查看我绑定的所有网站账号\n"
            "  /zibi del <序号>            删除我的指定网站账号\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎运行签到\n"
            "  /zibi run                  立即运行子比签到（只运行你的网站）\n"
            "  运行后机器人会先提示预计等待时间，完成后推送结果\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎定时与推送\n"
            "  /zibi time <HH:MM>          设置我的个人签到时间（与3D共用）\n"
            "  /zibi time reset            恢复跟随管理员全局时间\n"
            "  /zibi push on|off           开启/关闭结果推送（与3D共用）\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎其他\n"
            "  /zibi list                  查看全部绑定（管理员）\n"
            "  /zibi help                  本帮助\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔒 自动提取 wordpress_logged_in 登录态；账号只存本地，\n"
            "   运行只注入你自己的网站，每个网站间隔 3~10 秒，推送只含你的结果。"
        )

    @staticmethod
    def _help_admin() -> str:
        return (
            "🔐 QLpanel-pro · 管理员后台\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎部署初始化（新环境必做）\n"
            "  /ql init      一键检测青龙连接 → 创建签到任务 →\n"
            "                添加订阅拉取脚本（可重复执行，自动对齐/清理重复任务）\n"
            "  /ql status    查看面板连接状态\n"
            "  /ql unbind    清除面板绑定（或到 WebUI 清空配置）\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎账号迁移\n"
            "  /ql backup    导出全部账号到 accounts_backup.json\n"
            "  /ql restore   从备份恢复账号（迁移服务器后执行）\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎面板运维\n"
            "  /ql env list|add|del   管理青龙环境变量\n"
            "  /ql cron list|run|log  管理青龙定时任务\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▎配置说明\n"
            "  面板配置推荐在 AstrBot WebUI → 插件配置 填写：\n"
            "    ql_url（面板地址，含 http:// 和端口）\n"
            "    ql_client_id / ql_client_secret（青龙 OpenAPI，仅支持 Client ID 认证）\n"
            "  聊天框备用: /ql bind <地址> <Client ID> <Client Secret>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔒 以上功能仅管理员可触发，普通用户无法查看/使用。"
        )

    # ---------------- /ql 命令（管理员） ----------------
    @filter.command("ql")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ql(self, event: AstrMessageEvent):
        self._patch_plain_result(event)
        try:
            parts, _ = self._parse_args(event)
            if not parts:
                yield event.plain_result(self._help_menu())
                return
            sub = parts[0].lower()
            if sub in ("help", "帮助", "菜单", "menu"):
                if len(parts) == 1:
                    yield event.plain_result(self._help_menu())
                elif parts[1].lower() in ("3d", "3d壁纸", "壁纸"):
                    yield event.plain_result(self._help_3d())
                elif parts[1].lower() in ("zibi", "子比"):
                    yield event.plain_result(self._help_zibi())
                elif parts[1].lower() in ("admin", "管理员", "后台"):
                    yield event.plain_result(
                        "🔐 管理员后台帮助仅管理员可见。\n请发送 /ql admin 查看（非管理员无法触发）。"
                    )
                else:
                    yield event.plain_result(f"未知分区: {parts[1]}\n\n" + self._help_menu())
            elif sub == "admin":
                yield event.plain_result(self._help_admin())
            elif sub == "init":
                yield await self._cmd_init(event)
            elif sub == "bind":
                yield await self._cmd_bind(event, parts[1:])
            elif sub == "unbind":
                yield await self._cmd_unbind(event)
            elif sub == "status":
                yield await self._cmd_status(event)
            elif sub == "env":
                yield await self._cmd_env(event, parts[1:])
            elif sub == "cron":
                yield await self._cmd_cron(event, parts[1:])
            elif sub in ("backup", "export"):
                yield await self._cmd_backup(event)
            elif sub in ("restore", "import"):
                yield await self._cmd_restore(event)
            else:
                yield event.plain_result(f"未知子命令: {sub}\n\n" + self._help_menu())
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] /ql 命令异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 处理出错: {e}")

    async def _check_script_version(self, client: QinglongClient) -> List[str]:
        """检查签到脚本是否就绪。

        青龙订阅托管的脚本位于 repo 目录（任务命令 task <仓库目录>/xxx.py），
        不会出现在「脚本管理」中——因此以「任务命令指向订阅仓库」为就绪判据；
        若脚本管理中存在同名脚本（手动上传/订阅同步过），则进一步比对内容版本
        （新版读取 BIZHI_RUN_DATA / ZIBI_RUN_DATA，旧版读取 BIZHI_COOKIE / ZIBI_CONFIG）。"""
        notes: List[str] = []
        checks = [
            ("3gbizhi.py", RUN_ENV_3D),
            ("zibi_check_in.py", RUN_ENV_ZIBI),
        ]
        try:
            crons = await client.get_crons()
        except RuntimeError as e:
            notes.append(f"⚠️ 无法读取青龙任务列表（{e}）。")
            return notes

        scripts = None  # 懒加载：脚本管理接口可能无权限
        for fname, marker in checks:
            # 1) 任务就绪判据：存在命令指向订阅仓库的任务
            sub_task = next(
                (c for c in crons if (c.get("command") or "").strip().endswith(f"/{fname}")),
                None,
            )
            if sub_task is None:
                notes.append(
                    f"❌ 未找到 {fname} 签到任务（命令 task …/{fname}）。\n"
                    f"   请管理员 /ql init 创建任务（或青龙手动创建：命令 task fiezhu_astrbot_plugin_qlpanel-sql_master/{fname}）。"
                )
                continue
            # 2) 尝试从脚本管理读取内容做版本比对（可能无权限/不存在，不强求）
            if scripts is None:
                try:
                    scripts = await client.get_scripts()
                except RuntimeError:
                    scripts = None
            target = None
            if scripts is not None:
                target = next(
                    (s for s in scripts if (s.get("filename") or s.get("name") or "").split("/")[-1] == fname),
                    None,
                )
            if target is None:
                notes.append(
                    f"✅ 任务 {sub_task.get('name')} 就绪（命令指向订阅仓库，脚本由订阅托管运行，无需出现在脚本管理）"
                )
                continue
            content = target.get("content") or ""
            if marker in content:
                notes.append(f"✅ 青龙脚本 {fname} 已更新（读取 {marker}）")
            else:
                notes.append(
                    f"❌ 青龙脚本管理中的 {fname} 仍是旧版（未读取 {marker}）！\n"
                    f"   请把插件包 scripts/{fname} 上传替换 Gitee 仓库同名文件，\n"
                    f"   再到青龙订阅管理点「运行」重新拉取。"
                )
        return notes

    async def _cmd_init(self, event: AstrMessageEvent):
        """管理员初始化：检测连接 → 创建定时任务 → 添加订阅 → 拉取脚本"""
        REPO_URL = "https://gitee.com/fiezhu/astrbot_plugin_qlpanel-sql.git"
        SUB_NAME = "astrbot_plugin_qlpanel-sql"
        SUB_SCHEDULE = "0 0 * * *"

        cfg = self._get_panel_config()
        if not cfg:
            return event.plain_result(
                "⚠️ 尚未配置青龙面板。\n"
                "请在 AstrBot WebUI → 插件配置 中填写：\n"
                "  • ql_url（面板地址）\n"
                "  • ql_client_id（青龙 OpenAPI Client ID）\n"
                "  • ql_client_secret（青龙 OpenAPI Client Secret）\n"
                "保存后再执行 /ql init。"
            )

        client = self._new_client()
        if not client:
            return event.plain_result("❌ 面板配置异常，请检查 WebUI 配置。")

        client.token = None
        results = []

        # 1. 检测连接
        try:
            await client.get_envs()
            results.append(f"✅ 青龙连接正常（{cfg.get('auth_mode', 'OpenAPI')}）")
        except RuntimeError as e:
            return event.plain_result(
                f"❌ 青龙连接失败: {e}\n\n"
                f"请检查 WebUI 插件配置：\n"
                f"  • 面板地址是否正确（含 http:// 和端口）\n"
                f"  • Client ID / Client Secret 是否正确\n"
                f"  • 面板是否可从 AstrBot 所在网络访问\n"
                f"修改配置后保存，再执行 /ql init。"
            )

        # 2. 创建/对齐定时任务（与订阅目录一致：任务名=脚本文件名，命令=task 仓库目录/脚本.py）
        #    青龙订阅 autoAddCron 自动创建的任务即此格式；init 不再创建「xxx签到」风格任务，避免重复。
        REPO_DIR = "fiezhu_astrbot_plugin_qlpanel-sql_master"
        script_tasks = [
            ("3gbizhi.py", "3gbizhi签到"),
            ("zibi_check_in.py", "zibi签到"),
        ]
        for script_file, old_task_name in script_tasks:
            try:
                crons = await client.get_crons()
                sub_cmd = f"task {REPO_DIR}/{script_file}"
                handled_ids = set()
                # 订阅风格任务：命令为 task <仓库目录>/<脚本>
                sub_task = next(
                    (c for c in crons if (c.get("command") or "").strip().endswith(f"/{script_file}")),
                    None,
                )
                if sub_task:
                    results.append(
                        f"✅ 签到任务已存在（与订阅一致）: {sub_task.get('name')} (ID: {sub_task.get('id')})"
                    )
                    handled_ids.add(sub_task["id"])
                else:
                    # 找不到订阅风格任务：若存在旧版「xxx签到」任务则重命名对齐，否则新建
                    legacy_task = next(
                        (c for c in crons if old_task_name.lower() in (c.get("name") or "").lower()),
                        None,
                    )
                    if legacy_task:
                        await client.update_cron(
                            legacy_task["id"], name=script_file, command=sub_cmd
                        )
                        results.append(
                            f"✅ 已把旧任务 {legacy_task.get('name')} 对齐为订阅风格: "
                            f"{script_file}（命令: {sub_cmd}）"
                        )
                        handled_ids.add(legacy_task["id"])
                    else:
                        new_cron = await client.add_cron(script_file, sub_cmd)
                        new_id = None
                        nd = new_cron.get("data") or []
                        if isinstance(nd, list) and nd:
                            new_id = nd[0].get("id") if isinstance(nd[0], dict) else None
                        elif isinstance(nd, dict):
                            new_id = nd.get("id")
                        if new_id is not None:
                            handled_ids.add(new_id)
                        results.append(
                            f"✅ 已创建定时任务: {script_file}（命令: {sub_cmd}）"
                        )
                # 清理多余的旧版重复任务（如残留的「3gbizhi签到」/「zibi签到」）
                for c in crons:
                    cid = c.get("id")
                    if cid in handled_ids:
                        continue
                    cname = (c.get("name") or "").lower()
                    ccmd = (c.get("command") or "").strip()
                    is_old_style = (
                        old_task_name.lower() in cname
                        and not ccmd.endswith(f"/{script_file}")
                    )
                    if is_old_style:
                        await client.delete_cron(cid)
                        results.append(f"🗑️ 已删除重复旧任务: {c.get('name')} (ID: {cid})")
            except RuntimeError as e:
                results.append(f"❌ 任务处理失败: {script_file}: {e}")

        # 3. 添加订阅
        try:
            subs = await client.get_subscriptions()
            existing_sub = next(
                (s for s in subs if REPO_URL in (s.get("url") or "")), None
            )
            if existing_sub:
                sub_id = existing_sub.get("id")
                # 老订阅可能未开自动添加，补齐字段确保脚本能同步进脚本管理
                try:
                    await client.update_subscription(
                        sub_id,
                        whitelist="3gbizhi.py|zibi_check_in.py",
                        autoAddCron=True,
                    )
                    results.append(f"✅ 订阅已存在: {SUB_NAME} (ID: {sub_id})（已确认自动添加脚本开启）")
                except RuntimeError:
                    results.append(f"✅ 订阅已存在: {SUB_NAME} (ID: {sub_id})")
            else:
                sub_result = await client.add_subscription(
                    name=SUB_NAME, url=REPO_URL, schedule=SUB_SCHEDULE,
                    whitelist="3gbizhi.py|zibi_check_in.py", auto_add_cron=True,
                )
                sub_data = sub_result.get("data") or {}
                if isinstance(sub_data, list) and sub_data:
                    sub_id = sub_data[0].get("id")
                elif isinstance(sub_data, dict):
                    sub_id = sub_data.get("id")
                else:
                    sub_id = None
                results.append(f"✅ 已添加订阅: {SUB_NAME}（每天0点自动拉取）")
        except RuntimeError as e:
            results.append(
                f"❌ 订阅添加失败: {e}\n"
                f"   请手动操作：青龙面板 → 订阅管理 → 新建订阅\n"
                f"   名称: {SUB_NAME}\n"
                f"   类型: 公开仓库\n"
                f"   链接: {REPO_URL}\n"
                f"   定时类型: crontab，定时规则: {SUB_SCHEDULE}\n"
                f"   保存后点「运行」拉取脚本"
            )
            sub_id = None

        # 4. 拉取订阅
        if sub_id:
            try:
                await client.run_subscription(sub_id)
                results.append(f"✅ 已触发订阅拉取，脚本即将同步到青龙脚本管理")
            except RuntimeError as e:
                results.append(f"⚠️ 触发拉取失败: {e}（可在青龙订阅管理手动点「运行」）")

        # 5. 清理旧版持久化账号环境变量（v2.x 遗留的 BIZHI_COOKIE / ZIBI_CONFIG）
        #    新架构账号只存插件本地文件，青龙不再持久存储任何账号数据
        try:
            envs = await client.get_envs()
            legacy_found = []
            for legacy_name in (LEGACY_ENV_3D, LEGACY_ENV_ZIBI):
                if any(e.get("name") == legacy_name for e in envs):
                    legacy_found.append(legacy_name)
            if legacy_found:
                removed_note = "、".join(legacy_found)
                try:
                    for legacy_name in legacy_found:
                        matches = await client.find_envs_by_name(legacy_name)
                        ids = [QinglongClient.env_id(e) for e in matches
                               if QinglongClient.env_id(e) is not None]
                        if ids:
                            await client.delete_envs(ids)
                    results.append(f"✅ 已清理旧版账号环境变量 {removed_note}（新架构账号只存本地，更安全）")
                except RuntimeError as e:
                    results.append(f"⚠️ 检测到旧版环境变量 {removed_note}，清理失败: {e}")
            else:
                results.append(f"✅ 账号存储: 本地文件（青龙不存储任何账号数据）")
        except RuntimeError as e:
            results.append(f"⚠️ 环境变量检查失败: {e}")

        # 6. 检测青龙脚本管理中的签到脚本是否为 v1.0.0 新版（读临时运行数据）
        script_notes = await self._check_script_version(client)
        results.extend(script_notes)

        summary = "\n".join(results)
        all_ok = all(r.startswith("✅") or r.startswith("ℹ️") for r in results)
        header = "🎉 初始化完成！" if all_ok else "⚠️ 初始化部分失败，请检查上方错误。"
        return event.plain_result(
            f"{header}\n\n{summary}\n\n"
            f"脚本拉取后，用户可以 /3d set <Cookie> 上传签到 Cookie，/3d run 立即签到。\n"
            f"迁移服务器: /ql backup 导出账号 → 拷贝 accounts_backup.json → /ql restore 恢复"
        )

    async def _cmd_bind(self, event: AstrMessageEvent, args: List[str]):
        if len(args) < 3:
            return event.plain_result(
                "用法: /ql bind <面板地址> <Client ID> <Client Secret>\n"
                "例: /ql bind http://127.0.0.1:5700 你的ClientID 你的ClientSecret\n"
                "（Client ID/Secret 在青龙 系统设置 → 应用设置 中创建）"
            )
        url, client_id, client_secret = args[0], args[1], args[2]
        url = self._normalize_base_url(url)

        client = QinglongClient(base_url=url, client_id=client_id, client_secret=client_secret)
        async with aiohttp.ClientSession() as session:
            try:
                token = await client._login(session)
            except RuntimeError as e:
                return event.plain_result(f"❌ 绑定失败: {e}")

        self._panel = {
            "url": url,
            "client_id": client_id,
            "client_secret": client_secret,
            "token": token,
            "bound_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        await self._save_panel()
        webui_note = ""
        if self.config and (self.config.get("ql_url") or "").strip():
            webui_note = "\n⚠️ 检测到 WebUI 已配置面板地址，将以 WebUI 配置为准，命令绑定暂不生效。"
        return event.plain_result(
            f"✅ 面板绑定成功（命令方式）！\n面板: {url}\nClient ID: {_mask(client_id, 6)}\n\n"
            f"💡 推荐在 AstrBot WebUI → 插件配置 中填写面板信息，更方便。\n"
            f"用户现在可以 /3d set <Cookie> 上传签到 Cookie，/3d run 运行签到。{webui_note}"
        )

    async def _cmd_unbind(self, event: AstrMessageEvent):
        cfg = self._get_panel_config()
        if not cfg:
            return event.plain_result("⚠️ 尚未绑定面板。")
        if cfg.get("source") == "WebUI配置":
            return event.plain_result(
                "⚠️ 当前面板来自 WebUI 配置，无法通过命令解绑。\n"
                "请在 AstrBot WebUI → 插件配置 → 清空「青龙面板地址」后保存。"
            )
        self._panel = None
        await self._save_panel()
        return event.plain_result("✅ 已解绑命令绑定的面板。")

    async def _cmd_status(self, event: AstrMessageEvent):
        cfg = self._get_panel_config()
        if not cfg:
            return event.plain_result(
                "⚠️ 尚未绑定面板。\n"
                "推荐：AstrBot WebUI → 插件配置 → 填写青龙面板地址/Client ID/Client Secret 后保存。\n"
                "或执行: /ql bind <面板地址> <Client ID> <Client Secret>"
            )
        client = self._new_client()
        try:
            crons = await client.get_crons()
            active = [c for c in crons if not c.get("isDisabled")]
            return event.plain_result(
                f"📊 青龙面板状态\n"
                f"配置来源: {cfg.get('source', '-')}\n"
                f"认证方式: {cfg.get('auth_mode', 'OpenAPI')}（Client ID）\n"
                f"面板地址: {cfg['url']}\n"
                f"Client ID: {_mask(cfg.get('client_id') or '-', 6)}\n"
                f"3D壁纸任务: 3gbizhi.py\n"
                f"子比签到任务: zibi_check_in.py\n"
                f"定时任务: {len(crons)} 个（启用 {len(active)} 个）\n"
                f"账号存储: 本地文件 users.json（青龙不存储账号）\n"
                f"注册用户: {len(self._users)} 人 | 3D账号: {len(self._all_3d_accounts())} 个 | 子比账号: {len(self._all_zibi_accounts())} 个\n"
                f"备份文件: {'✅ 已存在' if self.backup_file.exists() else '未备份'}（/ql backup 导出）\n"
                f"定时签到: {self.daily_time or '已禁用'} | 日志推送: {'开' if self.push_after_run else '关'}"
            )
        except RuntimeError as e:
            return event.plain_result(f"❌ 面板状态获取失败: {e}")

    async def _cmd_env(self, event: AstrMessageEvent, args: List[str]):
        if not self._get_panel_config():
            return event.plain_result("⚠️ 尚未绑定面板。")
        client = self._new_client()
        try:
            if not args or args[0].lower() == "list":
                envs = await client.get_envs()
                if not envs:
                    return event.plain_result("📭 面板暂无环境变量。")
                lines = [f"{i + 1}. {e.get('name', '?')} = {_mask(str(e.get('value', '')), 10)}" for i, e in enumerate(envs)]
                return event.plain_result("🔑 环境变量列表:\n" + "\n".join(lines[:50]) +
                                          ("\n……（仅前 50 条）" if len(envs) > 50 else ""))
            action = args[0].lower()
            if action in ("add", "set", "update"):
                if len(args) < 3:
                    return event.plain_result("用法: /ql env add <名称> <值>")
                name = args[1].strip()
                value = " ".join(args[2:]).strip()
                if not name or not value:
                    return event.plain_result("❌ 名称和值不能为空。")
                return event.plain_result("✅ " + await self._write_env(client, name, value, "管理员手动"))
            if action in ("del", "delete", "remove"):
                if len(args) < 2:
                    return event.plain_result("用法: /ql env del <名称>")
                matches = await client.find_envs_by_name(args[1])
                if not matches:
                    return event.plain_result(f"⚠️ 未找到环境变量 {args[1]}。")
                await client.delete_envs([QinglongClient.env_id(m) for m in matches if QinglongClient.env_id(m) is not None])
                return event.plain_result(f"✅ 已删除 {len(matches)} 个 {args[1]}。")
            return event.plain_result("未知 env 子命令，支持: list / add / del")
        except RuntimeError as e:
            return event.plain_result(f"❌ 操作失败: {e}")

    async def _cmd_cron(self, event: AstrMessageEvent, args: List[str]):
        if not self._get_panel_config():
            return event.plain_result("⚠️ 尚未绑定面板。")
        client = self._new_client()
        try:
            if not args or args[0].lower() == "list":
                crons = await client.get_crons()
                if not crons:
                    return event.plain_result("📭 面板暂无定时任务。")
                lines = [f"{'✅' if not c.get('isDisabled') else '⛔'} [{c.get('id')}] {c.get('name') or c.get('command', '?')}" for c in crons[:50]]
                return event.plain_result("🗓️ 定时任务列表:\n" + "\n".join(lines))
            action = args[0].lower()
            if action in ("run", "start", "log"):
                if len(args) < 2:
                    return event.plain_result("用法: /ql cron run|log <任务名/ID>")
                crons = await client.get_crons()
                target = None
                if args[1].isdigit():
                    target = next((c for c in crons if str(c.get("id")) == args[1]), None)
                if not target:
                    target = next((c for c in crons if args[1].lower() in (c.get("name") or "").lower()), None)
                if not target:
                    return event.plain_result(f"⚠️ 未找到任务: {args[1]}")
                if action in ("run", "start"):
                    await client.run_cron(target["id"])
                    return event.plain_result(f"🚀 已触发运行 [{target['id']}] {target.get('name')}\n任务日志稍后可用 /ql cron log {target['id']} 查看。")
                content = await client.get_cron_log(target["id"])
                if not content or not content.strip():
                    return event.plain_result(f"📄 任务 [{target['id']}] {target.get('name')} 暂无日志。")
                return event.plain_result(f"📄 任务 [{target['id']}] {target.get('name')} 日志（末尾）:\n\n{content.strip()[-2000:]}")
            return event.plain_result("未知 cron 子命令，支持: list / run / log")
        except RuntimeError as e:
            return event.plain_result(f"❌ 操作失败: {e}")

    # ---------------- /zibi 命令（所有用户） ----------------
    @filter.command("zibi")
    async def cmd_zibi(self, event: AstrMessageEvent):
        self._patch_plain_result(event)
        try:
            parts, _ = self._parse_args(event)
            if not parts:
                yield event.plain_result(self._help_zibi())
                return
            sub = parts[0].lower()
            if sub in ("help", "帮助", "-h"):
                yield event.plain_result(self._help_zibi())
            elif sub == "set":
                yield await self._cmd_zibi_set(event, parts[1:])
            elif sub == "run":
                yield event.plain_result(await self._run_zibi_and_push(event))
            elif sub == "del":
                yield await self._cmd_zibi_del(event, parts[1:])
            elif sub == "me":
                yield await self._cmd_zibi_me(event)
            elif sub == "time":
                yield await self._cmd_time(event, parts[1:], "zibi")
            elif sub == "push":
                yield await self._cmd_push(event, parts[1:])
            elif sub == "list":
                yield await self._cmd_zibi_list(event)
            else:
                yield event.plain_result(f"未知命令: {sub}\n\n" + self._help_zibi())
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] /zibi 命令异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 处理出错: {e}")

    async def _cmd_zibi_set(self, event: AstrMessageEvent, args: List[str]):
        """绑定子比网站：/zibi set <网站地址> <Cookie>（追加一个新账号）"""
        if not self._get_panel_config():
            return event.plain_result("⚠️ 管理员还未绑定青龙面板，请联系管理员。")
        uid = self._get_user_id(event)
        origin = self._get_origin(event)

        if not args:
            return event.plain_result(
                "用法:\n"
                "1. /zibi set <网站地址> <Cookie>\n"
                "   例: /zibi set https://www.725361764.cn \"wordpress_logged_in_xxx=abc; ...\"\n"
                "2. /zibi set <网站地址> + 发送 txt 文件（Cookie 在文件里）\n"
                "每个用户可绑定多个网站（再次 set 为追加新账号）"
            )

        host = args[0].strip().rstrip("/")
        if not re.match(r"^https?://", host, re.IGNORECASE):
            return event.plain_result("❌ 网站地址需以 http:// 或 https:// 开头。")
        # 规范化：协议/域名小写、去末尾斜杠（防止同一个站因写法不同被重复绑定）
        host = self._normalize_site_url(host)

        # 读取 Cookie（文件优先）
        file_component = await self._extract_file_component(event)
        if file_component:
            try:
                file_name, content = await self._download_uploaded_file(file_component)
            except RuntimeError as e:
                return event.plain_result(f"❌ {e}")
            cookie = self._clean_cookie(content)
            if not cookie:
                return event.plain_result("⚠️ 文件内容为空，未读取到 Cookie。")
        else:
            if len(args) < 2:
                return event.plain_result(
                    "❌ 缺少 Cookie。用法: /zibi set <网站地址> <Cookie>\n"
                    "也可以 /zibi set <网站地址> 后直接发送 txt 文件（文件内容作为 Cookie）"
                )
            cookie = self._clean_cookie(" ".join(args[1:]))

        user = self._find_user(uid)
        if user:
            user.setdefault("zibi_sites", [])
            # 去重：仅当「网站 + Cookie」与已有账号完全相同（host 忽略大小写/末尾斜杠）才提示已绑定；
            # 同一网站的不同账号（不同 Cookie）属于不同签到账号，一律追加保留，绝不覆盖。
            dup = next(
                (s for s in user["zibi_sites"]
                 if (s.get("host") or "").strip().rstrip("/").lower() == host.lower()
                 and (s.get("cookie") or "").strip() == cookie),
                None,
            )
            if dup is not None:
                user["origin"] = origin or user.get("origin", "")
                user["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                await self._save_users()
                return event.plain_result(
                    f"ℹ️ 该网站 + Cookie 已绑定过（{host}），无需重复添加。\n"
                    f"同一网站的不同账号请使用不同的 Cookie 再次 /zibi set。\n"
                    f"发送 /zibi me 查看你的全部账号。"
                )
            user["zibi_sites"].append({"host": host, "cookie": cookie})
            user["origin"] = origin or user.get("origin", "")
            user["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            my_nums = [n for u, n, _ in self._all_zibi_accounts() if u.get("uid") == uid]
            num = my_nums[-1] if my_nums else 0
            msg = f"✅ 已追加你的子比网站账号（账号{num}）\n🌐 {host}\n🍪 {_mask(cookie, 10)}"
        else:
            self._users.append({
                "uid": uid,
                "origin": origin,
                "zibi_sites": [{"host": host, "cookie": cookie}],
                "joined_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            msg = f"✅ 已绑定你的子比网站（账号1）\n🌐 {host}\n🍪 {_mask(cookie, 10)}"
        await self._save_users()
        return event.plain_result(
            f"{msg}\n🔒 账号已安全存储在本插件数据目录（青龙不再存储账号，只有运行你的签到时才临时使用）\n\n"
            f"发送 /zibi run 立即签到，或等待每日定时签到。"
        )

    async def _cmd_zibi_del(self, event: AstrMessageEvent, args: List[str]):
        """删除指定网站账号：/zibi del <我的账号序号>"""
        uid = self._get_user_id(event)
        user = self._find_user(uid)
        sites = (user or {}).get("zibi_sites") or []
        if not sites:
            return event.plain_result("⚠️ 你还没有绑定子比网站。\n用法: /zibi set <网站地址> <Cookie>")
        if not args or not args[0].isdigit():
            lines = "\n".join(f"{i + 1}. {s.get('host')} | {_mask(s.get('cookie', ''), 12)}" for i, s in enumerate(sites))
            return event.plain_result(
                f"📋 你的子比网站账号（{len(sites)} 个）:\n{lines}\n\n"
                f"删除用法: /zibi del <序号>"
            )
        idx = int(args[0])
        if not (1 <= idx <= len(sites)):
            return event.plain_result(f"❌ 序号超出范围（1-{len(sites)}）。")
        removed = sites.pop(idx - 1)
        user["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        await self._save_users()
        return event.plain_result(f"🗑️ 已删除你的网站账号 {idx}: {removed.get('host')}，下次签到将不再包含。")

    async def _cmd_zibi_me(self, event: AstrMessageEvent):
        uid = self._get_user_id(event)
        user = self._find_user(uid)
        sites = (user or {}).get("zibi_sites") or []
        if not sites:
            return event.plain_result("⚠️ 你还没有绑定子比网站。\n用法: /zibi set <网站地址> <Cookie>")
        push_status = "开" if user.get("push_enabled", True) else "关"
        user_time = user.get("daily_time_zibi") or ""
        time_display = user_time if user_time else f"跟随全局（{self.daily_time or '已禁用'}）"
        # 每个网站的全局序号（按全局平铺顺序）
        my_nums = [n for u, n, _ in self._all_zibi_accounts() if u.get("uid") == uid]
        lines = []
        for i, s in enumerate(sites, 1):
            num = my_nums[i - 1] if i - 1 < len(my_nums) else i
            lines.append(f"{i}. 账号{num} | {s.get('host')} | {_mask(s.get('cookie', ''), 12)}")
        return event.plain_result(
            f"👤 我的子比网站（{len(sites)} 个账号）\n"
            + "\n".join(lines)
            + f"\n子比签到时间: {time_display}\n"
            f"结果推送: {push_status}\n"
            f"注册时间: {user.get('joined_at', '-')}\n"
            f"最近更新: {user.get('updated_at', '-')}\n"
            f"删除: /zibi del <序号>"
        )

    async def _cmd_zibi_list(self, event: AstrMessageEvent):
        accounts = self._all_zibi_accounts()
        if not accounts:
            return event.plain_result("📭 暂无用户绑定子比网站。")
        lines = [
            f"{num}. {u['uid']} | {s.get('host')} | {_mask(s.get('cookie', ''), 8)}"
            for u, num, s in accounts
        ]
        return event.plain_result(f"🌐 子比网站绑定列表（{len(accounts)} 个账号）:\n" + "\n".join(lines))

    # ---------------- /3d 命令（所有用户） ----------------
    @filter.command("3d")
    async def cmd_3d(self, event: AstrMessageEvent):
        self._patch_plain_result(event)
        try:
            parts, _ = self._parse_args(event)
            if not parts:
                # 无参数：直接运行签到
                yield event.plain_result(await self._run_sign_and_push(event))
                return
            sub = parts[0].lower()
            if sub in ("help", "帮助", "-h"):
                yield event.plain_result(self._help_3d())
            elif sub == "set":
                yield await self._cmd_set(event, parts[1:])
            elif sub == "run":
                yield event.plain_result(await self._run_sign_and_push(event))
            elif sub == "del":
                yield await self._cmd_del(event, parts[1:])
            elif sub == "me":
                yield await self._cmd_me(event)
            elif sub == "time":
                yield await self._cmd_time(event, parts[1:], "3d")
            elif sub == "push":
                yield await self._cmd_push(event, parts[1:])
            elif sub == "list":
                yield await self._cmd_list(event)
            else:
                yield event.plain_result(f"未知命令: {sub}\n\n" + self._help_3d())
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] /3d 命令异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 处理出错: {e}")

    async def _cmd_set(self, event: AstrMessageEvent, args: List[str]):
        """注册/追加 3D壁纸签到账号：/3d set <Cookie>（多个账号用多次 set 追加）"""
        if not self._get_panel_config():
            return event.plain_result("⚠️ 管理员还未绑定青龙面板，请联系管理员。")
        uid = self._get_user_id(event)
        origin = self._get_origin(event)

        # 优先读取上传文件
        file_component = await self._extract_file_component(event)
        if file_component:
            try:
                file_name, content = await self._download_uploaded_file(file_component)
            except RuntimeError as e:
                return event.plain_result(f"❌ {e}")
            cookie = self._clean_cookie(content)
            if not cookie:
                return event.plain_result("⚠️ 文件内容为空，未读取到 Cookie。")
        else:
            if not args:
                return event.plain_result(
                    "用法:\n"
                    "1. /3d set <Cookie>（每个账号发一次，可绑定多个账号）\n"
                    "2. /3d set + 发送 txt 文件（自动读取文件内容作为 Cookie）"
                )
            cookie = self._clean_cookie(" ".join(args))

        user = self._find_user(uid)
        if user:
            user.setdefault("cookies", [])
            # 去重：相同 Cookie 视为同一账号，更新而不是重复追加
            if cookie in [ck for ck in user["cookies"] if ck]:
                user["origin"] = origin or user.get("origin", "")
                user["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                my_nums = [n for u, n, _ in self._all_3d_accounts() if u.get("uid") == uid]
                num = my_nums[-1] if my_nums else 0
                await self._save_users()
                return event.plain_result(
                    f"ℹ️ 该 Cookie 已绑定过（账号{num}），无需重复添加。\n"
                    f"🍪 {_mask(cookie, 10)}\n"
                    f"发送 /3d run 立即签到，或等待每日定时签到。"
                )
            user["cookies"].append(cookie)
            user["origin"] = origin or user.get("origin", "")
            user["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            my_nums = [n for u, n, _ in self._all_3d_accounts() if u.get("uid") == uid]
            num = my_nums[-1] if my_nums else 0
            msg = f"✅ 已追加你的签到账号（账号{num}）\n🍪 {_mask(cookie, 10)}"
        else:
            self._users.append({
                "uid": uid,
                "cookies": [cookie],
                "origin": origin,
                "joined_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            msg = f"✅ 已注册你的签到账号（账号1）\n🍪 {_mask(cookie, 10)}"
        await self._save_users()
        return event.plain_result(
            f"{msg}\n🔒 账号已安全存储在本插件数据目录（青龙不再存储账号，只有运行你的签到时才临时使用）\n\n"
            f"发送 /3d run 立即签到，或等待每日定时签到。"
        )

    async def _cmd_del(self, event: AstrMessageEvent, args: List[str]):
        """删除指定签到账号：/3d del <我的账号序号>"""
        uid = self._get_user_id(event)
        user = self._find_user(uid)
        cookies = (user or {}).get("cookies") or []
        if not cookies:
            return event.plain_result("⚠️ 你还没有注册 Cookie。\n用法: /3d set <Cookie>")
        if not args or not args[0].isdigit():
            lines = "\n".join(f"{i + 1}. {_mask(ck, 12)}" for i, ck in enumerate(cookies))
            return event.plain_result(
                f"📋 你的签到账号（{len(cookies)} 个）:\n{lines}\n\n"
                f"删除用法: /3d del <序号>"
            )
        idx = int(args[0])
        if not (1 <= idx <= len(cookies)):
            return event.plain_result(f"❌ 序号超出范围（1-{len(cookies)}）。")
        removed = cookies.pop(idx - 1)
        user["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        await self._save_users()
        return event.plain_result(f"🗑️ 已删除你的账号 {idx}（{_mask(removed, 12)}），下次签到将不再包含。")

    async def _cmd_me(self, event: AstrMessageEvent):
        uid = self._get_user_id(event)
        user = self._find_user(uid)
        cookies = (user or {}).get("cookies") or []
        if not cookies:
            return event.plain_result("⚠️ 你还没有注册 Cookie。\n用法: /3d set <Cookie>")
        push_status = "开" if user.get("push_enabled", True) else "关"
        user_time = user.get("daily_time_3d") or ""
        time_display = user_time if user_time else f"跟随全局（{self.daily_time or '已禁用'}）"
        my_nums = [n for u, n, _ in self._all_3d_accounts() if u.get("uid") == uid]
        lines = []
        for i, ck in enumerate(cookies, 1):
            num = my_nums[i - 1] if i - 1 < len(my_nums) else i
            lines.append(f"{i}. 账号{num} | {_mask(ck, 12)}")
        return event.plain_result(
            f"👤 我的签到账号（{len(cookies)} 个）\n"
            + "\n".join(lines)
            + f"\n3D签到时间: {time_display}\n"
            f"结果推送: {push_status}\n"
            f"注册时间: {user.get('joined_at', '-')}\n"
            f"最近更新: {user.get('updated_at', '-')}\n"
            f"删除: /3d del <序号>"
        )

    async def _cmd_time(self, event: AstrMessageEvent, args: List[str], feature: str = "3d"):
        """设置/重置个人签到时间（3D 与子比各自独立）：
        /3d time HH:MM → daily_time_3d；/zibi time HH:MM → daily_time_zibi"""
        key = "daily_time_3d" if feature == "3d" else "daily_time_zibi"
        label = "3D壁纸" if feature == "3d" else "子比"
        uid = self._get_user_id(event)
        user = self._find_user(uid)
        if not user or not ((user.get("cookies") or []) or (user.get("zibi_sites") or [])):
            return event.plain_result("⚠️ 你还没有注册任何签到账号，请先 /3d set <Cookie> 或 /zibi set <网站> <Cookie>。")
        if not args:
            cur = user.get(key) or ""
            other_key = "daily_time_zibi" if feature == "3d" else "daily_time_3d"
            other = user.get(other_key) or ""
            return event.plain_result(
                f"⏰ {label}签到时间: {cur if cur else f'跟随全局（{self.daily_time or '已禁用'}）'}\n"
                f"{'子比' if feature == '3d' else '3D壁纸'}签到时间: {other if other else '跟随全局'}\n"
                f"用法: /{feature} time 08:30  设置{label}个人时间\n"
                f"      /{feature} time reset  恢复跟随全局\n"
                f"（两个功能的时间相互独立）"
            )
        val = args[0].strip().lower()
        if val in ("reset", "默认", "global"):
            user[key] = ""
            await self._save_users()
            return event.plain_result(f"✅ 已恢复{label}签到跟随全局时间（{self.daily_time or '已禁用'}）。")
        # 校验 HH:MM
        if not re.match(r"^\d{1,2}:\d{2}$", val):
            return event.plain_result("❌ 时间格式错误，请用 HH:MM，例如 08:30。")
        hh, mm = int(val.split(":")[0]), int(val.split(":")[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return event.plain_result("❌ 时间超出范围，小时 0-23，分钟 0-59。")
        user[key] = f"{hh:02d}:{mm:02d}"
        await self._save_users()
        return event.plain_result(
            f"✅ 已设置{label}签到时间为 {user[key]}。\n"
            f"到点将自动触发{label}签到并推送结果（如开启推送），不影响另一个功能的定时。"
        )

    async def _cmd_push(self, event: AstrMessageEvent, args: List[str]):
        """开启/关闭个人结果推送：/3d push on|off"""
        uid = self._get_user_id(event)
        user = self._find_user(uid)
        if not user or not ((user.get("cookies") or []) or (user.get("zibi_sites") or [])):
            return event.plain_result("⚠️ 你还没有注册任何签到账号，请先 /3d set <Cookie> 或 /zibi set <网站> <Cookie>。")
        if not args:
            status = "开" if user.get("push_enabled", True) else "关"
            return event.plain_result(
                f"📤 当前结果推送: {status}\n用法: /3d push on  开启\n      /3d push off 关闭"
            )
        val = args[0].strip().lower()
        if val in ("on", "开", "开启", "1", "true"):
            user["push_enabled"] = True
            await self._save_users()
            return event.plain_result("✅ 已开启签到结果推送，签到完成后会私聊发送结果。")
        if val in ("off", "关", "关闭", "0", "false"):
            user["push_enabled"] = False
            await self._save_users()
            return event.plain_result("✅ 已关闭签到结果推送，你仍可手动 /3d run 查看结果。")
        return event.plain_result("❌ 参数错误，用法: /3d push on|off")

    async def _cmd_list(self, event: AstrMessageEvent):
        accounts = self._all_3d_accounts()
        if not accounts:
            return event.plain_result("📭 暂无注册账号。")
        lines = [
            f"{num}. {u['uid']} | {_mask(ck, 8)} | {u.get('updated_at', '-')}"
            for u, num, ck in accounts
        ]
        uid = self._get_user_id(event)
        my_nums = [n for u, n, _ in accounts if u.get("uid") == uid]
        my_line = f"（你的账号: {'、'.join(map(str, my_nums))}）" if my_nums else "（你尚未注册）"
        return event.plain_result(f"👥 签到账号列表（{len(accounts)} 个）{my_line}:\n" + "\n".join(lines))

    # ---------------- 文件自动接收 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_upload_file(self, event: AstrMessageEvent):
        """用户直接发送文件（不带命令）时，自动当作 Cookie 注册"""
        try:
            text = (event.message_str or "").strip()
            if text.lower().startswith(("/ql", "ql ", "/3d", "3d ", "/zibi", "zibi ")):
                return
            file_component = await self._extract_file_component(event)
            if not file_component:
                return
            if not self._get_panel_config():
                yield event.plain_result(
                    "📎 检测到你上传了文件，但管理员还未绑定青龙面板。\n请联系管理员执行 /ql bind 后重试。"
                )
                return
            try:
                file_name, content = await self._download_uploaded_file(file_component)
            except RuntimeError as e:
                yield event.plain_result(f"❌ {e}")
                return
            cookie = self._clean_cookie(content)
            if not cookie:
                yield event.plain_result("⚠️ 文件内容为空，未读取到 Cookie。")
                return
            uid = self._get_user_id(event)
            origin = self._get_origin(event)
            user = self._find_user(uid)
            if user:
                user.setdefault("cookies", [])
                user["cookies"].append(cookie)
                user["origin"] = origin or user.get("origin", "")
                user["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                my_nums = [n for u, n, _ in self._all_3d_accounts() if u.get("uid") == uid]
                num = my_nums[-1] if my_nums else 0
                msg = f"✅ 已追加你的签到账号（账号{num}）\n🍪 {_mask(cookie, 10)}"
            else:
                self._users.append({
                    "uid": uid, "cookies": [cookie], "origin": origin,
                    "joined_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                msg = f"✅ 已注册你的签到账号（账号1）\n🍪 {_mask(cookie, 10)}"
            await self._save_users()
            yield event.plain_result(
                f"{msg}\n🔒 账号已安全存储在本插件数据目录（青龙不再存储账号，只有运行你的签到时才临时使用）\n\n"
                f"发送 /3d run 立即签到。"
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 文件处理异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 文件处理出错: {e}")

    async def terminate(self):
        """插件卸载/禁用时保存数据"""
        await self._save_panel()
        await self._save_users()
        logger.info(f"[{PLUGIN_ID}] 插件已卸载，数据已保存。")
