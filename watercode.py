#!/usr/bin/env python3
"""通过校方网页获取用水码；登录失效时接收账号密码并自动提交。"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

from credential_store import CredentialStoreError, FILE as CREDENTIAL_FILE, load_credentials, save_credentials


HOME = "https://vcard.cup.edu.cn/uias-h5/home"
ROOT = Path(__file__).resolve().parent
CODE_RE = re.compile(r"(?<!\d)(\d{4})[\s\u00a0]{0,12}(\d{4})(?!\d)")


class WaterCodeError(Exception):
    pass


def make_driver(browser: str, compat: bool = False):
    profile = ROOT / f"profile-{browser}"
    profile.mkdir(exist_ok=True)
    prefs = {"credentials_enable_service": False, "profile.password_manager_enabled": False}
    if browser == "edge":
        options = webdriver.EdgeOptions()
        options.add_experimental_option("prefs", prefs)
        options.add_argument(f"--user-data-dir={profile}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        if compat:
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-gpu")
        return webdriver.Edge(options=options)
    options = webdriver.ChromeOptions()
    options.add_experimental_option("prefs", prefs)
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    if compat:
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
    return webdriver.Chrome(options=options)


def page_text(driver) -> str:
    try:
        return driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except WebDriverException:
        return ""


def is_home(driver) -> bool:
    parsed = urlparse(driver.current_url)
    return parsed.hostname == "vcard.cup.edu.cn" and "/uias-h5/home" in parsed.path


def ask_credentials(initial_username: str = "") -> tuple[str, str]:
    def console_prompt() -> tuple[str, str]:
        account = initial_username or input("学号 / 账号：").strip()
        secret = getpass.getpass("密码（输入时不显示）：")
        if not account or not secret:
            raise WaterCodeError("账号和密码不能为空。")
        return account, secret

    try:
        import tkinter as tk
    except ImportError:
        return console_prompt()
    try:
        root = tk.Tk()
    except tk.TclError:
        return console_prompt()

    answer = None
    root.title("学校统一身份认证")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.geometry("360x160")
    tk.Label(root, text="学号 / 账号").grid(row=0, column=0, padx=18, pady=(20, 8), sticky="e")
    username = tk.Entry(root, width=28)
    username.insert(0, initial_username)
    username.grid(row=0, column=1, padx=(0, 18), pady=(20, 8))
    tk.Label(root, text="密码").grid(row=1, column=0, padx=18, pady=8, sticky="e")
    password = tk.Entry(root, width=28, show="*")
    password.grid(row=1, column=1, padx=(0, 18), pady=8)

    def submit():
        nonlocal answer
        if username.get().strip() and password.get():
            answer = (username.get().strip(), password.get())
            root.destroy()

    tk.Button(root, text="登录", width=12, command=submit).grid(row=2, column=1, pady=12, sticky="w")
    root.bind("<Return>", lambda _event: submit())
    (password if initial_username else username).focus_set()
    root.mainloop()
    if answer is None:
        raise WaterCodeError("已取消输入账号密码。")
    return answer


def find_login_fields(driver):
    def first_visible(selector: str):
        return next(
            (e for e in driver.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed() and e.is_enabled()),
            None,
        )

    def search(depth: int):
        password = first_visible("input[type='password']")
        if password:
            for selector in (
                "input#username", "input[name='username']", "input[name='userName']",
                "input[name='account']", "input[autocomplete='username']",
                "input[placeholder*='学号']", "input[placeholder*='账号']",
                "input[type='text']", "input[type='email']", "input:not([type])",
            ):
                username = first_visible(selector)
                if username:
                    return username, password
        if depth >= 4:
            return None
        for frame in driver.find_elements(By.CSS_SELECTOR, "iframe,frame"):
            try:
                driver.switch_to.frame(frame)
                result = search(depth + 1)
                if result:
                    return result
                driver.switch_to.parent_frame()
            except WebDriverException:
                driver.switch_to.default_content()
                return None
        return None

    driver.switch_to.default_content()
    result = search(0)
    if result:
        return result
    driver.switch_to.default_content()
    return None


def submit_login(driver, username: str, password: str) -> bool:
    fields = find_login_fields(driver)
    if not fields:
        return False
    account_field, password_field = fields
    try:
        account_field.clear()
        account_field.send_keys(username)
        password_field.clear()
        password_field.send_keys(password)
        form = password_field.find_elements(By.XPATH, "./ancestor::form[1]")
        if form:
            buttons = form[0].find_elements(By.CSS_SELECTOR, "button[type='submit'],input[type='submit']")
            buttons = [b for b in buttons if b.is_displayed() and b.is_enabled()]
            if buttons:
                buttons[0].click()
                return True
        for selector in ("button[type='submit']", "input[type='submit']"):
            buttons = [b for b in driver.find_elements(By.CSS_SELECTOR, selector) if b.is_displayed() and b.is_enabled()]
            if buttons:
                buttons[0].click()
                return True
        try:
            click_label(driver, ("登录",), seconds=3)
            return True
        except WaterCodeError:
            return False
    finally:
        driver.switch_to.default_content()


def wait_for_home(driver, seconds: int, initial_username: str = "") -> None:
    deadline = time.monotonic() + seconds
    attempted = False
    login_seen_at = None
    stored_checked = False
    stored = None
    pending_save = None
    while time.monotonic() < deadline:
        if is_home(driver):
            if pending_save:
                try:
                    save_credentials(*pending_save)
                    print("登录成功，账号密码已加密保存在本机。")
                except CredentialStoreError as exc:
                    print(f"登录成功，但{exc}")
            return
        url = driver.current_url
        if not attempted and urlparse(url).hostname == "sso.cup.edu.cn":
            if login_seen_at is None:
                login_seen_at = time.monotonic()
            if find_login_fields(driver):
                if not stored_checked:
                    try:
                        stored = load_credentials()
                    except CredentialStoreError as exc:
                        print(exc)
                    stored_checked = True
                if stored:
                    username, password = stored
                    print("正在读取本机保存的账号密码登录。")
                else:
                    username, password = ask_credentials(initial_username)
                    pending_save = (username, password)
                    deadline = time.monotonic() + seconds
                if submit_login(driver, username, password):
                    print("已提交登录信息；如出现验证码，请在浏览器中完成。")
                    attempted = True
                    password = ""
                else:
                    print("登录页已变化，请在浏览器中手动完成登录。")
                    attempted = True
            elif time.monotonic() - login_seen_at > 20:
                print("未找到账号密码输入框，请在浏览器中完成登录。")
                attempted = True
        time.sleep(0.5)
    raise WaterCodeError("等待登录超时。请检查浏览器中的登录结果后重新运行。")


def visible_text_elements(driver, label: str, exact: bool = True):
    # 只根据页面可见文字定位，避开站点构建后会变化的 CSS 类名。
    return driver.execute_script(
        """
        const [label, exact] = arguments;
        return [...document.querySelectorAll('body *')]
          .filter(el => {
            if (!el.getClientRects().length) return false;
            const value = (el.innerText || '').trim();
            return exact ? value === label : value.includes(label) && value.length < 40;
          })
          .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length)
          .slice(0, 8);
        """,
        label,
        exact,
    )


def click_label(driver, labels: tuple[str, ...], seconds: int = 15) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for exact in (True, False):
            for label in labels:
                for element in visible_text_elements(driver, label, exact):
                    try:
                        target = driver.execute_script(
                            "return arguments[0].closest('a,button,[role=button]') || arguments[0]",
                            element,
                        )
                        try:
                            target.click()
                        except (ElementClickInterceptedException, ElementNotInteractableException):
                            driver.execute_script("arguments[0].click()", target)
                        return label
                    except WebDriverException:
                        continue
        time.sleep(0.4)
    raise WaterCodeError("找不到可点击的“{}”。学校页面可能已改版。".format("/".join(labels)))


def is_water_page(driver) -> bool:
    url = driver.current_url
    if "/water-code/" in url:
        return True
    text = page_text(driver)
    return "我的用水码" in text and ("点击获取" in text or "重新获取" in text or "可用" in text)


def wait_for_water_page(driver, seconds: int = 35) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for handle in driver.window_handles:
            try:
                driver.switch_to.window(handle)
                if is_water_page(driver):
                    return
            except (WebDriverException, NoSuchWindowException):
                continue
        if "404 Not Found" in page_text(driver):
            raise WaterCodeError("水码页返回 404。请从校园卡首页重新进入。")
        time.sleep(0.5)
    raise WaterCodeError("点击“用水码”后未进入水码页；请检查浏览器中的提示。")


def extract_code(text: str) -> str | None:
    matches = list(CODE_RE.finditer(text))
    if not matches:
        return None
    # 页面说明中的“可用 N 次”通常紧邻水码；优先取它后面的数字。
    for marker in re.finditer(r"可用\s*\d+\s*次", text):
        nearby = [m for m in matches if marker.end() <= m.start() <= marker.end() + 180]
        if nearby:
            m = nearby[0]
            return m.group(1) + m.group(2)
    if len(matches) == 1:
        m = matches[0]
        return m.group(1) + m.group(2)
    raise WaterCodeError("页面上存在多个八位数字，无法可靠判断哪个是水码。")


def wait_for_code(driver, previous: str | None, seconds: int = 35) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        code = extract_code(page_text(driver))
        if code and code != previous:
            return code
        time.sleep(0.5)
    if previous:
        raise WaterCodeError("点击获取后页面仍显示旧码，未确认新码生成；请在浏览器中核实。")
    raise WaterCodeError("等待水码超时；请查看浏览器中的报错或稍后重试。")


def run(args) -> str:
    driver = None
    last_error = None
    browsers = ("chrome", "edge") if args.browser == "auto" else (args.browser,)
    for browser in browsers:
        try:
            driver = make_driver(browser, args.compat)
            driver.set_page_load_timeout(45)
            driver.get(HOME)
            break
        except WebDriverException as exc:
            last_error = exc
            if driver is not None:
                try:
                    driver.quit()
                except WebDriverException:
                    pass
            driver = None
            if args.browser == "auto" and browser != browsers[-1]:
                print(f"{browser} 无法打开页面，正在尝试其他浏览器。")
    if driver is None:
        raise WaterCodeError(
            "浏览器启动或打开页面失败。请关闭占用 profile 的浏览器；首次运行还需联网安装匹配的驱动。"
        ) from last_error
    try:
        wait_for_home(driver, args.login_timeout, args.username)
        if args.login_only:
            print("登录成功，状态已保存在 profile 文件夹。")
            return ""

        click_label(driver, ("用水码",), seconds=20)
        wait_for_water_page(driver)

        text = page_text(driver)
        existing = extract_code(text)
        expired = bool(re.search(r"已过期|已失效|可用\s*0\s*次", text))
        if existing and not args.refresh and not expired:
            return existing

        if existing:
            click_label(driver, ("重新获取",), seconds=15)
        else:
            click_label(driver, ("点击获取", "获取水码", "重新获取"), seconds=15)
        return wait_for_code(driver, existing)
    except TimeoutException as exc:
        raise WaterCodeError("页面加载超时；请检查网络并重试。") from exc
    except WebDriverException as exc:
        raise WaterCodeError("浏览器操作失败；请检查页面或网络后重试。") from exc
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="通过学校校园卡网页获取用水码")
    parser.add_argument("--browser", choices=("auto", "edge", "chrome"), default="auto", help="使用的浏览器，默认先试 Chrome 再试 Edge")
    parser.add_argument("--refresh", action="store_true", help="已有水码时重新生成，会使旧码失效")
    parser.add_argument("--login-only", action="store_true", help="只完成登录，不获取水码")
    parser.add_argument("--compat", action="store_true", help="浏览器在受限环境中启动失败时使用兼容模式")
    parser.add_argument("--username", default="", help="预填学号 / 账号；密码运行时输入")
    parser.add_argument("--login-timeout", type=int, default=300, help="等待登录的秒数，默认 300")
    credentials_group = parser.add_mutually_exclusive_group()
    credentials_group.add_argument("--update-credentials", action="store_true", help="重新输入并加密保存账号密码")
    credentials_group.add_argument("--forget-credentials", action="store_true", help="删除本机保存的账号密码")
    args = parser.parse_args()
    if args.login_timeout < 10:
        parser.error("--login-timeout 必须至少为 10 秒")
    if args.forget_credentials:
        CREDENTIAL_FILE.unlink(missing_ok=True)
        print("本机保存的账号密码已删除。")
        return 0
    if args.update_credentials:
        username, password = ask_credentials(args.username)
        try:
            save_credentials(username, password)
        except CredentialStoreError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            return 1
        print("账号密码已加密保存在本机；下次需要登录时会自动读取。")
        return 0
    try:
        code = run(args)
    except WaterCodeError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    if code:
        print(f"用水码：{code[:4]} {code[4:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
