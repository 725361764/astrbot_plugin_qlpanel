#!/usr/bin/env python3
# 3gbizhi 青龙签到｜QLpanel-pro 安全架构版
# 运行数据从临时环境变量 BIZHI_RUN_DATA 读取（JSON 数组，由插件按用户注入，运行后自动删除）
#   BIZHI_RUN_DATA   ["cookie1","cookie2",...]
# 每个账号之间随机等待 3~10 秒，避免过快触发风控。
import os
import sys
import json
import time
import random

import requests

if sys.version_info[0] == 3:
    sys.stdout.reconfigure(encoding='utf-8')

ENV_NAME = "BIZHI_RUN_DATA"
END_MARK = "====任务结束===="

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://www.3gbizhi.com/",
    "X-Requested-With": "XMLHttpRequest"
}
base_url = "https://www.3gbizhi.com/api/user"


def run_one(cookie_str):
    session = requests.Session()
    session.headers.update(headers)
    session.headers["Cookie"] = cookie_str

    ret_item = {
        "status": "失败",
        "continuous": 0,
        "week": 0,
        "month": 0,
        "today_points": 0,
        "total_points": 0
    }
    print("\n====== 执行签到 ======")
    try:
        resp = session.get(f"{base_url}/getUserSignin", timeout=15)
        ret = resp.json()
        if "data" not in ret:
            print(f"❌Cookie失效:{ret}")
            ret_item["status"] = "Cookie失效"
            return ret_item

        d = ret["data"]
        signed = d.get("signin_op", 0)
        continuous = d.get("continuous_days", 0)
        week = d.get("week_signin_days", 0)
        month = d.get("month_signin_days", 0)

        today_points = 0
        sign_list = d.get("signin_dates", [])
        if len(sign_list) > 0:
            today_points = sign_list[0].get("points", 0)

        ret_item["continuous"] = continuous
        ret_item["week"] = week
        ret_item["month"] = month
        ret_item["today_points"] = today_points

        print(f"📊连续签到:{continuous}天 | 本周:{week}天 | 本月:{month}天")
        print(f"✨今日签到获得积分：{today_points}")

        total_points = 0
        try:
            info_resp = session.get(f"{base_url}/getUserInfo", timeout=15)
            info_json = info_resp.json()
            if info_json and info_json.get("data"):
                total_points = info_json["data"].get("points", 0)
                print(f"💰账号总积分：{total_points}")
        except Exception:
            print("⚠️读取总积分接口异常")
        ret_item["total_points"] = total_points

        if signed == 1:
            print("ℹ️今日已签到，无需重复执行")
            ret_item["status"] = "今日已签到"
            return ret_item

        sign_resp = session.post(f"{base_url}/setUserSignin", timeout=15)
        sign_ret = sign_resp.json()
        print(f"🎉签到执行结果：{sign_ret}")
        ret_item["status"] = "签到成功"
        return ret_item

    except Exception as e:
        print(f"💥异常:{str(e)}")
        ret_item["status"] = f"异常:{str(e)}"
        return ret_item


def main():
    raw = os.getenv(ENV_NAME, "").strip()
    if not raw:
        print("⚠️ 未检测到运行数据（BIZHI_RUN_DATA）。请通过机器人 /3d run 触发签到，不要直接在青龙手动运行。")
        print(END_MARK)
        return
    try:
        cookie_list = json.loads(raw)
        if not isinstance(cookie_list, list):
            raise ValueError("not list")
    except Exception:
        print("⚠️ 运行数据格式错误（应为 JSON 数组）。")
        print(END_MARK)
        return

    cookie_list = [c for c in cookie_list if c and str(c).strip()]
    if not cookie_list:
        print("⚠️ 运行数据为空，请先通过机器人 /3d set <Cookie> 上传签到账号。")
        print(END_MARK)
        return

    n = len(cookie_list)
    print(f"📦 本次运行 {n} 个账号，每个账号间隔 3~10 秒，预计耗时约 {n*3}~{n*10} 秒")
    output_lines = []

    for idx, ck in enumerate(cookie_list):
        res = run_one(str(ck))
        line = f"【账号{idx+1}】状态:{res['status']} 连续{res['continuous']}天 今日积分{res['today_points']} 总积分{res['total_points']}"
        output_lines.append(line)
        print(line)
        # 除最后一个账号外，随机等待 3~10 秒再执行下一个
        if idx < n - 1:
            wait = random.uniform(3, 10)
            print(f"⏳ 等待 {wait:.1f} 秒后执行下一个账号...")
            time.sleep(wait)

    print("\n====任务结束====")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"【签到结果】脚本运行异常: {e}")
        print(END_MARK)
        sys.exit(0)
