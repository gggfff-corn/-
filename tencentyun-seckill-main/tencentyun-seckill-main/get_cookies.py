"""扫码登录腾讯云, 保存 cookies.json, 并自动同步活动 URL 到 config.json.

使用 Selenium 驱动系统已装浏览器 (默认 Edge, Windows 自带;
驱动由 Selenium Manager 自动匹配, 无需下载浏览器本体).
登录成功后不写死活动期号: 只要地址栏跳到任意 act/pro/* 活动页即停,
把最终 URL 写入 config.json, 供 snap_up_server.py 自动读取
(referer / fromUrl / 时间校准都用它).

登录后顺带确认人脸核身状态 (user/get-facecheck-info ever_pass):
没通过则在同一浏览器打开验证页, 等用户做完再退出,
状态写入 config.json["facecheck"] 供抢购脚本读取.
"""

import contextlib
import json
import os
import time
from urllib.parse import quote

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_PATH = os.path.join(BASE_DIR, "cookies.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# 使用系统浏览器: "edge" (Windows 自带, 推荐) 或 "chrome".
# Selenium 4.6+ 会自动下载匹配的驱动, 不下载浏览器本体.
BROWSER = "edge"

# 当期活动落地页 (含 #MS 锚点, s_url 必须整体编码, 否则 # 会被当成外层 URL 的片段)
ACT_TARGET = (
    "https://cloud.tencent.com/act/pro/featured-202607"
    "?fromSource=gwzcw.11925098.11925098.11925098&utm_medium=cpc"
    "&utm_id=gwzcw.11925098.11925098.11925098&bd_vid=10549759730845762680#MS"
)

# 登录入口(稳定); 登录后跳到哪一期活动页则自动捕获, 无需手写
LOGIN_URL = "https://cloud.tencent.com/login?s_url=" + quote(ACT_TARGET, safe="")

LOGIN_TIMEOUT_S = 600  # 扫码登录最多等 10 分钟
FACE_URL = "https://cloud.tencent.com/act/pro/face_authentication"
FACE_POLL_S = 15
FACE_TIMEOUT_S = 600  # 验证页最多等 10 分钟


def make_driver():
    """启动系统浏览器, 驱动由 Selenium Manager 自动匹配."""
    if BROWSER == "chrome":
        options = ChromeOptions()
        options.add_argument("--start-maximized")
        return webdriver.Chrome(options=options)
    options = EdgeOptions()
    options.add_argument("--start-maximized")
    return webdriver.Edge(options=options)


def compute_g_tk(skey):
    """腾讯经典 g_tk 算法: x-csrf-token 由 skey hash 得出, 与 snap_up_server.py 同逻辑."""
    h = 5381
    for ch in skey:
        h += (h << 5) + ord(ch)
    return str(h & 0x7FFFFFFF)


def query_face_pass(cookies):
    """查人脸核身状态, 返回 True/False/None(查不到). 永不抛异常."""
    try:
        session = requests.Session()
        skey = None
        for cookie in cookies:
            session.cookies.set(
                cookie.get("name", ""),
                cookie.get("value", ""),
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
            if (
                cookie.get("name") == "skey"
                and cookie.get("value")
                and (skey is None or cookie.get("domain") == ".cloud.tencent.com")
            ):
                skey = cookie["value"]
        if not skey:
            return None
        headers = {
            "x-csrf-token": compute_g_tk(skey),
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "referer": FACE_URL,
        }
        resp = session.post(
            "https://act-api.cloud.tencent.com/user/get-facecheck-info",
            json={},
            headers=headers,
            timeout=10,
        )
        result = resp.json()
        if result.get("code") != 0:
            return None
        return bool((result.get("data") or {}).get("ever_pass"))
    except Exception as e:
        print(f"⚠️ 人脸状态查询失败: {e}")
        return None


def ensure_face_verified(driver):
    """登录后人脸核身确认, 返回 (ever_pass, checked_at).

    没通过则在同一浏览器打开验证页等用户做完 (微信扫一扫+人脸识别),
    轮询通过或超时后返回. 轮询时实时从浏览器取 Cookie,
    验证流程刷新 skey 也能立刻感知. 永不抛异常, 不影响已保存的登录态.
    """
    checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
    passed = query_face_pass(driver.get_cookies())
    if passed is True:
        print("✅ 人脸核身已通过, 无需额外操作")
        return True, checked_at
    if passed is not False:
        print("⚠️ 未能确认人脸状态, 请开抢前手动确认是否需核身")
        return None, checked_at
    print("⛔ 账号需人脸核身校验, 否则抢购必报 1100202!")
    print("👉 已在当前浏览器打开验证页, 请按页完成验证 (微信扫一扫+人脸识别)")
    try:
        driver.get(FACE_URL)
    except Exception as e:
        print(f"⚠️ 验证页打开失败, 请手动访问: {FACE_URL} ({e})")
    deadline = time.time() + FACE_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(FACE_POLL_S)
        if query_face_pass(driver.get_cookies()) is True:
            print("✅ 人脸核身已通过!")
            return True, time.strftime("%Y-%m-%d %H:%M:%S")
        print("⌛ 等待人脸核身完成...")
    print(f"⚠️ 超时未检测到通过, 请开抢前手动确认: {FACE_URL}")
    return False, checked_at


def save_cookies(driver):
    """导出当前浏览器 Cookie 到 cookies.json, 返回 cookie 列表."""
    cookies = driver.get_cookies()
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(cookies)} 个 Cookie 到 cookies.json")
    return cookies


def auto_rush_buy():
    driver = make_driver()
    try:
        driver.get(LOGIN_URL)

        print("请扫描二维码登录...")
        # 不写死期号: 跳到任意活动页即视为登录成功
        try:
            WebDriverWait(driver, LOGIN_TIMEOUT_S).until(
                EC.url_contains("/act/pro/")
            )
        except TimeoutException:
            print(f"⚠️ {LOGIN_TIMEOUT_S}s 内未检测到登录跳转, 请重试")
            return
        time.sleep(2)

        act_url = driver.current_url
        print(f"登录成功! 当前活动页: {act_url}")

        # 先存一份, 防止后续验证流程中浏览器被关掉导致登录态丢失
        save_cookies(driver)

        # 登录后人脸核身确认: 状态写入 config.json 供抢购脚本读取
        face_pass, face_checked = ensure_face_verified(driver)

        # 人脸流程可能刷新登录态 Cookie, 验证结束后重新导出覆盖一次
        try:
            save_cookies(driver)
        except Exception as e:
            print(f"⚠️ 人脸验证后重新导出 Cookie 失败, 沿用验证前快照: {e}")

        # 自动同步活动 URL, 主脚本优先读取它
        auto_config = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    auto_config = json.load(f)
            except json.JSONDecodeError:
                auto_config = {}
        auto_config["act_url"] = act_url
        auto_config["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        auto_config["facecheck"] = {"ever_pass": face_pass, "checked": face_checked}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(auto_config, f, ensure_ascii=False, indent=2)
        print("活动 URL 已同步到 config.json")
    finally:
        with contextlib.suppress(Exception):
            driver.quit()  # 验证时用户可能已手动关窗
    print("浏览器已关闭, 可以运行 snap_up_server.py 了")


if __name__ == "__main__":
    auto_rush_buy()
