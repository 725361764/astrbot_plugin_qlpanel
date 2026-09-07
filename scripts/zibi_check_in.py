# -*- coding: utf-8 -*-
"""
子比主题(Zibll) 通用签到脚本 —— QLpanel-pro 安全架构版
=========================================================
运行数据从临时环境变量 ZIBI_RUN_DATA 读取（JSON 数组，由插件按用户注入，运行后自动删除）：
    [{"host": "https://www.example.com", "cookie": "完整Cookie字符串"}, ...]

每个账号输出一行：【账号N】网站 -> 结果
每个账号之间随机等待 3~10 秒，避免过快触发风控。
结束标记：====任务结束====  （插件据此判断日志已完整）
"""
import json
import os
import sys
import time
import random

import requests
import urllib3

# Disable the warning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENV_NAME = "ZIBI_RUN_DATA"
END_MARK = "====任务结束===="


def parse_cookie(raw: str) -> dict:
    """从完整 Cookie 字符串中提取 wordpress_logged_in_ 开头的登录态键值对"""
    result = {}
    if not raw:
        return result
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            key = key.strip()
            if key.startswith("wordpress_logged_in"):
                result[key] = value.strip()
    return result


def checkin(host: str, cookie: str) -> str:
    """对单个子比网站执行签到，返回结果文本"""
    cookie_dict = parse_cookie(cookie)
    if not cookie_dict:
        return "Cookie中未找到wordpress_logged_in登录态"

    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": host,
    }
    data = {"action": "user_checkin"}
    resp = requests.post(
        f"{host}/wp-admin/admin-ajax.php",
        cookies=cookie_dict,
        headers=headers,
        data=data,
        verify=False,
        timeout=15,
    )
    try:
        json_data = json.loads(resp.text)
        return str(json_data.get("msg", resp.text[:200]))
    except Exception:
        return f"接口返回异常: {resp.text[:200]}"


def main():
    raw = os.environ.get(ENV_NAME, "").strip()
    if not raw:
        print("⚠️ 未检测到运行数据（ZIBI_RUN_DATA）。请通过机器人 /zibi run 触发签到，不要直接在青龙手动运行。")
        print(END_MARK)
        return
    try:
        configs = json.loads(raw)
        if not isinstance(configs, list):
            raise ValueError("not list")
    except Exception:
        print("⚠️ 运行数据格式错误（应为 JSON 数组）。")
        print(END_MARK)
        return

    configs = [c for c in configs if isinstance(c, dict)
               and (c.get("host") or "").strip() and (c.get("cookie") or "").strip()]
    if not configs:
        print("⚠️ 运行数据为空，请先通过机器人 /zibi set <网站> <Cookie> 绑定网站。")
        print(END_MARK)
        return

    n = len(configs)
    print(f"📦 本次运行 {n} 个网站，每个网站间隔 3~10 秒，预计耗时约 {n*3}~{n*10} 秒")

    for idx, cfg in enumerate(configs, 1):
        host = (cfg.get("host") or "").strip()
        cookie = cfg.get("cookie") or ""
        try:
            msg = checkin(host, cookie)
            print(f"【账号{idx}】{host} -> {msg}")
        except Exception as e:
            print(f"【账号{idx}】{host} -> 签到失败: {e}")
        # 除最后一个账号外，随机等待 3~10 秒再执行下一个
        if idx < n:
            wait = random.uniform(3, 10)
            print(f"⏳ 等待 {wait:.1f} 秒后执行下一个网站...")
            time.sleep(wait)

    print(END_MARK)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"【签到结果】脚本运行异常: {e}")
        print(END_MARK)
        sys.exit(0)
