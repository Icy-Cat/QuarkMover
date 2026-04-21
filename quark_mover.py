#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""夸克转存助手 - 本地 Web 服务（同步版）

功能：
  - 粘贴推文 → DeepSeek 二创文案 + 夸克自动转存 → 输出新推文
  - 首次使用在网页内配置 DeepSeek Key
  - 扫码登录夸克：headless Chrome 抓 QR → 网页展示 → 后台轮询登录态
  - 分步进度反馈（Job Manager）
  - 端口自动让位、日志（loguru）、Chrome 路径智能查找
"""
import json
import random
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from accounts import AccountStore
from login_worker import QuarkLoginWorker
from utils import app_root, pick_free_port, setup_logger
from version import APP_VERSION

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

# ============ Paths & config ============
ROOT = app_root()
CONFIG_PATH = ROOT / "config" / "config.json"
COOKIES_PATH = ROOT / "config" / "cookies.txt"
LOG_DIR = ROOT / "logs"

setup_logger(LOG_DIR)

DEFAULT_CONFIG: Dict[str, Any] = {
    "deepseek_api_key": "",
    "deepseek_model": "deepseek-chat",
    "port": 7788,
    "share_use_passcode": False,
    "share_expired_type": 1,
    "rewrite_prompt": (
        "你是一位资深推文写手。请对下面的推文进行二次创作：保持原帖的风格、语气和核心信息，"
        "改写措辞和句式，使其读起来像一条全新的原创推文。不要包含任何网盘链接、URL 或提取码。"
        "直接输出改写后的推文正文，不要加解释、标题或引号。\n\n原文：\n{text}"
    ),
}

import string as _string

def _random_passcode(length: int = 4) -> str:
    chars = _string.ascii_letters + _string.digits
    return "".join(random.choices(chars, k=length))


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


CONFIG = load_config()
CONFIG_LOCK = threading.Lock()


def get_cfg(key: str, default: Any = None) -> Any:
    with CONFIG_LOCK:
        return CONFIG.get(key, default)


def update_cfg(patch: Dict[str, Any]) -> None:
    with CONFIG_LOCK:
        CONFIG.update(patch)
        save_config(CONFIG)


# ============ Quark API (sync httpx) ============
QUARK_REQUIRED = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
QUARK_HEADERS_BASE = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "origin": "https://pan.quark.cn",
    "referer": "https://pan.quark.cn/",
    "content-type": "application/json;charset=UTF-8",
    "accept": "application/json, text/plain, */*",
}


def load_cookie() -> str:
    if not COOKIES_PATH.exists():
        return ""
    with open(COOKIES_PATH, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    return lines[0] if lines else ""


def quark_headers() -> Dict[str, str]:
    h = dict(QUARK_HEADERS_BASE)
    ck = load_cookie()
    if ck:
        h["cookie"] = ck
    return h


def quark_params(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p: Dict[str, Any] = dict(QUARK_REQUIRED)
    if extra:
        p.update(extra)
    p["__dt"] = random.randint(100, 9999)
    p["__t"] = int(time.time() * 1000)
    return p


class QuarkError(Exception):
    pass


class QuarkAuthError(QuarkError):
    pass


# 把夸克后端英文错误码翻译成用户看得懂的中文提示；命中时 QuarkError.message 用中文
QUARK_FRIENDLY_ERRORS = [
    ("user not real name", "当前账号未完成实名认证，无法创建分享。请打开夸克 App → 我的 → 设置 → 账号与安全 → 实名认证，完成后再试。"),
    ("capacity limit", "网盘容量不足，请先清理夸克网盘空间后再试。"),
    ("risk control", "当前账号被风控，请稍后再试或更换账号。"),
    ("share frequency", "分享操作过于频繁，请等待几分钟后再试。"),
]


def _friendly_quark_msg(raw_msg: str) -> Optional[str]:
    low = raw_msg.lower()
    for key, friendly in QUARK_FRIENDLY_ERRORS:
        if key in low:
            return friendly
    return None


def _check(data: Dict[str, Any], op: str) -> Dict[str, Any]:
    if data.get("status") != 200:
        code = data.get("code")
        msg = data.get("message") or data
        if code == 31001 or "login" in str(msg).lower():
            raise QuarkAuthError(f"{op} 失败：未登录或登录态过期")
        friendly = _friendly_quark_msg(str(msg))
        if friendly:
            raise QuarkError(f"{op} 失败：{friendly}")
        raise QuarkError(f"{op} 失败: {msg}")
    return data.get("data") or {}


def quark_is_logged_in(client: httpx.Client) -> bool:
    try:
        r = client.get(
            "https://drive-pc.quark.cn/1/clouddrive/config",
            headers=quark_headers(),
            params=quark_params(),
            timeout=8,
        )
        return r.json().get("status") == 200
    except Exception:
        return False


def quark_account_info(client: httpx.Client) -> Dict[str, Any]:
    """返回 {logged_in, account_hint} — 若已登录，从账户接口提取昵称/手机号等标识。"""
    try:
        # 先确认登录态
        r = client.get(
            "https://drive-pc.quark.cn/1/clouddrive/config",
            headers=quark_headers(), params=quark_params(), timeout=8,
        )
        if r.json().get("status") != 200:
            return {"logged_in": False, "account_hint": ""}
    except Exception:
        return {"logged_in": False, "account_hint": ""}

    def _clean(s: Any) -> str:
        if not isinstance(s, str) or not s:
            return ""
        # 剔除无法编码为 UTF-8 的代理字符（实测 /account/info 偶发返回这种脏数据）
        try:
            s.encode("utf-8")
        except UnicodeEncodeError:
            return ""
        return s.strip()

    hint = ""
    # 尝试拉账户信息（多个候选端点，命中就用）
    endpoints = [
        ("https://pan.quark.cn/account/info", {"fr": "pc", "platform": "pc"}),
        ("https://drive-pc.quark.cn/1/clouddrive/member", quark_params()),
    ]
    for url, params in endpoints:
        try:
            r = client.get(url, headers=quark_headers(), params=params, timeout=6)
            j = r.json()
            data = j.get("data") or j  # /account/info 与 /member 结构不同
            if not isinstance(data, dict):
                continue
            nickname = _clean(data.get("nickname") or data.get("username") or "")
            mobile = _clean(data.get("mobile") or data.get("phone") or "")
            if nickname and mobile:
                hint = f"{nickname}（{mobile}）"
            elif nickname:
                hint = nickname
            elif mobile:
                hint = mobile
            if hint:
                break
        except Exception:
            continue
    if not hint:
        hint = "已登录"
    return {"logged_in": True, "account_hint": hint}


def q_get_stoken(client: httpx.Client, pwd_id: str, passcode: str = "") -> str:
    r = client.post(
        "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token",
        headers=quark_headers(),
        params=quark_params(),
        json={"pwd_id": pwd_id, "passcode": passcode or ""},
        timeout=30,
    )
    raw = r.json()
    if raw.get("status") != 200:
        msg = str(raw.get("message") or raw)
        if "提取码" in msg or "passcode" in msg.lower() or raw.get("code") in (41009, 41008):
            raise QuarkError(f"获取 stoken 失败: 需要提取码（未提供或错误）")
        raise QuarkError(f"获取 stoken 失败: {msg}")
    data = raw.get("data") or {}
    stoken = data.get("stoken")
    if not stoken:
        raise QuarkError("stoken 为空")
    return stoken


def q_list_share_files(client: httpx.Client, pwd_id: str, stoken: str) -> List[Dict[str, Any]]:
    r = client.get(
        "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail",
        headers=quark_headers(),
        params=quark_params({
            "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": "0", "force": "0",
            "_page": 1, "_size": 50, "_fetch_banner": 0, "_fetch_share": 0,
            "_fetch_total": 1, "_sort": "file_type:asc,updated_at:desc",
        }),
        timeout=30,
    )
    data = _check(r.json(), "获取分享文件列表")
    return data.get("list") or []


def q_save_files(client: httpx.Client, pwd_id: str, stoken: str,
                 fid_list: List[str], fid_token_list: List[str]) -> str:
    r = client.post(
        "https://drive.quark.cn/1/clouddrive/share/sharepage/save",
        headers=quark_headers(),
        params=quark_params(),
        json={
            "fid_list": fid_list, "fid_token_list": fid_token_list,
            "to_pdir_fid": "0", "pwd_id": pwd_id, "stoken": stoken,
            "pdir_fid": "0", "scene": "link",
        },
        timeout=30,
    )
    data = _check(r.json(), "转存")
    task_id = data.get("task_id")
    if not task_id:
        raise QuarkError("转存返回无 task_id")
    return task_id


def q_wait_task(client: httpx.Client, task_id: str, progress_cb=None) -> Dict[str, Any]:
    for i in range(60):
        r = client.get(
            "https://drive-pc.quark.cn/1/clouddrive/task",
            headers=quark_headers(),
            params=quark_params({"task_id": task_id, "retry_index": i}),
            timeout=30,
        )
        data = _check(r.json(), "任务轮询")
        if data.get("status") == 2:
            return data
        if progress_cb:
            progress_cb(i)
        time.sleep(0.8)
    raise QuarkError("转存任务超时")


def q_find_recent_files(client: httpx.Client, names: List[str]) -> List[str]:
    r = client.get(
        "https://drive-pc.quark.cn/1/clouddrive/file/sort",
        headers=quark_headers(),
        params=quark_params({
            "pdir_fid": "0", "_page": 1, "_size": 100,
            "_fetch_total": 0, "_fetch_sub_dirs": 0,
            "_sort": "file_type:asc,updated_at:desc",
        }),
        timeout=30,
    )
    data = _check(r.json(), "查找文件")
    items = data.get("list") or []
    name_set = set(names)
    fids: List[str] = []
    for it in items:
        if it.get("file_name") in name_set:
            fids.append(it["fid"])
            name_set.discard(it["file_name"])
        if not name_set:
            break
    return fids


def q_create_share(client: httpx.Client, fid_list: List[str], title: str) -> Tuple[str, str]:
    """返回 (share_id, passcode)。passcode 为空字符串表示无口令。"""
    use_passcode = get_cfg("share_use_passcode", False)
    expired_type = int(get_cfg("share_expired_type", 1))
    passcode = _random_passcode() if use_passcode else ""
    url_type = 2 if passcode else 1

    body: Dict[str, Any] = {
        "fid_list": fid_list,
        "title": title,
        "url_type": url_type,
        "expired_type": expired_type,
    }
    if passcode:
        body["passcode"] = passcode

    r = client.post(
        "https://drive-pc.quark.cn/1/clouddrive/share",
        headers=quark_headers(),
        params=quark_params(),
        json=body,
        timeout=30,
    )
    data = _check(r.json(), "创建分享")
    task_id = data.get("task_id")
    if not task_id:
        raise QuarkError("创建分享返回无 task_id")
    for i in range(60):
        r2 = client.get(
            "https://drive-pc.quark.cn/1/clouddrive/task",
            headers=quark_headers(),
            params=quark_params({"task_id": task_id, "retry_index": i}),
            timeout=30,
        )
        d2 = _check(r2.json(), "分享任务轮询")
        if d2.get("status") == 2:
            share_id = d2.get("share_id")
            if not share_id:
                raise QuarkError("分享完成但无 share_id")
            return share_id, passcode
        time.sleep(0.6)
    raise QuarkError("创建分享超时")


def q_get_share_url(client: httpx.Client, share_id: str) -> str:
    r = client.post(
        "https://drive-pc.quark.cn/1/clouddrive/share/password",
        headers=quark_headers(),
        params=quark_params(),
        json={"share_id": share_id},
        timeout=30,
    )
    data = _check(r.json(), "获取分享链接")
    share_url = data.get("share_url")
    if not share_url:
        raise QuarkError("分享链接为空")
    return share_url


# ============ DeepSeek ============
def deepseek_rewrite(client: httpx.Client, text: str) -> str:
    if not text.strip():
        return ""
    key = get_cfg("deepseek_api_key", "")
    if not key or key.startswith("sk-REPLACE"):
        raise RuntimeError("未配置 DeepSeek API Key")
    prompt = get_cfg("rewrite_prompt", DEFAULT_CONFIG["rewrite_prompt"]).format(text=text)
    r = client.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": get_cfg("deepseek_model", "deepseek-chat"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.8,
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


# ============ Pipeline ============
# 链接正则：
#   - https?:// 前缀可选（很多人复制小红书/微信时只保留裸域名）
#   - 也容忍 s/ 后面紧跟中文（如 "pan.quark.cn/s/abc提取码:xxx"）
SHARE_RE = re.compile(r"(?:https?://)?pan\.quark\.cn/s/([A-Za-z0-9]+)(?:-pwd([A-Za-z0-9]+))?")
PASSCODE_RE = re.compile(
    r"(?:提取码|访问码|密码|pwd|passcode|code)\s*[:：=]?\s*([A-Za-z0-9]{4,8})",
    re.IGNORECASE,
)


def extract_share(text: str) -> Tuple[Optional[str], str, str]:
    m = SHARE_RE.search(text)
    if not m:
        return None, "", text
    pwd_id = m.group(1)
    passcode = m.group(2) or ""
    cleaned = SHARE_RE.sub("", text)
    if not passcode:
        pm = PASSCODE_RE.search(cleaned)
        if pm:
            passcode = pm.group(1)
            cleaned = cleaned[:pm.start()] + cleaned[pm.end():]
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return pwd_id, passcode, cleaned


def run_pipeline(job: "Job", raw_text: str, mode: str = "full") -> None:
    """mode: 'full' = DeepSeek 二创 + 夸克转存；'reshare_only' = 只转存不二创"""
    try:
        pwd_id, src_passcode, text_no_link = extract_share(raw_text)
        logger.info(f"[job {job.id}] start mode={mode} has_link={bool(pwd_id)} has_passcode={bool(src_passcode)} text_len={len(text_no_link)}")

        rewritten = ""
        rewrite_err = ""
        new_link = ""
        share_passcode = ""
        quark_err = ""
        files_info: List[str] = []
        share_title = ""

        with httpx.Client() as client:
            # --- DeepSeek rewrite (full mode only, skip gracefully if no key) ---
            if mode == "full" and text_no_link.strip():
                if _has_key():
                    job.update(step="调用 DeepSeek 二创文案")
                    try:
                        rewritten = deepseek_rewrite(client, text_no_link)
                    except Exception as e:
                        rewrite_err = f"{type(e).__name__}: {e}"
                        logger.warning(f"[job {job.id}] deepseek failed: {e}")
                else:
                    rewrite_err = "未配置 DeepSeek Key，已跳过二创"
                    logger.info(f"[job {job.id}] no deepseek key, skip rewrite")

            # --- Quark reshare ---
            if pwd_id:
                try:
                    job.update(step="获取分享 stoken")
                    stoken = q_get_stoken(client, pwd_id, src_passcode)

                    job.update(step="读取分享文件列表")
                    files = q_list_share_files(client, pwd_id, stoken)
                    if not files:
                        raise QuarkError("分享中没有文件")
                    fid_list = [f["fid"] for f in files]
                    fid_token_list = [f["share_fid_token"] for f in files]
                    names = [f["file_name"] for f in files]
                    title = files[0]["file_name"]
                    share_title = title
                    files_info = [f.get("file_name", "") for f in files]

                    job.update(step=f"转存 {len(files)} 个文件到我的网盘")
                    task_id = q_save_files(client, pwd_id, stoken, fid_list, fid_token_list)

                    job.update(step="等待转存完成")
                    q_wait_task(client, task_id, progress_cb=lambda i: job.update(sub=f"轮询 {i+1}/60"))
                    time.sleep(0.5)

                    job.update(step="在网盘中定位新文件", sub=None)
                    new_fids = q_find_recent_files(client, names)
                    if not new_fids:
                        raise QuarkError("转存后未找到新文件")

                    job.update(step="创建新的分享链接")
                    sid, share_passcode = q_create_share(client, new_fids, title)

                    job.update(step="获取分享 URL")
                    new_link = q_get_share_url(client, sid)
                    logger.info(f"[job {job.id}] new_link={new_link}")
                except QuarkAuthError:
                    quark_err = "登录态过期，请点击右上角重新登录"
                    logger.warning(f"[job {job.id}] quark auth expired")
                except Exception as e:
                    quark_err = f"{type(e).__name__}: {e}"
                    logger.exception(f"[job {job.id}] quark failed")
            elif mode == "full":
                # full 模式但没链接，也不算错误
                pass
            elif mode == "reshare_only":
                quark_err = "未检测到夸克分享链接"

        # 构造夸克原生分享格式
        quark_block = ""
        if new_link:
            title_for_tpl = share_title or "分享"
            link_with_pwd = new_link
            if share_passcode:
                link_with_pwd = f"{new_link}?pwd={share_passcode}"
            lines = [
                f"我用夸克网盘给你分享了「{title_for_tpl}」，"
                "点击链接或复制整段内容，打开「夸克APP」即可获取。",
                f"链接：{link_with_pwd}",
            ]
            if share_passcode:
                lines.insert(1, f"提取码：{share_passcode}")
            quark_block = "\n".join(lines)

        parts: List[str] = []
        if rewritten:
            parts.append(rewritten)
        if quark_block:
            if parts:
                parts.append("")
            parts.append(quark_block)
        output = "\n".join(parts).strip()

        job.finish({
            "rewritten": rewritten,
            "rewrite_error": rewrite_err,
            "new_link": new_link,
            "share_passcode": share_passcode,
            "share_title": share_title,
            "quark_block": quark_block,
            "output": output,
            "had_link": bool(pwd_id),
            "quark_error": quark_err,
            "files": files_info,
            "mode": mode,
        })
    except Exception as e:
        logger.exception(f"[job {job.id}] pipeline failed")
        job.fail(f"{type(e).__name__}: {e}")


# ============ Job Manager ============
class Job:
    def __init__(self, jid: str):
        self.id = jid
        self.status = "running"  # running | done | failed
        self.step = "准备中"
        self.sub: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self._lock = threading.Lock()

    def update(self, step: Optional[str] = None, sub: Optional[str] = "__keep__") -> None:
        with self._lock:
            if step is not None:
                self.step = step
                logger.info(f"[job {self.id}] {step}")
            if sub != "__keep__":
                self.sub = sub

    def finish(self, result: Dict[str, Any]) -> None:
        with self._lock:
            self.status = "done"
            self.result = result
            self.step = "完成"
            self.sub = None

    def fail(self, error: str) -> None:
        with self._lock:
            self.status = "failed"
            self.error = error
            self.step = "失败"
            self.sub = None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "step": self.step,
                "sub": self.sub,
                "result": self.result,
                "error": self.error,
            }


class JobManager:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def create(self) -> Job:
        with self._lock:
            self._counter += 1
            jid = str(self._counter)
            job = Job(jid)
            self._jobs[jid] = job
            # opportunistic cleanup
            if len(self._jobs) > 50:
                old = sorted(self._jobs.items(), key=lambda kv: kv[1].created_at)[:10]
                for k, _ in old:
                    self._jobs.pop(k, None)
            return job

    def get(self, jid: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(jid)


JOB_MANAGER = JobManager()
ACCOUNTS = AccountStore(ROOT / "config" / "accounts.json", COOKIES_PATH)


def _on_login_success(cookie_str: str) -> None:
    """登录 worker 登录成功后的回调：查询账号信息并写入 accounts store。"""
    hint = "已登录"
    try:
        with httpx.Client() as client:
            info = quark_account_info(client)
        if info.get("account_hint"):
            hint = info["account_hint"]
    except Exception as e:
        logger.debug(f"fetch account hint failed: {e}")
    try:
        ACCOUNTS.upsert(cookie_str, hint=hint)
    except Exception as e:
        logger.warning(f"记账号到 accounts store 失败: {e}")
    _invalidate_login_cache()


LOGIN_WORKER = QuarkLoginWorker(COOKIES_PATH, on_success=_on_login_success)


# ============ State helpers ============
def _has_key() -> bool:
    k = get_cfg("deepseek_api_key", "")
    return bool(k) and not k.startswith("sk-REPLACE")


_LOGIN_CACHE = {"ts": 0.0, "ok": False}


def _is_logged_in_cached() -> bool:
    now = time.time()
    if now - _LOGIN_CACHE["ts"] < 30:
        return _LOGIN_CACHE["ok"]
    with httpx.Client() as client:
        ok = quark_is_logged_in(client) if load_cookie() else False
    _LOGIN_CACHE["ts"] = now
    _LOGIN_CACHE["ok"] = ok
    return ok


def _invalidate_login_cache() -> None:
    _LOGIN_CACHE["ts"] = 0.0


# ============ HTTP Server ============
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>夸克转存助手</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Instrument+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root {
  /* ---- dark (默认) ---- */
  --bg-0: #0B0C0E;
  --bg-1: #141619;
  --bg-2: #1B1E23;
  --bg-3: #23272E;
  --line: #2A2E36;
  --line-strong: #363B44;
  --text-0: #EAECEF;
  --text-1: #9BA1AB;
  --text-2: #5B616D;
  --text-3: #3E434C;
  --accent: #E8B568;
  --accent-hot: #F1C57C;
  --accent-dim: rgba(232,181,104,0.14);
  --accent-line: rgba(232,181,104,0.28);
  --accent-fg: #1A1208;
  --ok: #7BB88A;
  --warn: #D4A44C;
  --err: #D96B5E;
  --modal-backdrop: rgba(11,12,14,0.78);
  --header-bg: rgba(11,12,14,0.7);
  --body-glow-1: rgba(232,181,104,0.04);
  --body-glow-2: rgba(232,181,104,0.03);
  --mono: 'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace;
  --sans: 'Instrument Sans', -apple-system, 'Microsoft YaHei', sans-serif;
}

/* 跟随系统偏好：系统为浅色时切到浅色主题 */
@media (prefers-color-scheme: light) {
  :root:not([data-theme]) {
    --bg-0: #F7F4EC;
    --bg-1: #FFFFFF;
    --bg-2: #F0ECDE;
    --bg-3: #E6E0CC;
    --line: #DDD6C2;
    --line-strong: #C5BCA3;
    --text-0: #1A1613;
    --text-1: #5D564A;
    --text-2: #8B8374;
    --text-3: #B5AE9C;
    --accent: #A0651C;
    --accent-hot: #B8731F;
    --accent-dim: rgba(160,101,28,0.10);
    --accent-line: rgba(160,101,28,0.32);
    --accent-fg: #FFF7E8;
    --ok: #2E7D3D;
    --warn: #B07A08;
    --err: #B8352A;
    --modal-backdrop: rgba(26,22,19,0.35);
    --header-bg: rgba(247,244,236,0.82);
    --body-glow-1: rgba(160,101,28,0.05);
    --body-glow-2: rgba(160,101,28,0.04);
  }
}

/* 用户显式选择：浅色 */
:root[data-theme="light"] {
  --bg-0: #F7F4EC;
  --bg-1: #FFFFFF;
  --bg-2: #F0ECDE;
  --bg-3: #E6E0CC;
  --line: #DDD6C2;
  --line-strong: #C5BCA3;
  --text-0: #1A1613;
  --text-1: #5D564A;
  --text-2: #8B8374;
  --text-3: #B5AE9C;
  --accent: #A0651C;
  --accent-hot: #B8731F;
  --accent-dim: rgba(160,101,28,0.10);
  --accent-line: rgba(160,101,28,0.32);
  --accent-fg: #FFF7E8;
  --ok: #2E7D3D;
  --warn: #B07A08;
  --err: #B8352A;
  --modal-backdrop: rgba(26,22,19,0.35);
  --header-bg: rgba(247,244,236,0.82);
  --body-glow-1: rgba(160,101,28,0.05);
  --body-glow-2: rgba(160,101,28,0.04);
}

/* 用户显式选择：深色（即便系统是浅色也强制） */
:root[data-theme="dark"] {
  --bg-0: #0B0C0E;
  --bg-1: #141619;
  --bg-2: #1B1E23;
  --bg-3: #23272E;
  --line: #2A2E36;
  --line-strong: #363B44;
  --text-0: #EAECEF;
  --text-1: #9BA1AB;
  --text-2: #5B616D;
  --text-3: #3E434C;
  --accent: #E8B568;
  --accent-hot: #F1C57C;
  --accent-dim: rgba(232,181,104,0.14);
  --accent-line: rgba(232,181,104,0.28);
  --accent-fg: #1A1208;
  --ok: #7BB88A;
  --warn: #D4A44C;
  --err: #D96B5E;
  --modal-backdrop: rgba(11,12,14,0.78);
  --header-bg: rgba(11,12,14,0.7);
  --body-glow-1: rgba(232,181,104,0.04);
  --body-glow-2: rgba(232,181,104,0.03);
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg-0);
  color: var(--text-0);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.6;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  -webkit-font-smoothing: antialiased;
  background-image:
    radial-gradient(ellipse 800px 400px at 20% -10%, var(--body-glow-1), transparent 60%),
    radial-gradient(ellipse 600px 300px at 80% 110%, var(--body-glow-2), transparent 60%);
  transition: background-color 0.25s, color 0.25s;
}

/* ============ Header ============ */
header {
  border-bottom: 1px solid var(--line);
  padding: 14px 28px;
  display: flex;
  align-items: center;
  gap: 24px;
  background: var(--header-bg);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 10;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}
.brand-mark {
  width: 22px; height: 22px;
  border: 1.5px solid var(--accent);
  position: relative;
  transform: rotate(45deg);
}
.brand-mark::before {
  content: '';
  position: absolute;
  inset: 3px;
  background: var(--accent);
}
.brand-name {
  font-family: var(--mono);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-0);
}
.brand-sub {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-2);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  margin-left: 4px;
}

.status-rail {
  flex: 1;
  display: flex;
  gap: 18px;
  justify-content: center;
}
.stat {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.06em;
  color: var(--text-1);
  text-transform: uppercase;
}
.stat-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-3);
  box-shadow: 0 0 0 2px var(--bg-0);
  transition: all 0.3s;
}
.stat.ok .stat-dot { background: var(--ok); box-shadow: 0 0 0 2px var(--bg-0), 0 0 10px rgba(123,184,138,0.5); }
.stat.warn .stat-dot { background: var(--warn); box-shadow: 0 0 0 2px var(--bg-0), 0 0 10px rgba(212,164,76,0.5); }
.stat.err .stat-dot { background: var(--err); box-shadow: 0 0 0 2px var(--bg-0); }
.stat-label { color: var(--text-2); }
.stat-val { color: var(--text-0); }
.stat.ok .stat-val { color: var(--ok); }
.stat.warn .stat-val { color: var(--warn); }
.stat.err .stat-val { color: var(--err); }

.header-actions { display: flex; gap: 8px; }
.header-actions button.logged-in {
  background: transparent;
  color: var(--text-2);
  border-color: var(--line);
}
.header-actions button.logged-in:hover {
  color: var(--text-0);
  border-color: var(--line-strong);
}
.header-actions button.need-login {
  background: var(--accent-dim);
  color: var(--accent);
  border-color: var(--accent-line);
}
.header-actions button.need-login:hover {
  background: var(--accent);
  color: var(--accent-fg);
}

/* ============ Main ============ */
main {
  max-width: 1400px;
  margin: 0 auto;
  padding: 28px;
  width: 100%;
  flex: 1;
}

/* Mode switch (segmented) */
.mode-switch {
  display: inline-flex;
  background: var(--bg-1);
  border: 1px solid var(--line);
  padding: 4px;
  border-radius: 8px;
  margin-bottom: 22px;
  position: relative;
}
.mode-btn {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  background: transparent;
  color: var(--text-1);
  border: none;
  padding: 9px 20px;
  cursor: pointer;
  border-radius: 5px;
  transition: all 0.2s;
  position: relative;
}
.mode-btn:hover { color: var(--text-0); }
.mode-btn.active {
  background: var(--bg-3);
  color: var(--accent);
  box-shadow: inset 0 0 0 1px var(--accent-line), 0 0 16px rgba(232,181,104,0.1);
}

/* ============ Panels ============ */
.workspace {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}
.panel {
  background: var(--bg-1);
  border: 1px solid var(--line);
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}
.panel-head {
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  gap: 10px;
}
.panel-num {
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.1em;
  color: var(--accent);
  background: var(--accent-dim);
  padding: 3px 8px;
  border-radius: 3px;
}
.panel-title {
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-0);
}
.panel-sub {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-2);
  letter-spacing: 0.06em;
  margin-left: auto;
}
.panel-body {
  padding: 0;
  display: flex;
  flex-direction: column;
  flex: 1;
}

textarea {
  width: 100%;
  flex: 1;
  min-height: 380px;
  background: transparent;
  border: none;
  padding: 18px 22px;
  font-family: var(--sans);
  font-size: 16px;
  line-height: 1.75;
  color: var(--text-0);
  resize: none;
}
textarea::placeholder { color: var(--text-3); font-style: italic; }
textarea:focus { outline: none; }

.panel-foot {
  padding: 12px 16px;
  border-top: 1px solid var(--line);
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--bg-0);
}

/* ============ Buttons ============ */
button {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 9px 18px;
  cursor: pointer;
  transition: all 0.15s;
  background: var(--bg-2);
  color: var(--text-1);
  border-color: var(--line);
}
button:hover { background: var(--bg-3); color: var(--text-0); border-color: var(--line-strong); }
button.primary {
  background: var(--accent);
  color: var(--accent-fg);
  border-color: var(--accent);
  font-weight: 600;
}
button.primary:hover { background: var(--accent-hot); box-shadow: 0 0 0 3px var(--accent-dim); }
button:disabled { opacity: 0.5; cursor: not-allowed; }
button.primary:disabled { background: var(--bg-2); color: var(--text-2); border-color: var(--line); box-shadow: none; }

.btn-icon {
  padding: 8px 10px;
  background: transparent;
  border-color: var(--line);
}

/* Status line */
.status {
  flex: 1;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--text-2);
  text-transform: uppercase;
  text-align: right;
}
.status.ok { color: var(--ok); }
.status.err { color: var(--err); }
.status.run { color: var(--accent); }

/* ============ Progress Timeline ============ */
.progress {
  margin-top: 20px;
  background: var(--bg-1);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 14px 18px;
  display: none;
}
.progress.show { display: block; }
.progress-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text-1);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.progress-spinner {
  width: 10px; height: 10px;
  border: 1.5px solid var(--accent);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  flex-shrink: 0;
}
.progress.done .progress-spinner,
.progress.failed .progress-spinner { display: none; }
.progress.done .progress-head::before {
  content: '✓';
  color: var(--ok);
  font-weight: 600;
  font-size: 12px;
  letter-spacing: 0;
}
.progress.failed .progress-head::before {
  content: '✕';
  color: var(--err);
  font-weight: 600;
  font-size: 12px;
  letter-spacing: 0;
}
.progress.done .progress-head { color: var(--ok); }
.progress.failed .progress-head { color: var(--err); }
@keyframes spin { to { transform: rotate(360deg); } }
.progress-steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-family: var(--mono);
  font-size: 12px;
  max-height: 180px;
  overflow-y: auto;
}
.step {
  display: flex;
  gap: 12px;
  color: var(--text-2);
  padding-left: 4px;
  border-left: 2px solid var(--line);
}
.step.active { color: var(--accent); border-left-color: var(--accent); padding-left: 4px; }
.step.done { color: var(--text-1); border-left-color: var(--ok); }
.step.done::before { content: '✓'; color: var(--ok); margin-right: -4px; }
.step-t { color: var(--text-3); min-width: 42px; }

/* ============ Result highlight ============ */
.new-link-box {
  margin: 12px 18px 0;
  padding: 12px 16px;
  background: var(--accent-dim);
  border: 1px solid var(--accent-line);
  border-radius: 6px;
  font-family: var(--mono);
  font-size: 13px;
  color: var(--accent-hot);
  word-break: break-all;
  display: none;
}
.new-link-box.show { display: block; }
.new-link-box-label {
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: 5px;
  opacity: 0.75;
}

/* ============ Modals ============ */
.modal-bg {
  position: fixed;
  inset: 0;
  background: var(--modal-backdrop);
  backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s;
}
.modal-bg[data-open="true"] {
  opacity: 1;
  pointer-events: auto;
}
.modal {
  background: var(--bg-1);
  border: 1px solid var(--line-strong);
  border-radius: 12px;
  padding: 28px;
  max-width: 440px;
  width: 90%;
  box-shadow: 0 20px 60px rgba(0,0,0,0.5), 0 0 0 1px rgba(232,181,104,0.06);
  transform: translateY(10px);
  transition: transform 0.25s cubic-bezier(.2,.8,.2,1);
}
.modal-bg[data-open="true"] .modal { transform: translateY(0); }
.modal h3 {
  margin: 0 0 4px;
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--accent);
}
.modal .modal-title {
  font-family: var(--sans);
  font-size: 22px;
  font-weight: 500;
  color: var(--text-0);
  margin: 0 0 14px;
  font-style: italic;
}
.modal p {
  margin: 0 0 18px;
  font-size: 14px;
  color: var(--text-1);
  line-height: 1.65;
}
.qr-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 22px;
  background: var(--bg-0);
  border: 1px solid var(--line);
  border-radius: 8px;
  margin: 16px 0;
  position: relative;
}
.qr-frame {
  width: 220px; height: 220px;
  background: #fff;
  padding: 12px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}
.qr-frame::before, .qr-frame::after {
  content: '';
  position: absolute;
  width: 20px; height: 20px;
  border: 2px solid var(--accent);
}
.qr-frame::before { top: -2px; left: -2px; border-right: none; border-bottom: none; }
.qr-frame::after { bottom: -2px; right: -2px; border-left: none; border-top: none; }
#qrImg { max-width: 100%; max-height: 100%; image-rendering: pixelated; }
.qr-empty {
  color: var(--text-3);
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.1em;
}
.qr-status {
  margin-top: 14px;
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-1);
  display: flex;
  align-items: center;
  gap: 8px;
}
.qr-status.ok { color: var(--ok); }
.qr-status.err { color: var(--err); }
.modal-actions {
  display: flex;
  gap: 10px;
  margin-top: 20px;
}
.modal-actions button { flex: 1; padding: 10px; }

.field { margin-bottom: 18px; }
.field label {
  display: block;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-2);
  margin-bottom: 7px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.field input, .field textarea {
  width: 100%;
  background: var(--bg-0);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 11px 13px;
  font-family: var(--mono);
  font-size: 14px;
  color: var(--text-0);
}
.field textarea {
  font-family: var(--sans);
  min-height: 130px;
  line-height: 1.65;
  resize: vertical;
}
.field input:focus, .field textarea:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-dim);
}

/* Reshare-only mode */
.reshare-only .panel-input textarea { min-height: 100px; }
.reshare-only .panel-input .panel-body { min-height: 140px; }

/* 底部推广 Banner */
.promo-banner {
  margin-top: auto;
  border-top: 1px solid var(--line);
  background: var(--bg-1);
}
.promo-banner-inner {
  max-width: 1400px;
  margin: 0 auto;
  padding: 20px 28px;
  display: flex;
  align-items: center;
  gap: 24px;
}
.promo-banner-text { flex: 1; }
.promo-banner-title {
  font-family: var(--sans);
  font-size: 16px;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 4px;
}
.promo-banner-desc {
  font-size: 13px;
  color: var(--text-1);
}
.promo-banner button { flex-shrink: 0; }
@media (max-width: 700px) {
  .promo-banner-inner { flex-direction: column; text-align: center; }
}

/* 推广弹窗 */
.promo-qrs {
  display: flex;
  gap: 28px;
  justify-content: center;
  margin: 20px 0;
}
.promo-qr {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.promo-qr img {
  width: 220px;
  height: 220px;
  border-radius: 10px;
  background: #fff;
  padding: 6px;
  object-fit: contain;
  border: 1px solid var(--line);
}
.promo-qr span {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-1);
  letter-spacing: 0.08em;
}

/* Responsive */
@media (max-width: 900px) {
  .workspace { grid-template-columns: 1fr; }
  .status-rail { display: none; }
  main { padding: 16px; }
  header { padding: 12px 16px; }
}
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="brand-mark"></div>
    <div>
      <span class="brand-name">夸克转存助手</span>
      <span class="brand-sub">/ v__APP_VERSION__</span>
    </div>
  </div>
  <div class="status-rail">
    <div class="stat" id="statQuark"><span class="stat-dot"></span><span class="stat-label">夸克</span><span class="stat-val">检查中</span></div>
    <div class="stat" id="statDeepseek"><span class="stat-dot"></span><span class="stat-label">DeepSeek</span><span class="stat-val">检查中</span></div>
    <div class="stat" id="statPort"><span class="stat-dot ok"></span><span class="stat-label">端口</span><span class="stat-val">--</span></div>
  </div>
  <div class="header-actions">
    <button id="themeBtn" title="切换主题">跟随系统</button>
    <button id="loginBtn">登录夸克</button>
    <button id="settingsBtn">设置</button>
  </div>
</header>

<main>
  <div class="mode-switch">
    <button class="mode-btn" data-mode="full">完整模式 · 二创 + 转存</button>
    <button class="mode-btn active" data-mode="reshare_only">仅转存</button>
  </div>

  <div class="workspace reshare-only" id="workspace">
    <div class="panel panel-input">
      <div class="panel-head">
        <span class="panel-num">01</span>
        <span class="panel-title" id="inputTitle">夸克分享链接</span>
        <span class="panel-sub" id="inputHint">仅需粘贴 pan.quark.cn/s/xxx</span>
      </div>
      <div class="panel-body">
        <textarea id="input" placeholder="只需粘贴 pan.quark.cn/s/xxxxx 即可，不会调 DeepSeek"></textarea>
      </div>
      <div class="panel-foot">
        <button class="primary" id="go">生成</button>
        <button id="clear">清空</button>
        <span class="status" id="status"></span>
      </div>
    </div>

    <div class="panel panel-output">
      <div class="panel-head">
        <span class="panel-num">02</span>
        <span class="panel-title">输出</span>
        <span class="panel-sub" id="outputHint">新分享链接</span>
      </div>
      <div class="new-link-box" id="newLinkBox">
        <div class="new-link-box-label">新分享链接</div>
        <div id="newLinkText"></div>
      </div>
      <div class="panel-body">
        <textarea id="output" placeholder="生成后的内容会显示在这里，可手动编辑"></textarea>
      </div>
      <div class="panel-foot">
        <button class="primary" id="copy">复制</button>
        <button id="copyLink">仅复制链接</button>
        <span class="status" id="copyStatus"></span>
      </div>
    </div>
  </div>

  <div class="progress" id="progress">
    <div class="progress-head">
      <div class="progress-spinner"></div>
      <span id="progressLabel">处理中…</span>
    </div>
    <div class="progress-steps" id="progressSteps"></div>
  </div>
</main>

<!-- 底部推广 Banner -->
<div class="promo-banner">
  <div class="promo-banner-inner">
    <div class="promo-banner-text">
      <div class="promo-banner-title">想用 AI 做网盘拉新日赚 1000？</div>
      <div class="promo-banner-desc">零基础也能上手，手把手教你用 AI 工具做推特网盘拉新</div>
    </div>
    <button class="primary" id="promoBtn">加入社群 →</button>
  </div>
</div>

<!-- Login / Accounts modal -->
<div class="modal-bg" id="loginModal" data-open="false">
  <div class="modal">
    <h3>// 账号</h3>
    <div class="modal-title" id="loginModalTitle">账号管理</div>

    <!-- 账号列表视图 -->
    <div id="accountListView">
      <div id="accountList" style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px;"></div>
      <div id="accountEmpty" style="display:none;padding:24px;text-align:center;color:var(--text-1);border:1px dashed var(--line);border-radius:8px;margin-bottom:14px;">
        暂无账号，请添加
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button class="primary" id="addAccountBtn">+ 添加新账号</button>
      </div>
    </div>

    <!-- 扫码视图（添加新账号或手动导入） -->
    <div id="loginQrView" style="display:none;">
    <p>请使用夸克 App 扫描下方二维码登录新账号。后台每 3 秒检查一次，成功后自动加入账号列表。</p>
    <div class="qr-wrap">
      <div class="qr-frame">
        <img id="qrImg" alt="">
        <span class="qr-empty" id="qrEmpty">加载中…</span>
      </div>
      <div class="qr-status" id="qrStatus">正在启动浏览器…</div>
      <div class="status" id="qrMeta"></div>
    </div>
    <div style="border-top:1px solid var(--line);margin:18px 0 0;padding-top:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
        <div style="font-family:var(--mono);font-size:12px;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:var(--text-0);">扫码失败？手动导入 Cookies</div>
        <button id="toggleManualCookie" style="font-size:12px;padding:4px 10px;">展开 ▾</button>
      </div>
      <div id="manualCookieBox" style="display:none;margin-top:12px;">
        <div class="field">
          <label>夸克网盘 Cookie（整段粘贴）</label>
          <textarea id="manualCookieInput" placeholder="在浏览器登录 https://pan.quark.cn/ 后，F12 → Network → 任意 drive-pc.quark.cn 请求 → Headers → 复制 Cookie 的值粘贴到这里" style="min-height:96px;font-family:var(--mono);font-size:12px;"></textarea>
        </div>
        <div class="status" id="manualCookieStatus" style="margin-bottom:10px;">
          获取方法：① 浏览器打开 pan.quark.cn 登录 → ② 按 F12 打开开发者工具 → ③ 切换到 Network 面板 → ④ 刷新页面，点一个 drive-pc.quark.cn 的请求 → ⑤ 在右侧 Request Headers 里复制 <code>cookie</code> 字段的完整值。
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;">
          <button id="saveManualCookie" class="primary">保存并校验</button>
        </div>
      </div>
    </div>
    </div><!-- /loginQrView -->
    <div class="modal-actions">
      <button id="copyLogBtn" title="复制本次运行日志到剪贴板，便于发送给开发者排查">复制日志</button>
      <button id="saveLogBtn" title="将本次运行日志另存为文件">另存日志</button>
      <button id="backToListBtn" style="display:none;">返回列表</button>
      <button id="closeLogin">关闭</button>
    </div>
  </div>
</div>

<!-- Settings modal -->
<div class="modal-bg" id="settingsModal" data-open="false">
  <div class="modal">
    <h3>// 设置</h3>
    <div class="modal-title">配置</div>
    <div class="field">
      <label>DeepSeek API 密钥</label>
      <input type="password" id="cfgKey" placeholder="sk-…" autocomplete="off">
    </div>
    <div class="field">
      <label>改写 Prompt  ·  {text} 会被替换成原文</label>
      <textarea id="cfgPrompt"></textarea>
    </div>
    <div style="border-top: 1px solid var(--line); margin: 18px 0 14px; padding-top: 14px;">
      <div style="font-family:var(--mono);font-size:12px;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:var(--text-0);margin-bottom:14px;">分享设置</div>
    </div>
    <div class="field">
      <label>分享提取码</label>
      <div style="display:flex;gap:12px;align-items:center;">
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text-0);cursor:pointer;text-transform:none;letter-spacing:0;">
          <input type="radio" name="cfgPasscode" id="cfgPasscodeOff" value="off"> 无需提取码（公开）
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text-0);cursor:pointer;text-transform:none;letter-spacing:0;">
          <input type="radio" name="cfgPasscode" id="cfgPasscodeOn" value="on"> 随机提取码
        </label>
      </div>
    </div>
    <div class="field">
      <label>分享有效期</label>
      <select id="cfgExpired" style="width:100%;background:var(--bg-0);border:1px solid var(--line);border-radius:6px;padding:10px 12px;font-family:var(--mono);font-size:13px;color:var(--text-0);cursor:pointer;appearance:auto;">
        <option value="1">永久有效</option>
        <option value="2">1 天</option>
        <option value="3">7 天</option>
        <option value="4">30 天</option>
      </select>
    </div>
    <div style="border-top:1px solid var(--line);margin:18px 0 14px;padding-top:14px;">
      <div style="font-family:var(--mono);font-size:12px;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:var(--text-0);margin-bottom:8px;">排查 / 反馈</div>
      <div class="status" id="settingsLogStatus" style="margin-bottom:10px;">遇到扫码无法跳转、或其他问题？可将日志发送给开发者。</div>
      <div style="display:flex;gap:8px;">
        <button id="settingsCopyLog">复制日志到剪贴板</button>
        <button id="settingsSaveLog">日志另存为文件</button>
      </div>
    </div>
    <div class="modal-actions">
      <button id="cancelSettings">取消</button>
      <button class="primary" id="saveSettings">保存</button>
    </div>
  </div>
</div>

<!-- 推广弹窗 -->
<div class="modal-bg" id="promoModal" data-open="false">
  <div class="modal" style="max-width:600px;">
    <h3>// 加入社群</h3>
    <div class="modal-title">想用 AI 做网盘拉新日赚 1000？</div>
    <p>手把手教你用 AI 工具做推特网盘拉新，零基础也能上手。<br>扫码关注公众号或加入交流群，获取最新教程和工具更新。</p>
    <div class="promo-qrs">
      <div class="promo-qr">
        <img src="/static/qr_gongzhonghao.webp" alt="公众号" onerror="this.closest('.promo-qr').style.display='none'">
        <span>关注公众号</span>
      </div>
      <div class="promo-qr">
        <img src="/static/qr_group.webp" alt="交流群" onerror="this.closest('.promo-qr').style.display='none'">
        <span>加入交流群</span>
      </div>
    </div>
    <div class="modal-actions">
      <button id="closePromo">关闭</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const openModal = id => $(id).setAttribute('data-open', 'true');
const closeModal = id => $(id).setAttribute('data-open', 'false');

// ---- Theme ----
const THEME_KEY = 'quark-mover-theme';
const THEME_CYCLE = ['system', 'light', 'dark'];
const THEME_LABEL = { system: '跟随系统', light: '明亮', dark: '暗色' };
const THEME_ICON = { system: '◐', light: '☀', dark: '☾' };

function currentTheme() {
  return localStorage.getItem(THEME_KEY) || 'system';
}
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'system') {
    root.removeAttribute('data-theme');
  } else {
    root.setAttribute('data-theme', theme);
  }
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = THEME_ICON[theme] + ' ' + THEME_LABEL[theme];
}
function cycleTheme() {
  const cur = currentTheme();
  const next = THEME_CYCLE[(THEME_CYCLE.indexOf(cur) + 1) % THEME_CYCLE.length];
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
}
applyTheme(currentTheme());
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (currentTheme() === 'system') applyTheme('system');
  });
}

function setStatus(msg, cls) {
  const s = $('status');
  s.textContent = msg || '';
  s.className = 'status ' + (cls||'');
}

async function apiGet(path) {
  const r = await fetch(path);
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type':'application/json; charset=utf-8'},
    body: body ? JSON.stringify(body) : '{}'
  });
  return r.json();
}

let currentMode = 'reshare_only';
let serverPort = '';

async function refreshState() {
  try {
    const s = await apiGet('/api/state');
    const qBadge = $('statQuark');
    qBadge.querySelector('.stat-val').textContent = s.logged_in ? '已登录' : '未登录';
    qBadge.className = 'stat ' + (s.logged_in ? 'ok' : 'warn');

    const dBadge = $('statDeepseek');
    dBadge.querySelector('.stat-val').textContent = s.has_key ? '已配置' : '未配置';
    dBadge.className = 'stat ' + (s.has_key ? 'ok' : 'warn');

    const pBadge = $('statPort');
    pBadge.querySelector('.stat-val').textContent = location.port || '80';
    pBadge.className = 'stat ok';

    const loginBtn = $('loginBtn');
    if (s.logged_in) {
      // 顺便把当前账号 hint 显示在按钮上
      let hint = '';
      try {
        const a = await apiGet('/api/accounts');
        const active = (a.accounts || []).find(x => x.active);
        if (active) hint = active.hint || '';
      } catch(e) {}
      loginBtn.textContent = hint ? ('账号 · ' + hint) : '账号管理';
      loginBtn.classList.remove('need-login');
      loginBtn.classList.add('logged-in');
    } else {
      loginBtn.textContent = '登录夸克';
      loginBtn.classList.remove('logged-in');
      loginBtn.classList.add('need-login');
    }
  } catch(e) {}
}

// ---- Mode switch ----
document.querySelectorAll('.mode-btn').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentMode = btn.dataset.mode;
    const ws = $('workspace');
    ws.classList.toggle('reshare-only', currentMode === 'reshare_only');
    if (currentMode === 'reshare_only') {
      $('inputTitle').textContent = '夸克分享链接';
      $('inputHint').textContent = '仅需粘贴 pan.quark.cn/s/xxx';
      $('input').placeholder = '只需粘贴 pan.quark.cn/s/xxxxx 即可，不会调 DeepSeek';
    } else {
      $('inputTitle').textContent = '原推文';
      $('inputHint').textContent = '粘贴原文 + 夸克分享链接';
      $('input').placeholder = '在这里粘贴对标账号的完整推文内容（含 pan.quark.cn/s/xxx 链接）';
    }
  };
});

// ---- Accounts & Login modal ----
let loginTimer = null;
function showLoginView(which) {
  // which: 'list' | 'qr'
  $('accountListView').style.display = which === 'list' ? 'block' : 'none';
  $('loginQrView').style.display = which === 'qr' ? 'block' : 'none';
  $('backToListBtn').style.display = which === 'qr' ? 'inline-block' : 'none';
  $('loginModalTitle').textContent = which === 'list' ? '账号管理' : '添加新账号';
}

async function renderAccountList() {
  const wrap = $('accountList');
  wrap.innerHTML = '';
  let accounts = [];
  try {
    const r = await apiGet('/api/accounts');
    accounts = r.accounts || [];
  } catch (e) {}
  if (!accounts.length) {
    $('accountEmpty').style.display = 'block';
    return;
  }
  $('accountEmpty').style.display = 'none';
  accounts.forEach(a => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid ' + (a.active ? 'var(--ok)' : 'var(--line)') + ';border-radius:8px;background:var(--bg-0);';
    const dot = document.createElement('span');
    dot.textContent = a.active ? '●' : '○';
    dot.style.cssText = 'color:' + (a.active ? 'var(--ok)' : 'var(--text-1)') + ';font-size:14px;';
    const text = document.createElement('div');
    text.style.flex = '1';
    const hintEl = document.createElement('div');
    hintEl.textContent = a.hint || '已登录';
    hintEl.style.cssText = 'font-family:var(--mono);font-size:14px;color:var(--text-0);';
    const sub = document.createElement('div');
    sub.textContent = a.active ? '当前使用中' : ('最后使用 ' + (a.last_used_at ? new Date(a.last_used_at * 1000).toLocaleString() : '-'));
    sub.style.cssText = 'font-size:11px;color:var(--text-1);margin-top:2px;';
    text.appendChild(hintEl); text.appendChild(sub);
    row.appendChild(dot); row.appendChild(text);

    if (!a.active) {
      const sw = document.createElement('button');
      sw.textContent = '切换';
      sw.style.fontSize = '12px';
      sw.onclick = async () => {
        const r = await apiPost('/api/accounts/switch', { id: a.id });
        if (r.ok) { await renderAccountList(); refreshState(); }
        else alert(r.error || '切换失败');
      };
      row.appendChild(sw);
    }
    const del = document.createElement('button');
    del.textContent = '删除';
    del.style.fontSize = '12px';
    del.onclick = async () => {
      if (!confirm('确定删除账号「' + (a.hint || a.id) + '」？删除后需重新扫码添加。')) return;
      const r = await apiPost('/api/accounts/remove', { id: a.id });
      if (r.ok) { await renderAccountList(); refreshState(); }
      else alert(r.error || '删除失败');
    };
    row.appendChild(del);
    wrap.appendChild(row);
  });
}

$('loginBtn').onclick = async () => {
  openModal('loginModal');
  showLoginView('list');
  await renderAccountList();
};

async function startQrFlow() {
  showLoginView('qr');
  $('qrImg').removeAttribute('src');
  $('qrEmpty').style.display = 'block';
  $('qrStatus').textContent = '正在启动浏览器…';
  $('qrStatus').className = 'qr-status';
  $('qrMeta').textContent = '首次启动可能会慢一些，如首轮失败会自动重试。';
  $('qrMeta').className = 'status';
  $('manualCookieBox').style.display = 'none';
  $('toggleManualCookie').textContent = '展开 ▾';
  $('manualCookieStatus').className = 'status';
  $('manualCookieInput').value = '';
  try {
    await apiPost('/api/login/start');
    pollLogin();
  } catch(e) {
    $('qrStatus').textContent = '启动失败: ' + e;
    $('qrStatus').className = 'qr-status err';
  }
}
$('addAccountBtn').onclick = startQrFlow;
$('backToListBtn').onclick = async () => {
  if (loginTimer) clearTimeout(loginTimer);
  try { await apiPost('/api/login/stop'); } catch(e) {}
  await renderAccountList();
  showLoginView('list');
};
$('closeLogin').onclick = async () => {
  closeModal('loginModal');
  if (loginTimer) clearTimeout(loginTimer);
  try { await apiPost('/api/login/stop'); } catch(e) {}
  refreshState();
};

$('toggleManualCookie').onclick = () => {
  const box = $('manualCookieBox');
  const btn = $('toggleManualCookie');
  const open = box.style.display === 'none';
  box.style.display = open ? 'block' : 'none';
  btn.textContent = open ? '收起 ▴' : '展开 ▾';
};
$('saveManualCookie').onclick = async () => {
  const v = $('manualCookieInput').value.trim();
  const st = $('manualCookieStatus');
  if (!v) { st.textContent = '请先粘贴 cookie 内容'; st.className = 'status err'; return; }
  st.textContent = '校验中…'; st.className = 'status run';
  $('saveManualCookie').disabled = true;
  try {
    const r = await apiPost('/api/login/manual', { cookie: v });
    if (r.ok) {
      st.textContent = '导入成功 ' + (r.account_hint ? ('· ' + r.account_hint) : '') + ' ✓';
      st.className = 'status ok';
      try { await apiPost('/api/login/stop'); } catch(e) {}
      setTimeout(async () => {
        await renderAccountList();
        showLoginView('list');
        refreshState();
      }, 700);
    } else {
      st.textContent = r.error || '校验失败'; st.className = 'status err';
    }
  } catch(e) {
    st.textContent = '请求失败: ' + e; st.className = 'status err';
  } finally {
    $('saveManualCookie').disabled = false;
  }
};

async function pollLogin() {
  if (loginTimer) clearTimeout(loginTimer);
  try {
    const s = await apiGet('/api/login/state');
    if (s.qr_data_url) {
      $('qrImg').src = s.qr_data_url;
      $('qrEmpty').style.display = 'none';
    }
    const map = {
      idle: '空闲',
      starting: '正在启动浏览器…',
      waiting_scan: '等待扫码…',
      logged_in: '登录成功 ✓',
      failed: '失败'
    };
    $('qrStatus').textContent = map[s.state] || s.state;
    $('qrStatus').className = 'qr-status' + (s.state === 'logged_in' ? ' ok' : s.state === 'failed' ? ' err' : '');
    const meta = [];
    if (s.account && s.account.username) meta.push('账号：' + s.account.username);
    if (s.account && s.account.quota) meta.push('容量：' + s.account.quota);
    if (s.account && s.account.note) meta.push(s.account.note);
    $('qrMeta').textContent = s.error || meta.join(' · ');
    $('qrMeta').className = 'status ' + (s.state === 'failed' ? 'err' : s.state === 'logged_in' ? 'ok' : '');

    if (s.state === 'logged_in') {
      setTimeout(async () => {
        await renderAccountList();
        showLoginView('list');
        refreshState();
      }, 900);
      return;
    }
    if (s.state === 'failed') {
      if (s.error) $('qrStatus').textContent = s.error;
      return;
    }
  } catch(e) {}
  if ($('loginModal').getAttribute('data-open') === 'true') {
    loginTimer = setTimeout(pollLogin, 1500);
  }
}

// ---- Log export (copy / save) ----
async function fetchLogs() {
  const r = await fetch('/api/logs');
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || '读取日志失败');
  return j;
}
async function copyLogsToClipboard(statusEl) {
  try {
    if (statusEl) { statusEl.textContent = '读取日志…'; statusEl.className = 'status'; }
    const j = await fetchLogs();
    const text = j.content || '';
    try {
      await navigator.clipboard.writeText(text);
    } catch (err) {
      // 降级：textarea + execCommand
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    if (statusEl) { statusEl.textContent = '已复制 ' + text.length + ' 字符到剪贴板，可直接粘贴给开发者'; statusEl.className = 'status ok'; }
  } catch (e) {
    if (statusEl) { statusEl.textContent = '复制失败: ' + e; statusEl.className = 'status err'; }
  }
}
async function saveLogsAsFile(statusEl) {
  try {
    if (statusEl) { statusEl.textContent = '读取日志…'; statusEl.className = 'status'; }
    const j = await fetchLogs();
    const blob = new Blob([j.content || ''], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    a.href = url;
    a.download = 'QuarkMover-log-' + stamp + '.txt';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    if (statusEl) { statusEl.textContent = '已导出：' + a.download; statusEl.className = 'status ok'; }
  } catch (e) {
    if (statusEl) { statusEl.textContent = '导出失败: ' + e; statusEl.className = 'status err'; }
  }
}
$('copyLogBtn').onclick = () => copyLogsToClipboard($('qrMeta'));
$('saveLogBtn').onclick = () => saveLogsAsFile($('qrMeta'));
$('settingsCopyLog').onclick = () => copyLogsToClipboard($('settingsLogStatus'));
$('settingsSaveLog').onclick = () => saveLogsAsFile($('settingsLogStatus'));

// ---- Settings modal ----
$('settingsBtn').onclick = async () => {
  try {
    const s = await apiGet('/api/config');
    $('cfgKey').value = s.deepseek_api_key || '';
    $('cfgPrompt').value = s.rewrite_prompt || '';
    if (s.share_use_passcode) {
      $('cfgPasscodeOn').checked = true;
    } else {
      $('cfgPasscodeOff').checked = true;
    }
    $('cfgExpired').value = String(s.share_expired_type || 1);
  } catch(e) {}
  openModal('settingsModal');
};
$('cancelSettings').onclick = () => closeModal('settingsModal');
$('saveSettings').onclick = async () => {
  try {
    await apiPost('/api/config', {
      deepseek_api_key: $('cfgKey').value,
      rewrite_prompt: $('cfgPrompt').value,
      share_use_passcode: $('cfgPasscodeOn').checked,
      share_expired_type: parseInt($('cfgExpired').value, 10)
    });
    closeModal('settingsModal');
    refreshState();
  } catch(e) {
    alert('保存失败: ' + e);
  }
};

// Click outside modal to close
document.querySelectorAll('.modal-bg').forEach(bg => {
  bg.addEventListener('click', e => {
    if (e.target === bg) bg.setAttribute('data-open', 'false');
  });
});

// ---- Generate pipeline ----
let jobTimer = null;
let stepHistory = [];
let jobStartedAt = 0;

$('go').onclick = async () => {
  const text = $('input').value.trim();
  if (!text) { setStatus('请先粘贴内容', 'err'); return; }
  $('go').disabled = true;
  setStatus('提交任务…', 'run');
  resetProgress();
  stepHistory = [];
  jobStartedAt = Date.now();
  try {
    const r = await apiPost('/api/generate', { text, mode: currentMode });
    if (!r.ok) {
      setStatus(r.error || '失败', 'err');
      $('go').disabled = false;
      return;
    }
    showProgress();
    pollJob(r.job_id);
  } catch(e) {
    setStatus('请求失败: ' + e, 'err');
    $('go').disabled = false;
  }
};

function resetProgress() {
  $('progressSteps').innerHTML = '';
  const p = $('progress');
  p.classList.remove('show', 'done', 'failed');
}
function showProgress() {
  const p = $('progress');
  p.classList.add('show');
  p.classList.remove('done', 'failed');
  $('progressLabel').textContent = '处理中…';
}
function markProgress(state) {
  const p = $('progress');
  p.classList.remove('done', 'failed');
  if (state === 'done' || state === 'failed') p.classList.add(state);
}
function addStep(step, active) {
  const elapsed = ((Date.now() - jobStartedAt) / 1000).toFixed(1) + 's';
  if (stepHistory.length && stepHistory[stepHistory.length-1].step === step) {
    return;
  }
  // mark previous as done
  const steps = $('progressSteps');
  steps.querySelectorAll('.step.active').forEach(el => {
    el.classList.remove('active');
    el.classList.add('done');
  });
  const div = document.createElement('div');
  div.className = 'step active';
  div.innerHTML = `<span class="step-t">${elapsed}</span><span>${escapeHtml(step)}</span>`;
  steps.appendChild(div);
  steps.scrollTop = steps.scrollHeight;
  stepHistory.push({ step, elapsed });
}
function escapeHtml(s) {
  return (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function pollJob(jid) {
  try {
    const s = await apiGet('/api/job/' + jid);
    let label = s.step || '';
    if (s.sub) label += ' · ' + s.sub;

    if (s.step && s.status === 'running') {
      addStep(s.step + (s.sub ? ' · ' + s.sub : ''), true);
    }

    setStatus(label, 'run');

    if (s.status === 'running') {
      jobTimer = setTimeout(() => pollJob(jid), 700);
      return;
    }

    $('go').disabled = false;
    // finalize last step
    $('progressSteps').querySelectorAll('.step.active').forEach(el => {
      el.classList.remove('active');
      el.classList.add('done');
    });

    if (s.status === 'failed') {
      markProgress('failed');
      $('progressLabel').textContent = '失败';
      setStatus(s.error || '任务失败', 'err');
      return;
    }

    markProgress('done');
    $('progressLabel').textContent = '完成';
    const r = s.result;
    $('output').value = r.output;

    if (r.new_link) {
      $('newLinkText').textContent = r.new_link;
      $('newLinkBox').classList.add('show');
    } else {
      $('newLinkBox').classList.remove('show');
    }

    const warnings = [];
    if (r.rewrite_error) warnings.push('DeepSeek：' + r.rewrite_error);
    if (r.had_link && !r.new_link) warnings.push('夸克：' + (r.quark_error || '转存失败'));
    if (!r.had_link && currentMode === 'reshare_only') warnings.push('未检测到夸克分享链接');

    if (warnings.length) {
      setStatus(warnings.join(' | '), 'err');
    } else {
      setStatus('完成 · ' + ((Date.now() - jobStartedAt)/1000).toFixed(1) + ' 秒', 'ok');
    }
  } catch(e) {
    $('go').disabled = false;
    setStatus('查询任务失败: ' + e, 'err');
  }
}

$('clear').onclick = () => {
  $('input').value = '';
  $('output').value = '';
  $('newLinkBox').classList.remove('show');
  resetProgress();
  setStatus('');
};

async function copyText(text, btnId) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    const s = $('copyStatus');
    s.textContent = '已复制 ✓';
    s.className = 'status ok';
    setTimeout(() => { s.textContent = ''; }, 2000);
  } catch(e) {
    const s = $('copyStatus');
    s.textContent = '复制失败';
    s.className = 'status err';
  }
}
$('copy').onclick = () => copyText($('output').value);
$('copyLink').onclick = () => {
  const text = $('newLinkText').textContent;
  if (text) copyText(text);
};

// Esc to close modals
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-bg[data-open="true"]').forEach(m => {
      m.setAttribute('data-open', 'false');
    });
  }
});

document.getElementById('themeBtn').onclick = cycleTheme;
$('promoBtn').onclick = () => openModal('promoModal');
$('closePromo').onclick = () => closeModal('promoModal');

refreshState();
setInterval(refreshState, 20000);

// Auto-onboard: first visit + not logged in → auto-open login modal
(async function autoOnboard() {
  try {
    const s = await apiGet('/api/state');
    if (!s.logged_in && !localStorage.getItem('quark-mover-onboarded')) {
      setTimeout(() => {
        document.getElementById('loginBtn').click();
        localStorage.setItem('quark-mover-onboarded', '1');
      }, 800);
    }
  } catch(e) {}
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("HTTP " + (format % args))

    @staticmethod
    def _is_client_disconnect(exc: BaseException) -> bool:
        return isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError as e:
            if self._is_client_disconnect(e):
                logger.debug(f"client disconnected before response was sent: {e}")
                return
            raise

    def _send_json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    _EXTRA_MIME = {".webp": "image/webp", ".avif": "image/avif", ".woff2": "font/woff2"}

    def _serve_static(self, path: str) -> None:
        import mimetypes
        safe = path.replace("\\", "/").split("/static/", 1)[-1]
        if ".." in safe or safe.startswith("/"):
            return self._send(403, b"forbidden", "text/plain")
        fpath = ROOT / "static" / safe
        if not fpath.is_file():
            return self._send(404, b"not found", "text/plain")
        ext = fpath.suffix.lower()
        ctype = self._EXTRA_MIME.get(ext) or mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
        with open(fpath, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    def do_GET(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html = INDEX_HTML.replace("__APP_VERSION__", APP_VERSION)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send_json({
                    "has_key": _has_key(),
                    "logged_in": _is_logged_in_cached(),
                })
            if path == "/api/login/state":
                return self._send_json(LOGIN_WORKER.snapshot())
            if path == "/api/login/info":
                # precheck：当前 active 账号是否有效 + 展示 hint
                if not load_cookie():
                    return self._send_json({"logged_in": False, "account_hint": ""})
                try:
                    with httpx.Client(http2=False, timeout=8) as client:
                        info = quark_account_info(client)
                    # 若拿到更新的 hint，顺便更新 active 账号的 hint
                    if info.get("logged_in") and info.get("account_hint"):
                        active = next((a for a in ACCOUNTS.list_accounts() if a["active"]), None)
                        if active:
                            ACCOUNTS.update_hint(active["id"], info["account_hint"])
                    return self._send_json(info)
                except Exception as e:
                    logger.debug(f"login info failed: {e}")
                    return self._send_json({"logged_in": False, "account_hint": ""})
            if path == "/api/accounts":
                return self._send_json({"accounts": ACCOUNTS.list_accounts()})
            if path == "/api/logs":
                # 返回今日日志文本，供前端复制/另存；若无今日日志，拼接最近一份
                try:
                    files = sorted(LOG_DIR.glob("*.log"))
                    if not files:
                        return self._send_json({"ok": True, "filename": "", "content": "(暂无日志)"})
                    # 取最近 2 份拼接，避免刚过 0 点时今日文件为空
                    picks = files[-2:]
                    chunks = []
                    total = 0
                    MAX = 512 * 1024  # 最多 512KB
                    for p in picks:
                        try:
                            text = p.read_text(encoding="utf-8", errors="replace")
                        except Exception as e:
                            text = f"(读取 {p.name} 失败: {e})"
                        chunks.append(f"===== {p.name} =====\n{text}")
                        total += len(text)
                    content = "\n\n".join(chunks)
                    if len(content) > MAX:
                        content = "(…前文已截断…)\n" + content[-MAX:]
                    return self._send_json({
                        "ok": True,
                        "filename": picks[-1].name,
                        "content": content,
                    })
                except Exception as e:
                    logger.exception("read logs failed")
                    return self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            if path == "/api/config":
                return self._send_json({
                    "deepseek_api_key": get_cfg("deepseek_api_key", ""),
                    "rewrite_prompt": get_cfg("rewrite_prompt", DEFAULT_CONFIG["rewrite_prompt"]),
                    "share_use_passcode": get_cfg("share_use_passcode", False),
                    "share_expired_type": get_cfg("share_expired_type", 1),
                })
            if path.startswith("/api/job/"):
                jid = path.rsplit("/", 1)[-1]
                job = JOB_MANAGER.get(jid)
                if not job:
                    return self._send_json({"error": "job not found"}, 404)
                return self._send_json(job.snapshot())
            if path.startswith("/static/"):
                return self._serve_static(path)
            return self._send(404, b"not found", "text/plain")
        except Exception as e:
            if self._is_client_disconnect(e):
                logger.debug(f"GET client disconnected: {e}")
                return
            logger.exception("GET handler error")
            try:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            except OSError as send_err:
                if self._is_client_disconnect(send_err):
                    logger.debug(f"GET error response aborted by client: {send_err}")
                    return
                raise

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/config":
                body = self._read_json()
                update_cfg({k: v for k, v in body.items() if k in DEFAULT_CONFIG})
                logger.info("config updated")
                return self._send_json({"ok": True})
            if path == "/api/login/start":
                LOGIN_WORKER.start()
                return self._send_json({"ok": True})
            if path == "/api/login/stop":
                LOGIN_WORKER.stop()
                return self._send_json({"ok": True})
            if path == "/api/accounts/switch":
                body = self._read_json()
                aid = (body.get("id") or "").strip()
                if not aid or not ACCOUNTS.switch(aid):
                    return self._send_json({"ok": False, "error": "账号不存在"})
                _invalidate_login_cache()
                return self._send_json({"ok": True})
            if path == "/api/accounts/remove":
                body = self._read_json()
                aid = (body.get("id") or "").strip()
                if not aid or not ACCOUNTS.remove(aid):
                    return self._send_json({"ok": False, "error": "账号不存在"})
                _invalidate_login_cache()
                return self._send_json({"ok": True})
            if path == "/api/login/manual":
                body = self._read_json()
                raw = (body.get("cookie") or "").strip()
                if not raw:
                    return self._send_json({"ok": False, "error": "cookie 为空"})
                # 允许用户整段粘贴，如 "Cookie: a=b; c=d" 或带换行；统一清洗
                if raw.lower().startswith("cookie:"):
                    raw = raw.split(":", 1)[1].strip()
                cookie_str = " ".join(raw.split())  # 折叠空白/换行
                # 校验
                try:
                    r = httpx.get(
                        "https://drive-pc.quark.cn/1/clouddrive/config?pr=ucpro&fr=pc&uc_param_str=",
                        headers={
                            "user-agent": QUARK_HEADERS_BASE["user-agent"],
                            "origin": "https://pan.quark.cn",
                            "referer": "https://pan.quark.cn/",
                            "cookie": cookie_str,
                            "accept": "application/json, text/plain, */*",
                        },
                        timeout=8,
                    )
                    ok = r.status_code == 200 and r.json().get("status") == 200
                except Exception as e:
                    return self._send_json({"ok": False, "error": f"校验失败: {type(e).__name__}: {e}"})
                if not ok:
                    return self._send_json({"ok": False, "error": "cookie 无效或已过期，请重新复制"})
                # 查账号 hint；失败则用默认
                hint = "已登录"
                try:
                    with httpx.Client(http2=False, timeout=8) as c2:
                        # quark_account_info 内部用 quark_headers 读全局 cookies.txt，
                        # 所以先短暂写入再查（try/finally 恢复 active）
                        prev_active = ACCOUNTS.active_cookie()
                        COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
                        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
                            f.write(cookie_str + "\n")
                        try:
                            info = quark_account_info(c2)
                            if info.get("account_hint"):
                                hint = info["account_hint"]
                        finally:
                            # 先还原成旧 active，随后 upsert 会再覆盖为新账号
                            if prev_active:
                                with open(COOKIES_PATH, "w", encoding="utf-8") as f:
                                    f.write(prev_active + "\n")
                except Exception as e:
                    logger.debug(f"fetch hint for manual cookie failed: {e}")
                try:
                    ACCOUNTS.upsert(cookie_str, hint=hint)
                except Exception as e:
                    return self._send_json({"ok": False, "error": f"保存账号失败: {e}"})
                _invalidate_login_cache()
                logger.info(f"cookies imported manually, hint={hint}")
                return self._send_json({"ok": True, "account_hint": hint})
            if path == "/api/generate":
                body = self._read_json()
                text = body.get("text", "")
                mode = body.get("mode", "full")
                if mode not in ("full", "reshare_only"):
                    mode = "full"
                if not text.strip():
                    return self._send_json({"ok": False, "error": "内容为空"})
                job = JOB_MANAGER.create()
                t = threading.Thread(target=run_pipeline, args=(job, text, mode), daemon=True)
                t.start()
                return self._send_json({"ok": True, "job_id": job.id})
            return self._send(404, b"not found", "text/plain")
        except Exception as e:
            if self._is_client_disconnect(e):
                logger.debug(f"POST client disconnected: {e}")
                return
            logger.exception("POST handler error")
            try:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            except OSError as send_err:
                if self._is_client_disconnect(send_err):
                    logger.debug(f"POST error response aborted by client: {send_err}")
                    return
                raise


def _log_startup_banner() -> None:
    """启动横幅 - 将版本号与环境信息打到日志，方便用户把日志发给开发者时一眼看出环境。"""
    import platform
    from utils import find_chrome_path

    frozen = bool(getattr(sys, "frozen", False))
    chrome = find_chrome_path() or "(未检测到 Chrome/Edge)"
    logger.info("=" * 60)
    logger.info(f"夸克转存助手  v{APP_VERSION}  (frozen={frozen})")
    logger.info(f"Python {platform.python_version()} · {platform.system()} {platform.release()} · {platform.machine()}")
    logger.info(f"可执行文件: {sys.executable}")
    logger.info(f"项目根目录: {ROOT}")
    logger.info(f"配置文件:   {CONFIG_PATH}")
    logger.info(f"Cookie:     {COOKIES_PATH} (exists={COOKIES_PATH.exists()})")
    logger.info(f"日志目录:   {LOG_DIR}")
    logger.info(f"浏览器:     {chrome}")
    logger.info("=" * 60)


def main() -> None:
    _log_startup_banner()
    preferred = int(get_cfg("port", 8899))
    port = pick_free_port(preferred)
    if port != preferred:
        logger.warning(f"端口 {preferred} 占用，改用 {port}")
    url = f"http://127.0.0.1:{port}"
    logger.info(f"夸克转存助手 已启动: {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
