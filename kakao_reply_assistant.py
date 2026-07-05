from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import html
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if os.name == "nt":
    from ctypes import wintypes
else:  # pragma: no cover - non-Windows fallback
    wintypes = None

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception:  # pragma: no cover - tkinter may be unavailable in CI
    tk = None
    ttk = None
    messagebox = None

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "data" / "state.json"
MESSAGE_LOG_PATH = APP_DIR / "data" / "messages.jsonl"
_DPI_AWARENESS_SET = False


DEFAULT_CONFIG: dict[str, Any] = {
    "target_senders": ["홍길동"],
    "sender_match_mode": "contains",
    "app_keywords": ["KakaoTalk", "카카오톡", "Kakao"],
    "poll_seconds": 2.0,
    "process_existing_on_start": False,
    "save_messages": True,
    "message_log_path": "data/messages.jsonl",
    "windows_notification_listener_enabled": True,
    "kakao_popup_vision_enabled": False,
    "kakao_chat_window_enabled": False,
    "kakao_chat_capture_seconds": 3.0,
    "kakao_chat_min_api_interval_seconds": 8.0,
    "kakao_chat_require_title_match": True,
    "kakao_chat_min_width": 360,
    "kakao_chat_min_height": 360,
    "kakao_chat_capture_method": "auto",
    "kakao_chat_bottom_crop_ratio": 0.72,
    "kakao_chat_bottom_trim_pixels": 45,
    "kakao_chat_dedup_seconds": 300,
    "save_chat_captures": False,
    "kakao_popup_capture_width": 430,
    "kakao_popup_capture_height": 300,
    "kakao_popup_burst_delays": [0.0, 0.05, 0.15, 0.35],
    "save_popup_captures": False,
    "openai_model": "gpt-5.4-mini",
    "openai_api_key_env": "OPENAI_API_KEY",
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1/responses",
    "max_output_tokens": 180,
    "reply_style": "친근하고 자연스럽게, 너무 길지 않게",
    "user_profile": "한국어로 대화하는 일반 사용자",
    "system_prompt": (
        "너는 카카오톡 답장 초안을 작성하는 한국어 비서다. "
        "사용자가 직접 복사해서 보낼 수 있는 답장 문장만 작성한다. "
        "확정적으로 알 수 없는 내용은 단정하지 말고 확인하는 어조로 쓴다. "
        "계좌, 인증번호, 비밀번호, 민감한 개인정보, 법률/의료/투자 판단이 필요한 요청은 "
        "바로 처리하지 말고 사용자가 직접 확인하도록 안전하게 답한다. "
        "따옴표, 목록, 설명, '초안:' 같은 접두사는 붙이지 않는다."
    ),
}


def enable_dpi_awareness() -> None:
    global _DPI_AWARENESS_SET
    if _DPI_AWARENESS_SET or os.name != "nt":
        return

    _DPI_AWARENESS_SET = True
    try:
        user32 = ctypes.windll.user32
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            bits = ctypes.sizeof(ctypes.c_void_p) * 8
            mask = (1 << bits) - 1
            for value in (-4, -2):
                context = ctypes.c_void_p(value & mask)
                if user32.SetProcessDpiAwarenessContext(context):
                    return
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


@dataclass(frozen=True)
class ParsedNotification:
    key: str
    app_id: str
    app_assets: str
    title: str
    body: str
    sender: str
    message: str
    arrival_time: int | None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return dict(DEFAULT_CONFIG)

    with path.open("r", encoding="utf-8") as f:
        user_config = json.load(f)

    config = dict(DEFAULT_CONFIG)
    config.update(user_config)
    return config


def write_default_config(path: Path = CONFIG_PATH) -> None:
    if path.exists():
        return
    path.write_text(
        json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"seen_keys": [], "recent_chat_messages": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state.get("seen_keys"), list):
            if not isinstance(state.get("recent_chat_messages"), list):
                state["recent_chat_messages"] = []
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"seen_keys": [], "recent_chat_messages": []}


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen_keys = list(dict.fromkeys(state.get("seen_keys", [])))[-2000:]
    recent_chat_messages = state.get("recent_chat_messages", [])
    if not isinstance(recent_chat_messages, list):
        recent_chat_messages = []
    payload = {
        "seen_keys": seen_keys,
        "recent_chat_messages": recent_chat_messages[-500:],
        "updated_at": now_iso(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def normalize_dedup_text(value: str) -> str:
    text = normalize(value).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([?!.,~…])", r"\1", text)
    text = re.sub(r"([?!.,~…])\s+", r"\1", text)
    return text


def chat_content_key(room: str, sender: str, message: str) -> str:
    return "|".join(
        [
            "chat_window",
            normalize_dedup_text(room),
            normalize_dedup_text(sender),
            normalize_dedup_text(message),
        ]
    )


def prune_recent_chat_messages(
    records: list[dict[str, Any]],
    now_ts: float,
    window_seconds: float,
) -> list[dict[str, Any]]:
    cutoff = now_ts - max(window_seconds, 0.0)
    pruned: list[dict[str, Any]] = []
    for record in records:
        try:
            seen_at = float(record.get("seen_at", 0.0))
        except (TypeError, ValueError):
            continue
        if seen_at >= cutoff:
            pruned.append(record)
    return pruned[-500:]


def recent_chat_message_seen(
    records: list[dict[str, Any]],
    room: str,
    sender: str,
    message: str,
    now_ts: float,
    window_seconds: float,
) -> bool:
    key = chat_content_key(room, sender, message)
    for record in prune_recent_chat_messages(records, now_ts, window_seconds):
        if record.get("key") == key:
            return True
    return False


def remember_recent_chat_message(
    records: list[dict[str, Any]],
    room: str,
    sender: str,
    message: str,
    now_ts: float,
    window_seconds: float,
) -> list[dict[str, Any]]:
    key = chat_content_key(room, sender, message)
    pruned = [record for record in prune_recent_chat_messages(records, now_ts, window_seconds) if record.get("key") != key]
    pruned.append(
        {
            "key": key,
            "room": normalize(room),
            "sender": normalize(sender),
            "message": normalize(message),
            "seen_at": now_ts,
            "created_at": datetime.fromtimestamp(now_ts, timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
    )
    return pruned[-500:]


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError(f"JSON 객체를 찾지 못했습니다: {text[:200]}")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("JSON 응답이 객체가 아닙니다.")
    return value


def app_relative_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return APP_DIR / path


def read_user_environment(name: str) -> str:
    if not name or winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value).strip()


def powershell_async_helpers() -> str:
    return r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$script:asTaskMethods = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' }
function AwaitOperation($operation, [Type]$resultType) {
  $method = $script:asTaskMethods | Where-Object { $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 } | Select-Object -First 1
  $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
  return $task.GetAwaiter().GetResult()
}
"""


def notification_listener_status() -> str:
    if os.name != "nt":
        return "Unsupported"
    script = powershell_async_helpers() + r"""
$null = [Windows.UI.Notifications.Management.UserNotificationListener, Windows.UI.Notifications, ContentType=WindowsRuntime]
$listener = [Windows.UI.Notifications.Management.UserNotificationListener]::Current
[string]$listener.GetAccessStatus()
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        return "Error: " + completed.stderr.strip()
    return completed.stdout.strip()


def request_notification_listener_access() -> str:
    if os.name != "nt":
        return "Unsupported"
    script = powershell_async_helpers() + r"""
$null = [Windows.UI.Notifications.Management.UserNotificationListener, Windows.UI.Notifications, ContentType=WindowsRuntime]
$listener = [Windows.UI.Notifications.Management.UserNotificationListener]::Current
$status = AwaitOperation $listener.RequestAccessAsync() ([Windows.UI.Notifications.Management.UserNotificationListenerAccessStatus])
[string]$status
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        return "Error: " + completed.stderr.strip()
    return completed.stdout.strip()


def notification_db_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA 환경 변수를 찾을 수 없습니다.")
    return Path(local_app_data) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"


def copy_notification_db(src: Path) -> Path:
    if not src.exists():
        raise FileNotFoundError(f"Windows 알림 DB를 찾을 수 없습니다: {src}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="kakao_wpn_"))
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(src) + suffix)
        if candidate.exists():
            shutil.copy2(candidate, tmp_dir / ("wpndatabase.db" + suffix))
    return tmp_dir


def cleanup_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def decode_payload(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    if isinstance(payload, bytes):
        for encoding in ("utf-8", "utf-16-le", "utf-16", "latin-1"):
            try:
                text = payload.decode(encoding)
            except UnicodeDecodeError:
                continue
            if "<" in text or "toast" in text.lower():
                return text
        return payload.decode("utf-8", errors="ignore")
    return str(payload)


def extract_texts_from_payload(payload: Any) -> list[str]:
    text = decode_payload(payload)
    if not text:
        return []

    text = text.strip("\ufeff\x00\r\n ")
    parsed: list[str] = []
    try:
        root = ET.fromstring(text)
        for node in root.iter():
            tag = node.tag.rsplit("}", 1)[-1].lower()
            if tag == "text":
                value = "".join(node.itertext()).strip()
                if value:
                    parsed.append(html.unescape(value))
    except ET.ParseError:
        parsed = []

    if parsed:
        return parsed

    matches = re.findall(r"<text\b[^>]*>(.*?)</text>", text, flags=re.IGNORECASE | re.DOTALL)
    return [html.unescape(re.sub(r"\s+", " ", match)).strip() for match in matches if match.strip()]


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def split_sender_from_body(body: str) -> tuple[str, str] | None:
    body = normalize(body)
    match = re.match(r"^(.{1,40}?)[\:：]\s+(.+)$", body)
    if not match:
        return None
    sender = normalize(match.group(1))
    message = normalize(match.group(2))
    if sender and message:
        return sender, message
    return None


def sender_matches(candidate: str, target: str, mode: str) -> bool:
    candidate_norm = normalize(candidate).casefold()
    target_norm = normalize(target).casefold()
    if not candidate_norm or not target_norm:
        return False
    if mode == "exact":
        return candidate_norm == target_norm
    return target_norm in candidate_norm


def find_target_sender(title: str, body: str, targets: list[str], mode: str) -> tuple[str, str] | None:
    candidates: list[tuple[str, str]] = []
    title_norm = normalize(title)
    body_norm = normalize(body)
    if title_norm:
        candidates.append((title_norm, body_norm))

    sender_body = split_sender_from_body(body_norm)
    if sender_body:
        candidates.append(sender_body)

    for target in targets:
        for sender, message in candidates:
            if sender_matches(sender, target, mode):
                return target, message or body_norm
    return None


class NotificationReader:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or notification_db_path()

    def read_recent(self, limit: int = 80) -> list[dict[str, Any]]:
        tmp_dir = copy_notification_db(self.db_path)
        try:
            db_copy = tmp_dir / "wpndatabase.db"
            conn = sqlite3.connect(db_copy)
            conn.row_factory = sqlite3.Row
            try:
                return self._query_recent(conn, limit)
            finally:
                conn.close()
        finally:
            cleanup_dir(tmp_dir)

    @staticmethod
    def _query_recent(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
        sql = """
            select
                n.Id as notification_id,
                n."Order" as notification_order,
                n.HandlerId as handler_id,
                n.Payload as payload,
                n.ArrivalTime as arrival_time,
                coalesce(h.PrimaryId, '') as app_id,
                coalesce(group_concat(a.AssetKey || '=' || a.AssetValue, ' | '), '') as app_assets
            from Notification n
            left join NotificationHandler h on h.RecordId = n.HandlerId
            left join HandlerAssets a on a.HandlerId = n.HandlerId
            where n.Payload is not null
            group by
                n.Id,
                n."Order",
                n.HandlerId,
                n.Payload,
                n.ArrivalTime,
                h.PrimaryId
            order by coalesce(n.ArrivalTime, n.Id, n."Order") desc
            limit ?
        """
        try:
            rows = conn.execute(sql, (int(limit),)).fetchall()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"Windows 알림 DB를 읽는 중 오류가 났습니다: {exc}") from exc
        return [dict(row) for row in rows]


def read_user_notification_listener(limit: int = 80) -> list[dict[str, Any]]:
    if os.name != "nt":
        return []

    script = powershell_async_helpers() + r"""
$ErrorActionPreference = 'Stop'
$null = [Windows.UI.Notifications.Management.UserNotificationListener, Windows.UI.Notifications, ContentType=WindowsRuntime]
$null = [Windows.UI.Notifications.NotificationKinds, Windows.UI.Notifications, ContentType=WindowsRuntime]
$listener = [Windows.UI.Notifications.Management.UserNotificationListener]::Current
$status = [string]$listener.GetAccessStatus()
if ($status -ne 'Allowed') {
  @{ status = $status; notifications = @() } | ConvertTo-Json -Depth 8 -Compress
  return
}
$items = AwaitOperation ($listener.GetNotificationsAsync([Windows.UI.Notifications.NotificationKinds]::Toast)) ([System.Collections.Generic.IReadOnlyList[Windows.UI.Notifications.UserNotification]])
$notifications = @()
foreach ($notification in $items) {
  $texts = New-Object System.Collections.Generic.List[string]
  try {
    foreach ($binding in $notification.Notification.Visual.Bindings) {
      foreach ($textElement in $binding.GetTextElements()) {
        $text = [string]$textElement.Text
        if (-not [string]::IsNullOrWhiteSpace($text)) {
          $texts.Add($text)
        }
      }
    }
  } catch {}

  $appName = ''
  $appId = ''
  try { $appName = [string]$notification.AppInfo.DisplayInfo.DisplayName } catch {}
  try { $appId = [string]$notification.AppInfo.AppUserModelId } catch {}

  $notifications += [pscustomobject]@{
    id = [string]$notification.Id
    creationTime = $notification.CreationTime.ToString('o')
    appName = $appName
    appUserModelId = $appId
    texts = @($texts)
  }
}
@{ status = $status; notifications = @($notifications | Sort-Object creationTime -Descending | Select-Object -First __LIMIT__) } | ConvertTo-Json -Depth 8 -Compress
""".replace("__LIMIT__", str(int(limit)))

    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Windows 알림 리스너 오류: {completed.stderr.strip()}")

    output = completed.stdout.strip()
    if not output:
        return []
    payload = json.loads(output)
    if payload.get("status") != "Allowed":
        return []
    notifications = payload.get("notifications", [])
    if isinstance(notifications, dict):
        notifications = [notifications]
    if not isinstance(notifications, list):
        return []
    return [item for item in notifications if isinstance(item, dict)]


def app_matches(row: dict[str, Any], keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{row.get('app_id', '')} {row.get('app_assets', '')}".casefold()
    return any(keyword.casefold() in haystack for keyword in keywords if keyword)


def listener_app_matches(item: dict[str, Any], keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{item.get('appName', '')} {item.get('appUserModelId', '')}".casefold()
    return any(keyword.casefold() in haystack for keyword in keywords if keyword)


def parse_notification(row: dict[str, Any], config: dict[str, Any]) -> ParsedNotification | None:
    if not app_matches(row, list(config.get("app_keywords", []))):
        return None

    texts = extract_texts_from_payload(row.get("payload"))
    if not texts:
        return None

    title = normalize(texts[0])
    body = normalize(" ".join(texts[1:]))
    if not title and not body:
        return None

    targets = [normalize(v) for v in config.get("target_senders", []) if normalize(v)]
    if not targets:
        return None

    match = find_target_sender(
        title,
        body,
        targets,
        normalize(config.get("sender_match_mode", "contains")),
    )
    if not match:
        return None

    sender, message = match
    key = "|".join(
        [
            str(row.get("handler_id", "")),
            str(row.get("notification_id", "")),
            str(row.get("arrival_time", "")),
            title,
            body,
        ]
    )
    return ParsedNotification(
        key=key,
        app_id=str(row.get("app_id", "")),
        app_assets=str(row.get("app_assets", "")),
        title=title,
        body=body,
        sender=sender,
        message=message,
        arrival_time=row.get("arrival_time"),
    )


def parse_listener_notification(item: dict[str, Any], config: dict[str, Any]) -> ParsedNotification | None:
    if not listener_app_matches(item, list(config.get("app_keywords", []))):
        return None

    raw_texts = item.get("texts", [])
    if isinstance(raw_texts, str):
        raw_texts = [raw_texts]
    texts = [normalize(str(text)) for text in raw_texts if normalize(str(text))]
    if not texts:
        return None

    title = texts[0]
    body = normalize(" ".join(texts[1:]))
    targets = [normalize(v) for v in config.get("target_senders", []) if normalize(v)]
    if not targets:
        return None

    match = find_target_sender(
        title,
        body,
        targets,
        normalize(config.get("sender_match_mode", "contains")),
    )
    if not match:
        return None

    sender, message = match
    key = "|".join(
        [
            "listener",
            str(item.get("id", "")),
            str(item.get("creationTime", "")),
            title,
            body,
        ]
    )
    return ParsedNotification(
        key=key,
        app_id=str(item.get("appUserModelId", "")),
        app_assets=str(item.get("appName", "")),
        title=title,
        body=body,
        sender=sender,
        message=message or body,
        arrival_time=None,
    )


class OpenAIReplyGenerator:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def api_key(self) -> str:
        configured = str(self.config.get("openai_api_key", "")).strip()
        if configured:
            return configured
        env_name = str(self.config.get("openai_api_key_env", "OPENAI_API_KEY")).strip()
        return os.environ.get(env_name, "").strip() or read_user_environment(env_name)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self.api_key()
        if not api_key:
            raise RuntimeError(
                "OpenAI API 키가 없습니다. OPENAI_API_KEY 환경 변수 또는 config.json의 openai_api_key를 설정하세요."
            )

        request = urllib.request.Request(
            str(self.config.get("openai_base_url", DEFAULT_CONFIG["openai_base_url"])),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI API 오류 {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI API 연결 오류: {exc.reason}") from exc

    def generate(self, notification: ParsedNotification) -> str:
        model = str(self.config.get("openai_model", DEFAULT_CONFIG["openai_model"])).strip()
        payload = {
            "model": model,
            "instructions": str(self.config.get("system_prompt", DEFAULT_CONFIG["system_prompt"])),
            "input": (
                f"사용자 프로필: {self.config.get('user_profile', '')}\n"
                f"답장 스타일: {self.config.get('reply_style', '')}\n"
                f"보낸 사람: {notification.sender}\n"
                f"받은 카카오톡 메시지: {notification.message}\n\n"
                "위 메시지에 대한 한국어 답장 초안 1개를 작성해."
            ),
            "max_output_tokens": int(self.config.get("max_output_tokens", 180)),
            "store": False,
        }

        data = self._post(payload)

        text = extract_openai_text(data)
        if not text:
            raise RuntimeError("OpenAI API 응답에서 답장 텍스트를 찾지 못했습니다.")
        return text.strip()

    def generate_for_message(self, sender: str, message: str) -> str:
        notification = ParsedNotification(
            key="manual_regenerate",
            app_id="",
            app_assets="",
            title=sender,
            body=message,
            sender=sender,
            message=message,
            arrival_time=None,
        )
        return self.generate(notification)

    def generate_from_popup_image(self, image_path: Path) -> dict[str, Any]:
        return self.generate_from_popup_images([image_path])

    def generate_from_popup_images(self, image_paths: list[Path]) -> dict[str, Any]:
        model = str(self.config.get("openai_model", DEFAULT_CONFIG["openai_model"])).strip()
        targets = [normalize(v) for v in self.config.get("target_senders", []) if normalize(v)]
        match_mode = normalize(self.config.get("sender_match_mode", "contains")) or "contains"
        prompt = {
            "target_senders": targets,
            "sender_match_mode": match_mode,
            "reply_style": self.config.get("reply_style", ""),
            "user_profile": self.config.get("user_profile", ""),
            "task": (
                "이 이미지들은 Windows 화면의 카카오톡 알림 후보 영역을 캡처한 것이다. "
                "일부 이미지는 알림이 아닌 배경, 콘솔, 빠른 답장 입력칸일 수 있다. "
                "보낸 사람과 받은 메시지를 읽어라. '메시지 입력'은 답장 입력칸 placeholder이므로 받은 메시지로 보지 마라. "
                "보낸 사람이 target_senders에 매칭되고 메시지를 읽을 수 있을 때만 should_reply를 true로 하고 draft를 작성하라. "
                "읽기 어렵거나 대상자가 아니면 should_reply=false로 하라. "
                "반드시 JSON 객체 하나만 출력하라."
            ),
            "json_schema": {
                "should_reply": "boolean",
                "sender": "string",
                "message": "string",
                "draft": "string",
                "reason": "string",
            },
        }
        content: list[dict[str, Any]] = [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]
        for image_path in image_paths:
            image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{image_data}",
                    "detail": "high",
                }
            )
        payload = {
            "model": model,
            "instructions": (
                "너는 한국어 카카오톡 알림 이미지를 읽고 답장 초안을 만드는 비서다. "
                "이미지에서 실제로 보이는 내용만 사용하고 추측하지 않는다. "
                "답장 초안에는 따옴표, 목록, 설명, '초안:' 같은 접두사를 붙이지 않는다."
            ),
            "input": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "max_output_tokens": max(int(self.config.get("max_output_tokens", 180)), 220),
            "store": False,
        }
        data = self._post(payload)
        text = extract_openai_text(data)
        if not text:
            raise RuntimeError("OpenAI API 이미지 응답에서 JSON 텍스트를 찾지 못했습니다.")
        parsed = parse_json_object(text)
        return {
            "should_reply": bool(parsed.get("should_reply")),
            "sender": normalize(str(parsed.get("sender", ""))),
            "message": normalize(str(parsed.get("message", ""))),
            "draft": normalize(str(parsed.get("draft", ""))),
            "reason": normalize(str(parsed.get("reason", ""))),
            "raw": parsed,
        }

    def generate_from_chat_window_image(self, image_path: Path, window_title: str = "") -> dict[str, Any]:
        return self.generate_from_chat_window_images([image_path], window_title)

    def generate_from_chat_window_images(
        self,
        image_paths: list[Path],
        window_title: str = "",
        image_labels: list[str] | None = None,
    ) -> dict[str, Any]:
        model = str(self.config.get("openai_model", DEFAULT_CONFIG["openai_model"])).strip()
        targets = [normalize(v) for v in self.config.get("target_senders", []) if normalize(v)]
        match_mode = normalize(self.config.get("sender_match_mode", "contains")) or "contains"
        prompt = {
            "target_senders": targets,
            "sender_match_mode": match_mode,
            "window_title": window_title,
            "reply_style": self.config.get("reply_style", ""),
            "user_profile": self.config.get("user_profile", ""),
            "task": (
                "이 이미지들은 사용자가 직접 열어둔 카카오톡 PC 채팅창이다. "
                "이미지 라벨이 after_last_outgoing_area이면 내 마지막 노란/오른쪽 말풍선 아래 영역만 잘라낸 것이다. "
                "after_last_outgoing_area 이미지가 제공된 경우, 최신 상대 메시지는 반드시 그 이미지 안에 있어야 한다. "
                "after_last_outgoing_area 안에 상대방 말풍선이 없으면 다른 이미지에 과거 상대 메시지가 보여도 should_reply=false로 하라. "
                "bottom_area는 최신 대화가 있는 하단 영역이고, full_window는 전체 창이다. "
                "채팅방 제목 또는 화면의 상대 이름이 target_senders에 매칭되는지 확인하라. "
                "내가 보낸 메시지는 보통 오른쪽 정렬/노란 말풍선이고, 상대가 보낸 메시지는 보통 왼쪽 정렬/흰색 또는 회색 말풍선이다. "
                "먼저 화면에서 가장 아래쪽에 보이는 내 노란/오른쪽 말풍선을 찾아라. "
                "그 내 말풍선보다 위에 있는 상대방 말풍선은 이미 내가 답장한 과거 메시지로 간주하고 절대 답장하지 마라. "
                "내 노란/오른쪽 말풍선이 상대 말풍선보다 아래에 조금이라도 보이면, 그 상대 말풍선에는 답장하지 말고 should_reply=false로 하라. "
                "내 마지막 말풍선보다 아래에 있는 상대방 말풍선이 있을 때만 그 가장 아래쪽 상대 말풍선을 최신 상대 메시지로 선택하라. "
                "화면에 내 말풍선이 전혀 보이지 않을 때만 화면에서 가장 아래쪽 상대방 말풍선을 최신 메시지로 선택하라. "
                "'메시지 입력' 같은 입력창 placeholder나 사용자가 입력 중인 텍스트는 상대 메시지가 아니므로 무시하라. "
                "내 마지막 메시지가 보이면 그 아래에 있는 상대방 메시지들만 이어 붙여 message로 반환하라. "
                "내 마지막 메시지 아래에 상대방 메시지가 없으면 should_reply=false로 하라. "
                "같은 상대 메시지가 화면에 중복으로 보이면 가장 아래쪽 중복 하나만 사용하라. "
                "상대 메시지가 짧은 확인 답변(예: 넵, 응, 오케이, ㅇㅋ, 네)이어도 should_reply=true로 하고 자연스러운 짧은 답장을 작성하라. "
                "상대 메시지가 없거나, 대상자가 아니거나, 내용을 읽기 어려울 때만 should_reply=false로 하라. "
                "should_reply=true일 때는 message에 대한 한국어 답장 초안 1개를 draft로 작성하라. "
                "반드시 JSON 객체 하나만 출력하라."
            ),
            "json_schema": {
                "should_reply": "boolean",
                "sender": "string",
                "message": "string",
                "draft": "string",
                "reason": "string",
            },
        }
        content: list[dict[str, Any]] = [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]
        labels = image_labels or [f"image_{index + 1}" for index in range(len(image_paths))]
        for index, image_path in enumerate(image_paths):
            label = labels[index] if index < len(labels) else f"image_{index + 1}"
            image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append({"type": "input_text", "text": f"image_label: {label}"})
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{image_data}",
                    "detail": "high",
                }
            )
        payload = {
            "model": model,
            "instructions": (
                "너는 한국어 카카오톡 채팅창 이미지를 읽고 답장 초안을 만드는 비서다. "
                "이미지에서 실제로 보이는 내용만 사용하고 추측하지 않는다. "
                "최우선 규칙: 가장 아래쪽 내 노란/오른쪽 말풍선보다 아래에 있는 상대방 말풍선에만 답장한다. "
                "상대 말풍선 아래에 내 노란/오른쪽 말풍선이 보이면 그 상대 말풍선은 과거 메시지이므로 should_reply=false다. "
                "입력창 placeholder와 내가 작성 중인 입력 텍스트는 무시한다. "
                "내가 보낸 메시지에는 답장하지 말고, 내 마지막 메시지 이후 상대가 보낸 메시지에만 답장한다. "
                "내 마지막 메시지가 화면에 전혀 없을 때만 가장 아래쪽 상대 메시지에 답장한다. "
                "대상자의 최신 메시지가 짧아도 답장 초안을 만든다. "
                "날씨, 일정 가능 여부, 장소, 가격처럼 실제 확인이 필요한 정보는 사실을 지어내지 말고 확인 후 알려주겠다는 자연스러운 답장을 쓴다. "
                "답장 초안에는 따옴표, 목록, 설명, '초안:' 같은 접두사를 붙이지 않는다."
            ),
            "input": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "max_output_tokens": max(int(self.config.get("max_output_tokens", 180)), 240),
            "store": False,
        }
        data = self._post(payload)
        text = extract_openai_text(data)
        if not text:
            raise RuntimeError("OpenAI API 채팅창 이미지 응답에서 JSON 텍스트를 찾지 못했습니다.")
        parsed = parse_json_object(text)
        return {
            "should_reply": bool(parsed.get("should_reply")),
            "sender": normalize(str(parsed.get("sender", ""))),
            "message": normalize(str(parsed.get("message", ""))),
            "draft": normalize(str(parsed.get("draft", ""))),
            "reason": normalize(str(parsed.get("reason", ""))),
            "raw": parsed,
        }


def extract_openai_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    parts: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(part.strip() for part in parts if part.strip())


class DraftPopup:
    def __init__(
        self,
        root: tk.Tk,
        item: dict[str, Any],
        regenerate_callback: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.root = root
        self.item = item
        self.regenerate_callback = regenerate_callback
        self.window = tk.Toplevel(root)
        self.window.title(f"카톡 답장 초안 - {item['sender']}")
        self.window.geometry("460x300+80+80")
        self.window.attributes("-topmost", True)
        self.window.after(800, lambda: self.window.attributes("-topmost", False))
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self._build()

    def _build(self) -> None:
        frame = ttk.Frame(self.window, padding=14)
        frame.pack(fill="both", expand=True)

        sender = ttk.Label(frame, text=f"{self.item['sender']}", font=("", 12, "bold"))
        sender.pack(anchor="w")

        incoming = ttk.Label(
            frame,
            text=self.item["message"],
            wraplength=420,
            foreground="#444444",
        )
        incoming.pack(anchor="w", fill="x", pady=(4, 10))

        self.text = tk.Text(frame, height=7, wrap="word", undo=True)
        self.text.insert("1.0", self.item["draft"])
        self.text.pack(fill="both", expand=True)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))

        copy_button = ttk.Button(actions, text="복사", command=self.copy)
        copy_button.pack(side="left")

        self.regenerate_button = ttk.Button(actions, text="새 답변", command=self.regenerate)
        self.regenerate_button.pack(side="left", padx=(8, 0))
        if self.regenerate_callback is None:
            self.regenerate_button.state(["disabled"])

        close_button = ttk.Button(actions, text="닫기", command=self.window.destroy)
        close_button.pack(side="right")

    def copy(self) -> None:
        draft = self.text.get("1.0", "end").strip()
        self.window.clipboard_clear()
        self.window.clipboard_append(draft)
        self.window.update()
        if messagebox:
            messagebox.showinfo("복사됨", "답장 초안이 클립보드에 복사되었습니다.", parent=self.window)

    def regenerate(self) -> None:
        if self.regenerate_callback is None:
            return

        self.regenerate_button.state(["disabled"])
        self.regenerate_button.configure(text="생성 중")

        def worker() -> None:
            try:
                draft = self.regenerate_callback(self.item)
            except Exception as exc:
                self.window.after(0, lambda error=exc: self._finish_regenerate_error(error))
                return
            self.window.after(0, lambda new_draft=draft: self._finish_regenerate(new_draft))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_regenerate(self, draft: str) -> None:
        self.item["draft"] = draft
        self.text.delete("1.0", "end")
        self.text.insert("1.0", draft)
        self.regenerate_button.configure(text="새 답변")
        self.regenerate_button.state(["!disabled"])

    def _finish_regenerate_error(self, exc: Exception) -> None:
        self.regenerate_button.configure(text="새 답변")
        self.regenerate_button.state(["!disabled"])
        if messagebox:
            messagebox.showerror("새 답변 생성 실패", str(exc), parent=self.window)


class KakaoReplyAssistant:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.reader = NotificationReader()
        self.generator = OpenAIReplyGenerator(config)
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.state = load_state()
        self.seen_keys: set[str] = set(self.state.get("seen_keys", []))
        raw_recent = self.state.get("recent_chat_messages", [])
        self.recent_chat_messages: list[dict[str, Any]] = raw_recent if isinstance(raw_recent, list) else []

    def run(self) -> None:
        if tk is None or ttk is None:
            raise RuntimeError("tkinter를 사용할 수 없습니다. Python tkinter 설치가 필요합니다.")

        write_default_config()
        self._mark_existing_seen_if_needed()

        root = tk.Tk()
        root.withdraw()
        root.title("Kakao Reply Assistant")

        worker = threading.Thread(target=self._poll_loop, daemon=True)
        worker.start()
        if self.config.get("kakao_popup_vision_enabled", False):
            popup_worker = threading.Thread(target=self._popup_vision_loop, daemon=True)
            popup_worker.start()
        if self.config.get("kakao_chat_window_enabled", False):
            chat_worker = threading.Thread(target=self._chat_window_loop, daemon=True)
            chat_worker.start()

        def drain_queue() -> None:
            while True:
                try:
                    item = self.events.get_nowait()
                except queue.Empty:
                    break
                if item.get("type") == "draft":
                    print(
                        "[popup] "
                        f"sender={item.get('sender', '')!r} "
                        f"message={item.get('message', '')!r} "
                        f"draft={item.get('draft', '')!r}"
                    )
                    DraftPopup(root, item, self._regenerate_draft)
                elif item.get("type") == "error" and messagebox:
                    messagebox.showerror("Kakao Reply Assistant", item.get("message", "알 수 없는 오류"))
            root.after(500, drain_queue)

        def on_close() -> None:
            self.stop_event.set()
            self._save_state()
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)
        root.after(500, drain_queue)
        print("Kakao Reply Assistant 실행 중입니다. 종료하려면 콘솔에서 Ctrl+C를 누르세요.")
        try:
            root.mainloop()
        except KeyboardInterrupt:
            on_close()

    def _save_state(self) -> None:
        save_state(
            {
                "seen_keys": list(self.seen_keys),
                "recent_chat_messages": self.recent_chat_messages,
            }
        )

    def _regenerate_draft(self, item: dict[str, Any]) -> str:
        sender = normalize(str(item.get("sender", "")))
        message = normalize(str(item.get("message", "")))
        if not sender or not message:
            raise RuntimeError("새 답변을 만들 sender/message가 없습니다.")
        draft = self.generator.generate_for_message(sender, message)
        item_payload = {
            "type": "draft",
            "id": str(uuid.uuid4()),
            "created_at": now_iso(),
            "source": "manual_regenerate",
            "sender": sender,
            "message": message,
            "draft": draft,
            "notification_key": item.get("notification_key", ""),
        }
        if self.config.get("save_messages", True):
            append_jsonl(app_relative_path(str(self.config.get("message_log_path", "data/messages.jsonl"))), item_payload)
        print(
            "[popup] regenerated "
            f"sender={sender!r} message={message!r} draft={draft!r}"
        )
        return draft

    def _mark_existing_seen_if_needed(self) -> None:
        if self.config.get("process_existing_on_start"):
            return
        try:
            rows = self.reader.read_recent()
        except Exception as exc:
            print(f"시작 시 알림 DB를 읽지 못했습니다: {exc}", file=sys.stderr)
            return
        for row in rows:
            parsed = parse_notification(row, self.config)
            if parsed:
                self.seen_keys.add(parsed.key)
        if self.config.get("windows_notification_listener_enabled", True):
            try:
                for item in read_user_notification_listener():
                    parsed = parse_listener_notification(item, self.config)
                    if parsed:
                        self.seen_keys.add(parsed.key)
            except Exception as exc:
                print(f"시작 시 Windows 알림 리스너를 읽지 못했습니다: {exc}", file=sys.stderr)
        self._save_state()

    def _poll_loop(self) -> None:
        poll_seconds = float(self.config.get("poll_seconds", 2.0))
        while not self.stop_event.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                self.events.put({"type": "error", "message": str(exc)})
                time.sleep(max(poll_seconds, 5.0))
            self.stop_event.wait(poll_seconds)

    def _poll_once(self) -> None:
        rows = self.reader.read_recent()
        parsed_items = [parse_notification(row, self.config) for row in rows]
        notifications = [item for item in parsed_items if item is not None]
        if self.config.get("windows_notification_listener_enabled", True):
            listener_items = read_user_notification_listener()
            notifications.extend(
                item
                for item in (parse_listener_notification(listener_item, self.config) for listener_item in listener_items)
                if item is not None
            )
        notifications.reverse()

        changed = False
        for notification in notifications:
            if notification.key in self.seen_keys:
                continue
            self.seen_keys.add(notification.key)
            changed = True
            draft = self.generator.generate(notification)
            item = {
                "type": "draft",
                "id": str(uuid.uuid4()),
                "created_at": now_iso(),
                "sender": notification.sender,
                "message": notification.message,
                "draft": draft,
                "notification_key": notification.key,
            }
            self.events.put(item)
            if self.config.get("save_messages", True):
                append_jsonl(app_relative_path(str(self.config.get("message_log_path", "data/messages.jsonl"))), item)

        if changed:
            self._save_state()

    def _popup_vision_loop(self) -> None:
        if os.name != "nt":
            return
        process_cache: dict[int, str] = {}
        seen_signatures = {
            popup_window_signature(item)
            for item in _top_level_windows().values()
            if is_popup_event_candidate(item, process_cache)
        }
        last_capture_at = 0.0

        while not self.stop_event.is_set():
            current = _top_level_windows()
            for hwnd, item in current.items():
                if not is_popup_event_candidate(item, process_cache):
                    continue
                signature = popup_window_signature(item)
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                now = time.monotonic()
                if now - last_capture_at < 1.5:
                    continue
                last_capture_at = now

                try:
                    self._handle_popup_image(item)
                except Exception as exc:
                    self.events.put({"type": "error", "message": f"카톡 팝업 이미지 처리 오류: {exc}"})
            self.stop_event.wait(0.2)

    def _handle_popup_image(self, item: dict[str, Any]) -> None:
        capture_dir = app_relative_path("data/popup_captures")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_dir = capture_dir if self.config.get("save_popup_captures", False) else Path(tempfile.gettempdir())
        captured = capture_popup_burst(item, self.config, output_dir, f"kakao_popup_{stamp}")
        image_paths = [image_path for image_path, _label, _rect, _delay in captured]
        try:
            parsed = self.generator.generate_from_popup_images(image_paths)
        finally:
            if not self.config.get("save_popup_captures", False):
                for image_path in image_paths:
                    image_path.unlink(missing_ok=True)

        sender = parsed.get("sender", "")
        message = parsed.get("message", "")
        draft = parsed.get("draft", "")
        if not parsed.get("should_reply") or not sender or not message or not draft:
            return

        key = f"popup_image|{sender}|{message}"
        if key in self.seen_keys:
            return
        self.seen_keys.add(key)
        item_payload = {
            "type": "draft",
            "id": str(uuid.uuid4()),
            "created_at": now_iso(),
            "source": "kakao_popup_image",
            "sender": sender,
            "message": message,
            "draft": draft,
            "notification_key": key,
        }
        self.events.put(item_payload)
        if self.config.get("save_messages", True):
            append_jsonl(app_relative_path(str(self.config.get("message_log_path", "data/messages.jsonl"))), item_payload)
        self._save_state()

    def _chat_window_loop(self) -> None:
        if os.name != "nt":
            return

        seen_hashes: dict[int, str] = {}
        last_api_at: dict[int, float] = {}
        interval = float(self.config.get("kakao_chat_capture_seconds", 3.0))
        while not self.stop_event.is_set():
            for window in kakao_chat_windows(self.config):
                try:
                    changed = self._handle_chat_window(window, seen_hashes, last_api_at)
                    if changed:
                        time.sleep(0.3)
                except Exception as exc:
                    self.events.put({"type": "error", "message": f"카톡 채팅창 처리 오류: {exc}"})
            self.stop_event.wait(max(interval, 1.0))

    def _handle_chat_window(
        self,
        window: dict[str, Any],
        seen_hashes: dict[int, str],
        last_api_at: dict[int, float],
    ) -> bool:
        capture_dir = app_relative_path("data/chat_captures")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        if self.config.get("save_chat_captures", False):
            image_path = capture_dir / f"kakao_chat_{stamp}_{int(window['hwnd'])}.png"
            bottom_path = capture_dir / f"kakao_chat_{stamp}_{int(window['hwnd'])}_bottom.png"
            after_path = capture_dir / f"kakao_chat_{stamp}_{int(window['hwnd'])}_after_me.png"
        else:
            image_path = Path(tempfile.gettempdir()) / f"kakao_chat_{uuid.uuid4().hex}.png"
            bottom_path = Path(tempfile.gettempdir()) / f"kakao_chat_{uuid.uuid4().hex}_bottom.png"
            after_path = Path(tempfile.gettempdir()) / f"kakao_chat_{uuid.uuid4().hex}_after_me.png"

        capture_method = capture_chat_window_png(window, image_path, self.config)
        if image_looks_blank(image_path):
            print(
                "[chat] skip: 화면 캡처가 검은 이미지입니다. "
                f"capture_method={capture_method}. "
                "dxcam/Pillow 설치 후 다시 시도하거나 --dump-chat-ui로 텍스트 노출 여부를 확인하세요."
            )
            return False
        crop_image_png(image_path, bottom_path, chat_bottom_crop_rect_in_image(window, self.config))
        image_paths = [bottom_path, image_path]
        image_labels = ["bottom_area", "full_window"]
        yellow_y = detect_bottommost_outgoing_yellow_y(image_path)
        if yellow_y is not None:
            _x, _y, width, height = window_capture_rect(window)
            bottom_trim = int(self.config.get("kakao_chat_bottom_trim_pixels", 45))
            crop_top = min(max(yellow_y + 6, 0), max(0, height - 1))
            crop_height = max(1, height - bottom_trim - crop_top)
            if crop_height < 36:
                print(
                    "[chat] skip: "
                    f"title={normalize(str(window.get('title', '')))!r} "
                    "내 마지막 노란 말풍선 아래에 보이는 상대 메시지 영역이 없습니다."
                )
                return False
            if crop_height >= 36:
                crop_image_png(image_path, after_path, (0, crop_top, width, crop_height))
                image_paths = [after_path, bottom_path, image_path]
                image_labels = ["after_last_outgoing_area", "bottom_area", "full_window"]
        try:
            image_hash = hashlib.sha256(b"".join(path.read_bytes() for path in image_paths)).hexdigest()
            hwnd = int(window["hwnd"])
            if seen_hashes.get(hwnd) == image_hash:
                return False
            seen_hashes[hwnd] = image_hash
            now = time.monotonic()
            min_interval = float(self.config.get("kakao_chat_min_api_interval_seconds", 8.0))
            if now - last_api_at.get(hwnd, 0.0) < min_interval:
                return False
            last_api_at[hwnd] = now

            parsed = self.generator.generate_from_chat_window_images(
                image_paths,
                normalize(str(window.get("title", ""))),
                image_labels,
            )
            print(
                "[chat] "
                f"title={normalize(str(window.get('title', '')))!r} "
                f"should_reply={parsed.get('should_reply')} "
                f"sender={parsed.get('sender')!r} "
                f"message={parsed.get('message')!r} "
                f"draft={parsed.get('draft')!r} "
                f"reason={parsed.get('reason')!r}"
            )
        finally:
            if not self.config.get("save_chat_captures", False):
                for path in {image_path, bottom_path, after_path}:
                    path.unlink(missing_ok=True)

        sender = parsed.get("sender", "")
        message = parsed.get("message", "")
        draft = parsed.get("draft", "")
        if not parsed.get("should_reply") or not sender or not message or not draft:
            return True

        room = normalize(str(window.get("title", "")))
        now_ts = time.time()
        dedup_seconds = float(self.config.get("kakao_chat_dedup_seconds", 300))
        if recent_chat_message_seen(
            self.recent_chat_messages,
            room,
            sender,
            message,
            now_ts,
            dedup_seconds,
        ):
            self.recent_chat_messages = prune_recent_chat_messages(
                self.recent_chat_messages,
                now_ts,
                dedup_seconds,
            )
            self._save_state()
            print(
                "[chat] duplicate content skipped "
                f"room={room!r} sender={sender!r} message={message!r} "
                f"window={dedup_seconds:g}s"
            )
            return False

        key = chat_content_key(room, sender, message)
        self.recent_chat_messages = remember_recent_chat_message(
            self.recent_chat_messages,
            room,
            sender,
            message,
            now_ts,
            dedup_seconds,
        )
        item_payload = {
            "type": "draft",
            "id": str(uuid.uuid4()),
            "created_at": now_iso(),
            "source": "kakao_chat_window",
            "room": room,
            "sender": sender,
            "message": message,
            "draft": draft,
            "notification_key": key,
        }
        self.events.put(item_payload)
        print(f"[chat] draft queued key={key!r}")
        if self.config.get("save_messages", True):
            append_jsonl(app_relative_path(str(self.config.get("message_log_path", "data/messages.jsonl"))), item_payload)
        self._save_state()
        return True


def list_recent(config: dict[str, Any], limit: int) -> None:
    rows = NotificationReader().read_recent(limit)
    for row in rows:
        texts = extract_texts_from_payload(row.get("payload"))
        if not texts:
            continue
        title = normalize(texts[0])
        body = normalize(" ".join(texts[1:]))
        matched = parse_notification(row, config)
        marker = "MATCH" if matched else "SKIP"
        app = normalize(f"{row.get('app_id', '')} {row.get('app_assets', '')}")
        print(f"[{marker}] app={app}")
        print(f"  title={title}")
        print(f"  body={body}")


def list_listener(config: dict[str, Any], limit: int) -> None:
    status = notification_listener_status()
    print(f"UserNotificationListener: {status}")
    items = read_user_notification_listener(limit)
    for item in items:
        parsed = parse_listener_notification(item, config)
        marker = "MATCH" if parsed else "SKIP"
        app = normalize(f"{item.get('appName', '')} {item.get('appUserModelId', '')}")
        texts = item.get("texts", [])
        if isinstance(texts, str):
            texts = [texts]
        print(f"[{marker}] app={app} id={item.get('id', '')} time={item.get('creationTime', '')}")
        for index, text in enumerate(texts):
            print(f"  text{index}={normalize(str(text))}")


def watch_listener(config: dict[str, Any], seconds: float, interval: float = 1.0) -> None:
    print(f"{seconds:g}초 동안 Windows UserNotificationListener 새 알림을 감시합니다.")
    print("이 명령을 켜둔 상태에서 카톡을 하나 받아보세요.")
    baseline = {str(item.get("id", "")) for item in read_user_notification_listener(200)}
    seen = set(baseline)
    deadline = time.time() + seconds

    while time.time() < deadline:
        try:
            items = read_user_notification_listener(200)
        except Exception as exc:
            print(f"listener error: {exc}")
            time.sleep(interval)
            continue
        for item in sorted(items, key=lambda value: str(value.get("creationTime", ""))):
            item_id = str(item.get("id", ""))
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            parsed = parse_listener_notification(item, config)
            marker = "MATCH" if parsed else "SKIP"
            app = normalize(f"{item.get('appName', '')} {item.get('appUserModelId', '')}")
            print(f"[{marker}] app={app} id={item_id} time={item.get('creationTime', '')}")
            texts = item.get("texts", [])
            if isinstance(texts, str):
                texts = [texts]
            for index, text in enumerate(texts):
                print(f"  text{index}={normalize(str(text))}")
        time.sleep(interval)

    print("감시 종료")


def diagnose(config: dict[str, Any], limit: int) -> None:
    env_name = str(config.get("openai_api_key_env", "OPENAI_API_KEY")).strip()
    generator = OpenAIReplyGenerator(config)
    rows = NotificationReader().read_recent(limit)
    with_text = 0
    app_matches_count = 0
    target_text_hits = 0
    full_matches = 0
    app_counts: dict[str, int] = {}
    targets = [normalize(v) for v in config.get("target_senders", []) if normalize(v)]

    for row in rows:
        texts = extract_texts_from_payload(row.get("payload"))
        if not texts:
            continue
        with_text += 1
        app = normalize(f"{row.get('app_id', '')} {row.get('app_assets', '')}") or "<blank>"
        app_counts[app[:120]] = app_counts.get(app[:120], 0) + 1
        title = normalize(texts[0])
        body = normalize(" ".join(texts[1:]))
        if app_matches(row, list(config.get("app_keywords", []))):
            app_matches_count += 1
        if any(target and (target in title or target in body) for target in targets):
            target_text_hits += 1
        if parse_notification(row, config):
            full_matches += 1

    print("=== 설정 ===")
    print(f"target_senders: {config.get('target_senders', [])}")
    print(f"sender_match_mode: {config.get('sender_match_mode', 'contains')}")
    print(f"app_keywords: {config.get('app_keywords', [])}")
    print(f"process_existing_on_start: {config.get('process_existing_on_start', False)}")
    print()
    print("=== OpenAI API 키 ===")
    print(f"{env_name} in current process: {bool(os.environ.get(env_name, '').strip())}")
    print(f"{env_name} in user environment: {bool(read_user_environment(env_name))}")
    print(f"usable by app: {bool(generator.api_key())}")
    print()
    print("=== Windows 알림 읽기 권한 ===")
    print(f"UserNotificationListener: {notification_listener_status()}")
    print()
    print("=== 최근 Windows 알림 ===")
    print(f"rows checked: {len(rows)}")
    print(f"notifications with text: {with_text}")
    print(f"app keyword matches: {app_matches_count}")
    print(f"target name text hits: {target_text_hits}")
    print(f"full matches that would call API: {full_matches}")
    print()
    print("=== 최근 앱 상위 목록 ===")
    for app, count in sorted(app_counts.items(), key=lambda item: item[1], reverse=True)[:10]:
        print(f"{count}x {app}")
    if config.get("windows_notification_listener_enabled", True):
        try:
            listener_items = read_user_notification_listener(limit)
            listener_matches = sum(1 for item in listener_items if parse_listener_notification(item, config))
            print()
            print("=== UserNotificationListener 최근 알림 ===")
            print(f"notifications read: {len(listener_items)}")
            print(f"matches that would call API: {listener_matches}")
        except Exception as exc:
            print()
            print("=== UserNotificationListener 최근 알림 ===")
            print(f"error: {exc}")


def _process_image_name(pid: int) -> str:
    if os.name != "nt":
        return ""
    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
        if not ok:
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _top_level_windows() -> dict[int, dict[str, Any]]:
    if os.name != "nt":
        return {}

    enable_dpi_awareness()
    user32 = ctypes.windll.user32
    enum_windows_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    def window_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    result: dict[int, dict[str, Any]] = {}

    @enum_windows_proc
    def callback(hwnd: int, _lparam: int) -> bool:
        rect = Rect()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        result[int(hwnd)] = {
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "class": class_name(hwnd),
            "title": window_text(hwnd),
            "rect": (rect.left, rect.top, rect.right, rect.bottom),
            "size": (width, height),
        }
        return True

    user32.EnumWindows(callback, 0)
    return result


def _child_windows(parent_hwnd: int) -> dict[int, dict[str, Any]]:
    if os.name != "nt":
        return {}

    user32 = ctypes.windll.user32
    enum_windows_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    def window_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    result: dict[int, dict[str, Any]] = {}

    @enum_windows_proc
    def callback(hwnd: int, _lparam: int) -> bool:
        rect = Rect()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        result[int(hwnd)] = {
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "class": class_name(hwnd),
            "title": window_text(hwnd),
            "rect": (rect.left, rect.top, rect.right, rect.bottom),
            "size": (width, height),
        }
        return True

    user32.EnumChildWindows(parent_hwnd, callback, 0)
    return result


def win32_control_text(hwnd: int) -> str:
    if os.name != "nt":
        return ""

    user32 = ctypes.windll.user32
    wm_gettext = 0x000D
    wm_gettextlength = 0x000E
    smto_abortifhung = 0x0002
    timeout_ms = 500

    result = ctypes.c_void_p()
    ok = user32.SendMessageTimeoutW(
        wintypes.HWND(hwnd),
        wm_gettextlength,
        wintypes.WPARAM(0),
        wintypes.LPARAM(0),
        smto_abortifhung,
        timeout_ms,
        ctypes.byref(result),
    )
    if not ok:
        return ""

    length = int(result.value or 0)
    if length <= 0:
        return ""

    buffer = ctypes.create_unicode_buffer(length + 1)
    result = ctypes.c_void_p()
    ok = user32.SendMessageTimeoutW(
        wintypes.HWND(hwnd),
        wm_gettext,
        wintypes.WPARAM(length + 1),
        wintypes.LPARAM(ctypes.addressof(buffer)),
        smto_abortifhung,
        timeout_ms,
        ctypes.byref(result),
    )
    if not ok:
        return ""
    return normalize(buffer.value)


def screen_size() -> tuple[int, int]:
    if os.name != "nt":
        return (0, 0)
    enable_dpi_awareness()
    user32 = ctypes.windll.user32
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def popup_capture_rect(window: dict[str, Any], config: dict[str, Any]) -> tuple[int, int, int, int]:
    if str(window.get("class", "")) == "XamlExplorerHostIslandWindow":
        screen_w, screen_h = screen_size()
        left, top, right, bottom = window["rect"]
        capture_width = int(config.get("kakao_popup_capture_width", 430))
        capture_height = int(config.get("kakao_popup_capture_height", 300))
        host_right = min(screen_w or right, right)
        host_top = max(0, top)
        width = min(capture_width, host_right)
        height = min(capture_height, max(1, bottom - host_top))
        return max(0, host_right - width), host_top, width, height

    rects = [window["rect"]]
    for child in _child_windows(int(window["hwnd"])).values():
        width, height = child["size"]
        if width > 5 and height > 5:
            rects.append(child["rect"])

    screen_w, screen_h = screen_size()
    min_left = min(rect[0] for rect in rects)
    max_right = max(rect[2] for rect in rects)
    max_bottom = max(rect[3] for rect in rects)
    capture_width = int(config.get("kakao_popup_capture_width", 430))
    capture_height = int(config.get("kakao_popup_capture_height", 300))

    right = min(screen_w or max_right, max_right + 8)
    bottom = min(screen_h or max_bottom, max_bottom + 8)
    left = max(0, min(min_left - 20, right - capture_width))
    top = max(0, bottom - capture_height)
    width = max(1, right - left)
    height = max(1, bottom - top)
    return left, top, width, height


def capture_screen_rect_png(
    rect: tuple[int, int, int, int],
    output_path: Path,
    include_layered_windows: bool = True,
) -> None:
    left, top, width, height = [int(value) for value in rect]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    operation = 0x00CC0020
    if include_layered_windows:
        operation |= 0x40000000
    script = r"""
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class DpiAwareCapture {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
"@
[DpiAwareCapture]::SetProcessDPIAware() | Out-Null
$left = __LEFT__
$top = __TOP__
$width = __WIDTH__
$height = __HEIGHT__
$out = @'
__OUT__
'@
$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$operation = [System.Drawing.CopyPixelOperation]([int]__OPERATION__)
$graphics.CopyFromScreen($left, $top, 0, 0, $bitmap.Size, $operation)
$bitmap.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
""".replace("__LEFT__", str(left))
    script = script.replace("__TOP__", str(top))
    script = script.replace("__WIDTH__", str(width))
    script = script.replace("__HEIGHT__", str(height))
    script = script.replace("__OUT__", str(output_path))
    script = script.replace("__OPERATION__", str(operation))
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"화면 캡처 실패: {completed.stderr.strip()}")


def capture_window_png(hwnd: int, output_path: Path, fallback_rect: tuple[int, int, int, int] | None = None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    script = r"""
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32PrintWindowCapture {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint nFlags);
  [StructLayout(LayoutKind.Sequential)] public struct RECT {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
  }
}
"@
[Win32PrintWindowCapture]::SetProcessDPIAware() | Out-Null
$hwnd = [IntPtr]([int64]__HWND__)
$out = @'
__OUT__
'@
$rect = New-Object Win32PrintWindowCapture+RECT
if (-not [Win32PrintWindowCapture]::GetWindowRect($hwnd, [ref]$rect)) {
  throw "GetWindowRect failed"
}
$width = [Math]::Max(1, $rect.Right - $rect.Left)
$height = [Math]::Max(1, $rect.Bottom - $rect.Top)
$saved = $false
foreach ($flag in 2, 3, 1, 0) {
  $bitmap = New-Object System.Drawing.Bitmap $width, $height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  $hdc = $graphics.GetHdc()
  $ok = [Win32PrintWindowCapture]::PrintWindow($hwnd, $hdc, [uint32]$flag)
  $graphics.ReleaseHdc($hdc)
  $graphics.Dispose()
  if ($ok) {
    $bitmap.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
    $saved = $true
    break
  }
  $bitmap.Dispose()
}
if (-not $saved) {
  throw "PrintWindow failed"
}
""".replace("__HWND__", str(int(hwnd)))
    script = script.replace("__OUT__", str(output_path))
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if completed.returncode == 0:
        return
    if fallback_rect is not None:
        capture_screen_rect_png(fallback_rect, output_path)
        return
    raise RuntimeError(f"창 캡처 실패: {completed.stderr.strip()}")


def capture_screen_rect_dxcam_png(rect: tuple[int, int, int, int], output_path: Path) -> None:
    left, top, width, height = [int(value) for value in rect]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import dxcam  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except Exception as exc:
        raise RuntimeError("dxcam/Pillow가 설치되어 있지 않습니다. python -m pip install dxcam pillow") from exc

    camera = dxcam.create(output_color="RGB")
    if camera is None:
        raise RuntimeError("dxcam 카메라를 만들지 못했습니다.")
    frame = camera.grab(region=(left, top, left + width, top + height))
    if frame is None:
        raise RuntimeError("dxcam 캡처 결과가 비어 있습니다.")
    Image.fromarray(frame).save(output_path)


def capture_chat_window_png(window: dict[str, Any], output_path: Path, config: dict[str, Any]) -> str:
    rect = window_capture_rect(window)
    method = normalize(str(config.get("kakao_chat_capture_method", "screen"))).lower()
    if method in {"window", "printwindow", "print_window"}:
        capture_window_png(int(window["hwnd"]), output_path, rect)
        return "window"
    if method in {"dxcam", "dxgi"}:
        capture_screen_rect_dxcam_png(rect, output_path)
        return "dxcam"
    if method in {"screen_plain", "screen-no-captureblt", "screen_no_captureblt"}:
        capture_screen_rect_png(rect, output_path, include_layered_windows=False)
        return "screen_plain"
    if method in {"screen", "gdi", "copyfromscreen"}:
        capture_screen_rect_png(rect, output_path, include_layered_windows=True)
        return "screen"

    attempts: list[tuple[str, Any]] = [
        ("dxcam", lambda: capture_screen_rect_dxcam_png(rect, output_path)),
        ("screen_plain", lambda: capture_screen_rect_png(rect, output_path, include_layered_windows=False)),
        ("screen", lambda: capture_screen_rect_png(rect, output_path, include_layered_windows=True)),
        ("window", lambda: capture_window_png(int(window["hwnd"]), output_path, rect)),
    ]
    errors: list[str] = []
    for name, action in attempts:
        try:
            action()
            if not image_looks_blank(output_path):
                return name
            errors.append(f"{name}: blank")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if errors:
        print("[chat] capture attempts: " + " | ".join(errors))
    return "blank"


def crop_image_png(source_path: Path, output_path: Path, rect: tuple[int, int, int, int]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    x, y, width, height = [int(value) for value in rect]
    script = r"""
Add-Type -AssemblyName System.Drawing
$src = @'
__SRC__
'@
$out = @'
__OUT__
'@
$x = __X__
$y = __Y__
$width = __WIDTH__
$height = __HEIGHT__
$source = [System.Drawing.Bitmap]::FromFile($src)
try {
  $x = [Math]::Max(0, [Math]::Min($x, $source.Width - 1))
  $y = [Math]::Max(0, [Math]::Min($y, $source.Height - 1))
  $width = [Math]::Max(1, [Math]::Min($width, $source.Width - $x))
  $height = [Math]::Max(1, [Math]::Min($height, $source.Height - $y))
  $crop = New-Object System.Drawing.Bitmap $width, $height
  $graphics = [System.Drawing.Graphics]::FromImage($crop)
  $graphics.DrawImage($source, 0, 0, (New-Object System.Drawing.Rectangle $x, $y, $width, $height), [System.Drawing.GraphicsUnit]::Pixel)
  $crop.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  $graphics.Dispose()
  $crop.Dispose()
} finally {
  $source.Dispose()
}
""".replace("__SRC__", str(source_path))
    script = script.replace("__OUT__", str(output_path))
    script = script.replace("__X__", str(x))
    script = script.replace("__Y__", str(y))
    script = script.replace("__WIDTH__", str(width))
    script = script.replace("__HEIGHT__", str(height))
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"이미지 crop 실패: {completed.stderr.strip()}")


def detect_bottommost_outgoing_yellow_y(image_path: Path) -> int | None:
    script = r"""
Add-Type -AssemblyName System.Drawing
$src = @'
__SRC__
'@
$bitmap = [System.Drawing.Bitmap]::FromFile($src)
try {
  $startX = [Math]::Max(0, [int]($bitmap.Width * 0.45))
  $minRun = [Math]::Max(24, [int]($bitmap.Width * 0.08))
  $minRows = 5
  $bestEnd = -1
  $clusterRows = 0
  $clusterEnd = -1

  for ($y = 0; $y -lt $bitmap.Height; $y += 2) {
    $run = 0
    $bestRun = 0
    $yellowCount = 0
    $xSum = 0

    for ($x = $startX; $x -lt $bitmap.Width; $x += 2) {
      $c = $bitmap.GetPixel($x, $y)
      if ($c.R -ge 220 -and $c.G -ge 170 -and $c.B -le 95) {
        $run += 2
        $yellowCount += 1
        $xSum += $x
        if ($run -gt $bestRun) { $bestRun = $run }
      } else {
        $run = 0
      }
    }

    $avgX = 0
    if ($yellowCount -gt 0) { $avgX = $xSum / $yellowCount }
    $isBubbleRow = ($bestRun -ge $minRun -and $yellowCount -ge 12 -and $avgX -ge ($bitmap.Width * 0.55))

    if ($isBubbleRow) {
      $clusterRows += 1
      $clusterEnd = $y
    } else {
      if ($clusterRows -ge $minRows) { $bestEnd = $clusterEnd }
      $clusterRows = 0
      $clusterEnd = -1
    }
  }
  if ($clusterRows -ge $minRows) { $bestEnd = $clusterEnd }
  Write-Output $bestEnd
} finally {
  $bitmap.Dispose()
}
""".replace("__SRC__", str(image_path))
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        value = int(completed.stdout.strip())
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def image_looks_blank(image_path: Path) -> bool:
    script = r"""
Add-Type -AssemblyName System.Drawing
$src = @'
__SRC__
'@
$bitmap = [System.Drawing.Bitmap]::FromFile($src)
try {
  $samples = 0
  $visible = 0
  for ($y = 0; $y -lt $bitmap.Height; $y += 8) {
    for ($x = 0; $x -lt $bitmap.Width; $x += 8) {
      $c = $bitmap.GetPixel($x, $y)
      $samples += 1
      if ($c.R -gt 24 -or $c.G -gt 24 -or $c.B -gt 24) {
        $visible += 1
      }
    }
  }
  if ($samples -le 0) {
    Write-Output "true"
  } else {
    $ratio = $visible / $samples
    Write-Output ($ratio -lt 0.01)
  }
} finally {
  $bitmap.Dispose()
}
""".replace("__SRC__", str(image_path))
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if completed.returncode != 0:
        return False
    return completed.stdout.strip().lower() == "true"


def popup_candidate_rects(window: dict[str, Any], config: dict[str, Any]) -> list[tuple[str, tuple[int, int, int, int]]]:
    screen_w, screen_h = screen_size()
    capture_width = int(config.get("kakao_popup_capture_width", 430))
    capture_height = int(config.get("kakao_popup_capture_height", 300))
    wide_width = max(capture_width, 560)
    tall_height = max(capture_height, 460)
    rects: list[tuple[str, tuple[int, int, int, int]]] = []

    rects.append(("window", popup_capture_rect(window, config)))

    if screen_w > 0 and screen_h > 0:
        right_width = min(wide_width, screen_w)
        bottom_height = min(tall_height, screen_h)
        top_height = min(tall_height, screen_h)
        rects.append(("right_bottom", (screen_w - right_width, max(0, screen_h - bottom_height), right_width, bottom_height)))
        rects.append(("right_top", (screen_w - right_width, 0, right_width, top_height)))
        rects.append(("right_full", (screen_w - right_width, 0, right_width, screen_h)))

    left, top, right, bottom = window["rect"]
    local_width = min(max(wide_width, right - left + 180), screen_w or max(wide_width, right - left + 180))
    local_height = min(max(tall_height, bottom - top + 220), screen_h or max(tall_height, bottom - top + 220))
    local_right = min(screen_w or right, max(right, left + local_width))
    local_bottom = min(screen_h or bottom, max(bottom, top + local_height))
    rects.append(("around_window", (max(0, local_right - local_width), max(0, local_bottom - local_height), local_width, local_height)))

    unique: list[tuple[str, tuple[int, int, int, int]]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for label, rect in rects:
        x, y, width, height = [int(value) for value in rect]
        if width <= 0 or height <= 0:
            continue
        normalized = (x, y, width, height)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append((label, normalized))
    return unique


def popup_burst_delays(config: dict[str, Any]) -> list[float]:
    raw = config.get("kakao_popup_burst_delays", DEFAULT_CONFIG["kakao_popup_burst_delays"])
    if not isinstance(raw, list):
        return [0.0, 0.05, 0.15, 0.35]
    delays: list[float] = []
    for value in raw:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            continue
        if 0 <= delay <= 2:
            delays.append(delay)
    return delays or [0.0, 0.05, 0.15, 0.35]


def capture_popup_burst(
    item: dict[str, Any],
    config: dict[str, Any],
    output_dir: Path,
    prefix: str,
) -> list[tuple[Path, str, tuple[int, int, int, int], float]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    captured: list[tuple[Path, str, tuple[int, int, int, int], float]] = []
    start = time.perf_counter()
    for delay in popup_burst_delays(config):
        while time.perf_counter() - start < delay:
            time.sleep(0.005)
        for label, rect in popup_candidate_rects(item, config):
            safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
            image_path = output_dir / f"{prefix}_d{int(delay * 1000):03d}_{safe_label}.png"
            capture_screen_rect_png(rect, image_path)
            captured.append((image_path, label, rect, delay))
    return captured


def is_kakao_popup_window(item: dict[str, Any]) -> bool:
    class_name = str(item.get("class", ""))
    title = str(item.get("title", ""))
    visible = bool(item.get("visible"))
    width, height = item.get("size", (0, 0))
    if width <= 5 or height <= 5:
        return False
    if class_name == "EVA_Window_Dblclk" and visible:
        return True
    if class_name == "KakaoTalkShadowWndClass" and "KakaoTalkShadowWnd" in title:
        return True
    if class_name == "XamlExplorerHostIslandWindow" and visible:
        return True
    return False


def popup_window_exe(item: dict[str, Any], process_cache: dict[int, str]) -> str:
    pid = int(item.get("pid", 0))
    if pid not in process_cache:
        process_cache[pid] = _process_image_name(pid)
    return Path(process_cache[pid]).name.casefold() if process_cache[pid] else ""


def is_popup_event_candidate(item: dict[str, Any], process_cache: dict[int, str]) -> bool:
    class_name = str(item.get("class", ""))
    if class_name not in {"EVA_Window_Dblclk", "KakaoTalkShadowWndClass", "XamlExplorerHostIslandWindow"}:
        return False

    width, height = item.get("size", (0, 0))
    if width <= 5 or height <= 5:
        return False

    exe = popup_window_exe(item, process_cache)
    if class_name in {"EVA_Window_Dblclk", "KakaoTalkShadowWndClass"}:
        return exe == "kakaotalk.exe"
    if class_name == "XamlExplorerHostIslandWindow":
        return exe == "explorer.exe" and bool(item.get("visible"))
    return False


def popup_window_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(item.get("hwnd", 0)),
        str(item.get("class", "")),
        bool(item.get("visible")),
        tuple(item.get("rect", ())),
        tuple(item.get("size", ())),
        str(item.get("title", "")),
    )


def any_target_matches_text(text: str, config: dict[str, Any]) -> bool:
    mode = normalize(config.get("sender_match_mode", "contains")) or "contains"
    targets = [normalize(v) for v in config.get("target_senders", []) if normalize(v)]
    return any(sender_matches(text, target, mode) for target in targets)


def is_kakao_chat_window(item: dict[str, Any], process_cache: dict[int, str], config: dict[str, Any]) -> bool:
    if not item.get("visible"):
        return False

    exe = popup_window_exe(item, process_cache)
    if exe != "kakaotalk.exe":
        return False

    class_name = str(item.get("class", ""))
    if class_name in {"KakaoTalkShadowWndClass", "SysShadow"}:
        return False

    width, height = item.get("size", (0, 0))
    min_width = int(config.get("kakao_chat_min_width", 360))
    min_height = int(config.get("kakao_chat_min_height", 360))
    if width < min_width or height < min_height:
        return False

    title = normalize(str(item.get("title", "")))
    if config.get("kakao_chat_require_title_match", True):
        return any_target_matches_text(title, config)
    return True


def kakao_chat_windows(config: dict[str, Any]) -> list[dict[str, Any]]:
    process_cache: dict[int, str] = {}
    windows = []
    for item in _top_level_windows().values():
        if is_kakao_chat_window(item, process_cache, config):
            windows.append(item)
    return sorted(windows, key=lambda item: (item.get("rect", (0, 0, 0, 0))[1], item.get("rect", (0, 0, 0, 0))[0]))


def window_capture_rect(item: dict[str, Any]) -> tuple[int, int, int, int]:
    left, top, right, bottom = item["rect"]
    screen_w, screen_h = screen_size()
    left = max(0, int(left))
    top = max(0, int(top))
    right = min(int(right), screen_w or int(right))
    bottom = min(int(bottom), screen_h or int(bottom))
    return left, top, max(1, right - left), max(1, bottom - top)


def chat_bottom_crop_rect(item: dict[str, Any], config: dict[str, Any]) -> tuple[int, int, int, int]:
    left, top, width, height = window_capture_rect(item)
    ratio = float(config.get("kakao_chat_bottom_crop_ratio", 0.72))
    ratio = min(max(ratio, 0.35), 1.0)
    crop_height = max(1, int(height * ratio))
    bottom_trim = int(config.get("kakao_chat_bottom_trim_pixels", 0))
    bottom_trim = min(max(bottom_trim, 0), max(0, height - 1))
    crop_top = top + max(0, height - crop_height - bottom_trim)
    crop_bottom = top + height - bottom_trim
    if crop_bottom <= crop_top:
        crop_top = top
        crop_bottom = top + height
    return left, crop_top, width, max(1, crop_bottom - crop_top)


def chat_bottom_crop_rect_in_image(item: dict[str, Any], config: dict[str, Any]) -> tuple[int, int, int, int]:
    _left, top, width, height = window_capture_rect(item)
    _screen_left, crop_top, _screen_width, crop_height = chat_bottom_crop_rect(item, config)
    image_y = max(0, crop_top - top)
    return 0, image_y, width, crop_height


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def watch_windows(seconds: float, interval: float = 0.25) -> None:
    print(f"{seconds:g}초 동안 새로 생기는 Windows 창을 감시합니다.")
    print("이 명령을 켜둔 상태에서 대상자에게 카톡을 하나 보내게 해보세요.")
    baseline = set(_top_level_windows().keys())
    seen = set(baseline)
    process_cache: dict[int, str] = {}
    deadline = time.time() + seconds

    while time.time() < deadline:
        current = _top_level_windows()
        for hwnd, item in current.items():
            if hwnd in seen:
                continue
            seen.add(hwnd)
            width, height = item["size"]
            if width <= 5 or height <= 5:
                continue
            pid = item["pid"]
            if pid not in process_cache:
                process_cache[pid] = _process_image_name(pid)
            exe = Path(process_cache[pid]).name if process_cache[pid] else ""
            print(
                "NEW "
                f"pid={pid} exe={exe} visible={item['visible']} "
                f"class={item['class']!r} title={item['title']!r} "
                f"rect={item['rect']}"
            )
        time.sleep(interval)

    print("감시 종료")


def dump_uia_for_hwnd(hwnd: int) -> list[str]:
    if os.name != "nt":
        return []
    script = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Clean([string]$value) {
  if ($null -eq $value) { return "" }
  return (($value -replace "[`r`n]+", " | ") -replace "`t", " ").Trim()
}
$hwnd = [IntPtr]([int64]__HWND__)
try {
  $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
} catch {
  "UNAVAILABLE`t" + (Clean $_.Exception.Message)
  return
}
if ($null -eq $root) { return }
$items = $root.FindAll(
  [System.Windows.Automation.TreeScope]::Subtree,
  [System.Windows.Automation.Condition]::TrueCondition
)
$max = [Math]::Min($items.Count, 120)
for ($i = 0; $i -lt $max; $i++) {
  $current = $items.Item($i).Current
  $name = ($current.Name -replace "[`r`n]+", " ").Trim()
  $className = ($current.ClassName -replace "[`r`n]+", " ").Trim()
  $automationId = ($current.AutomationId -replace "[`r`n]+", " ").Trim()
  $controlType = $current.ControlType.ProgrammaticName
  $textValue = ""
  $valueValue = ""
  $legacyValue = ""
  try {
    $pattern = $null
    if ($items.Item($i).TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pattern)) {
      $textValue = Clean($pattern.DocumentRange.GetText(2000))
    }
  } catch {}
  try {
    $pattern = $null
    if ($items.Item($i).TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
      $valueValue = Clean($pattern.Current.Value)
    }
  } catch {}
  try {
    $pattern = $null
    if ($items.Item($i).TryGetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern, [ref]$pattern)) {
      $legacyValue = Clean($pattern.Current.Value)
    }
  } catch {}
  if ($name.Length -gt 0 -or $className.Length -gt 0 -or $automationId.Length -gt 0 -or $textValue.Length -gt 0 -or $valueValue.Length -gt 0 -or $legacyValue.Length -gt 0) {
    "{0}`t{1}`tclass={2}`taid={3}`tname={4}`ttext={5}`tvalue={6}`tlegacy={7}" -f $i, $controlType, $className, $automationId, $name, $textValue, $valueValue, $legacyValue
  }
}
""".replace("__HWND__", str(int(hwnd)))
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        return [completed.stderr.strip()]
    return [line for line in completed.stdout.splitlines() if line.strip()]


def watch_ui(seconds: float, interval: float = 0.25) -> None:
    print(f"{seconds:g}초 동안 새 창의 UI Automation 텍스트를 감시합니다.")
    print("개인 메시지 내용이 출력될 수 있습니다. 이 명령을 켜둔 상태에서 대상자에게 카톡을 하나 보내게 해보세요.")
    process_cache: dict[int, str] = {}
    seen_signatures = {
        popup_window_signature(item)
        for item in _top_level_windows().values()
        if is_popup_event_candidate(item, process_cache)
    }
    deadline = time.time() + seconds

    while time.time() < deadline:
        current = _top_level_windows()
        for hwnd, item in current.items():
            if not is_popup_event_candidate(item, process_cache):
                continue
            signature = popup_window_signature(item)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            width, height = item["size"]
            if width <= 5 or height <= 5:
                continue
            exe = popup_window_exe(item, process_cache)
            pid = int(item.get("pid", 0))
            print()
            print(
                "NEW "
                f"hwnd={hwnd} pid={pid} exe={exe} visible={item['visible']} "
                f"class={item['class']!r} title={item['title']!r} rect={item['rect']}"
            )
            lines = dump_uia_for_hwnd(hwnd)
            if lines:
                print("UIA:")
                for line in lines:
                    print("  " + line)
            else:
                print("UIA: <no text exposed>")
            children = _child_windows(hwnd)
            for child in children.values():
                child_width, child_height = child["size"]
                if child_width <= 5 or child_height <= 5:
                    continue
                print(
                    "  CHILD "
                    f"hwnd={child['hwnd']} visible={child['visible']} "
                    f"class={child['class']!r} title={child['title']!r} rect={child['rect']}"
                )
                win32_text = win32_control_text(child["hwnd"])
                if win32_text:
                    print(f"    WIN32_TEXT: {win32_text}")
                child_lines = dump_uia_for_hwnd(child["hwnd"])
                for line in child_lines[:30]:
                    print("    " + line)
        time.sleep(interval)

    print("감시 종료")


def dump_chat_ui(config: dict[str, Any]) -> None:
    print("열려 있는 대상 카카오톡 채팅창의 UI Automation 텍스트를 출력합니다.")
    print("개인 메시지 내용이 출력될 수 있습니다.")
    windows = kakao_chat_windows(config)
    if not windows:
        print("매칭되는 카카오톡 채팅창이 없습니다. --list-chat-windows로 창 제목을 확인하세요.")
        return

    for window in windows:
        hwnd = int(window["hwnd"])
        print(
            "CHAT "
            f"hwnd={hwnd} title={window.get('title', '')!r} "
            f"class={window.get('class', '')!r} rect={window.get('rect', '')}"
        )
        lines = dump_uia_for_hwnd(hwnd)
        if not lines:
            print("  <UIA text 없음>")
        for line in lines:
            print(f"  {line}")

        for child in _child_windows(hwnd).values():
            width, height = child.get("size", (0, 0))
            if width <= 5 or height <= 5:
                continue
            print(
                "  CHILD "
                f"hwnd={child['hwnd']} visible={child['visible']} "
                f"class={child['class']!r} title={child['title']!r} rect={child['rect']}"
            )
            child_text = win32_control_text(int(child["hwnd"]))
            if child_text:
                print(f"    WIN32_TEXT: {normalize(child_text)}")
            child_lines = dump_uia_for_hwnd(int(child["hwnd"]))
            if not child_lines:
                print("    <UIA text 없음>")
            for line in child_lines:
                print(f"    {line}")


def watch_vision(config: dict[str, Any], seconds: float, interval: float = 0.25) -> None:
    print(f"{seconds:g}초 동안 카카오톡 팝업을 캡처해 OpenAI 이미지 판독을 테스트합니다.")
    print("알림 crop 이미지가 OpenAI API로 전송됩니다. 이 명령을 켜둔 상태에서 대상자에게 카톡을 하나 보내게 해보세요.")
    process_cache: dict[int, str] = {}
    seen_signatures = {
        popup_window_signature(item)
        for item in _top_level_windows().values()
        if is_popup_event_candidate(item, process_cache)
    }
    last_capture_at = 0.0
    generator = OpenAIReplyGenerator(config)
    deadline = time.time() + seconds

    while time.time() < deadline:
        current = _top_level_windows()
        for hwnd, item in current.items():
            if not is_popup_event_candidate(item, process_cache):
                continue
            signature = popup_window_signature(item)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            now = time.monotonic()
            if now - last_capture_at < 1.5:
                continue
            last_capture_at = now

            class_name = str(item.get("class", ""))
            exe = popup_window_exe(item, process_cache)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            captured = capture_popup_burst(
                item,
                config,
                app_relative_path("data/popup_captures"),
                f"vision_test_{stamp}",
            )
            image_paths = [image_path for image_path, _label, _rect, _delay in captured]
            for image_path, label, rect, delay in captured:
                print(f"캡처 저장: {image_path} delay={delay:.2f}s label={label} class={class_name} exe={exe} rect={rect}")
            try:
                parsed = generator.generate_from_popup_images(image_paths)
            except Exception as exc:
                print(f"OpenAI 이미지 판독 실패: {exc}")
                continue
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
        time.sleep(interval)

    print("감시 종료")


def list_chat_windows(config: dict[str, Any]) -> None:
    process_cache: dict[int, str] = {}
    windows = _top_level_windows()
    count = 0
    for item in sorted(windows.values(), key=lambda value: (str(value.get("class", "")), str(value.get("title", "")))):
        exe = popup_window_exe(item, process_cache)
        if exe != "kakaotalk.exe":
            continue
        count += 1
        matched = is_kakao_chat_window(item, process_cache, config)
        print(
            f"[{'MATCH' if matched else 'SKIP'}] "
            f"hwnd={item['hwnd']} visible={item['visible']} class={item['class']!r} "
            f"title={item['title']!r} rect={item['rect']} size={item['size']}"
        )
    if count == 0:
        print("KakaoTalk.exe 창을 찾지 못했습니다. 카카오톡 채팅창을 화면에 띄워주세요.")


def watch_chat(config: dict[str, Any], seconds: float, interval: float = 2.0) -> None:
    print(f"{seconds:g}초 동안 열린 카카오톡 채팅창을 캡처해 OpenAI 이미지 판독을 테스트합니다.")
    print("대상 채팅창을 화면에 보이게 열어두세요. 창 제목이 target_senders와 매칭되는 창만 처리합니다.")
    print(f"캡처 방식: {config.get('kakao_chat_capture_method', 'screen')}")
    generator = OpenAIReplyGenerator(config)
    seen_hashes: dict[int, str] = {}
    last_api_at: dict[int, float] = {}
    deadline = time.time() + seconds
    no_window_reported = False

    while time.time() < deadline:
        windows = kakao_chat_windows(config)
        if not windows and not no_window_reported:
            no_window_reported = True
            print("매칭되는 카카오톡 채팅창이 아직 없습니다. --list-chat-windows로 창 제목을 확인할 수 있습니다.")
        for window in windows:
            capture_dir = app_relative_path("data/chat_captures")
            image_path = capture_dir / f"chat_test_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{int(window['hwnd'])}.png"
            bottom_path = capture_dir / f"chat_test_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{int(window['hwnd'])}_bottom.png"
            after_path = capture_dir / f"chat_test_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{int(window['hwnd'])}_after_me.png"
            capture_method = capture_chat_window_png(window, image_path, config)
            if image_looks_blank(image_path):
                print(
                    f"스킵: 화면 캡처가 검은 이미지입니다. "
                    f"method={capture_method} file={image_path} title={window.get('title', '')!r} rect={window_capture_rect(window)}"
                )
                print("      다음 시도: python -m pip install dxcam pillow")
                continue
            crop_image_png(image_path, bottom_path, chat_bottom_crop_rect_in_image(window, config))
            image_paths = [bottom_path, image_path]
            image_labels = ["bottom_area", "full_window"]
            yellow_y = detect_bottommost_outgoing_yellow_y(image_path)
            skip_reason = ""
            if yellow_y is not None:
                _x, _y, width, height = window_capture_rect(window)
                bottom_trim = int(config.get("kakao_chat_bottom_trim_pixels", 45))
                crop_top = min(max(yellow_y + 6, 0), max(0, height - 1))
                crop_height = max(1, height - bottom_trim - crop_top)
                if crop_height < 36:
                    skip_reason = "내 마지막 노란 말풍선 아래에 보이는 상대 메시지 영역이 없습니다."
                    print(
                        f"스킵: {skip_reason} "
                        f"title={window.get('title', '')!r} yellow_y={yellow_y} rect={window_capture_rect(window)}"
                    )
                    continue
                if crop_height >= 36:
                    crop_image_png(image_path, after_path, (0, crop_top, width, crop_height))
                    image_paths = [after_path, bottom_path, image_path]
                    image_labels = ["after_last_outgoing_area", "bottom_area", "full_window"]
            image_hash = hashlib.sha256(b"".join(path.read_bytes() for path in image_paths)).hexdigest()
            if seen_hashes.get(int(window["hwnd"])) == image_hash:
                continue
            seen_hashes[int(window["hwnd"])] = image_hash
            now = time.monotonic()
            min_interval = float(config.get("kakao_chat_min_api_interval_seconds", 8.0))
            if now - last_api_at.get(int(window["hwnd"]), 0.0) < min_interval:
                continue
            last_api_at[int(window["hwnd"])] = now
            print(
                f"캡처 저장: {image_path} bottom={bottom_path} "
                f"after={after_path if after_path in image_paths else '<none>'} "
                f"method={capture_method} "
                f"yellow_y={yellow_y} "
                f"title={window.get('title', '')!r} class={window.get('class', '')!r} rect={window_capture_rect(window)}"
            )
            try:
                parsed = generator.generate_from_chat_window_images(
                    image_paths,
                    normalize(str(window.get("title", ""))),
                    image_labels,
                )
            except Exception as exc:
                print(f"OpenAI 채팅창 판독 실패: {exc}")
                continue
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
        time.sleep(interval)

    print("감시 종료")


def test_popup() -> None:
    if tk is None or ttk is None:
        raise RuntimeError("tkinter를 사용할 수 없습니다.")
    root = tk.Tk()
    root.withdraw()
    DraftPopup(
        root,
        {
            "sender": "홍길동",
            "message": "오늘 저녁 가능해?",
            "draft": "응, 오늘 저녁 가능해. 몇 시쯤 볼까?",
        },
    )
    root.mainloop()


def load_last_draft(config: dict[str, Any]) -> dict[str, Any] | None:
    path = app_relative_path(str(config.get("message_log_path", "data/messages.jsonl")))
    if not path.exists():
        return None
    last: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") == "draft" and item.get("draft"):
                last = item
    return last


def show_last_draft(config: dict[str, Any]) -> None:
    if tk is None or ttk is None:
        raise RuntimeError("tkinter를 사용할 수 없습니다.")
    item = load_last_draft(config)
    if not item:
        print("저장된 답장 초안이 없습니다.")
        return
    root = tk.Tk()
    root.withdraw()
    generator = OpenAIReplyGenerator(config)

    def regenerate(item: dict[str, Any]) -> str:
        sender = normalize(str(item.get("sender", "")))
        message = normalize(str(item.get("message", "")))
        if not sender or not message:
            raise RuntimeError("새 답변을 만들 sender/message가 없습니다.")
        draft = generator.generate_for_message(sender, message)
        item_payload = {
            "type": "draft",
            "id": str(uuid.uuid4()),
            "created_at": now_iso(),
            "source": "manual_regenerate",
            "sender": sender,
            "message": message,
            "draft": draft,
            "notification_key": item.get("notification_key", ""),
        }
        if config.get("save_messages", True):
            append_jsonl(app_relative_path(str(config.get("message_log_path", "data/messages.jsonl"))), item_payload)
        print(f"[popup] regenerated sender={sender!r} message={message!r} draft={draft!r}")
        return draft

    print(
        "[popup] last draft "
        f"sender={item.get('sender', '')!r} "
        f"message={item.get('message', '')!r} "
        f"draft={item.get('draft', '')!r}"
    )
    DraftPopup(root, item, regenerate)
    root.mainloop()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KakaoTalk 알림 답장 초안 도우미")
    parser.add_argument("--init-config", action="store_true", help="config.json 기본값 생성")
    parser.add_argument("--list-recent", action="store_true", help="최근 Windows 알림 파싱 결과 출력")
    parser.add_argument("--list-listener", action="store_true", help="Windows UserNotificationListener 알림 출력")
    parser.add_argument("--watch-listener", type=float, metavar="SECONDS", help="Windows UserNotificationListener 새 알림 감시")
    parser.add_argument("--diagnose", action="store_true", help="설정, API 키, 최근 알림 매칭 상태 진단")
    parser.add_argument("--watch-windows", type=float, metavar="SECONDS", help="새로 생기는 Windows 창을 일정 시간 감시")
    parser.add_argument("--watch-ui", type=float, metavar="SECONDS", help="새 창의 UI Automation 텍스트를 일정 시간 감시")
    parser.add_argument("--watch-vision", type=float, metavar="SECONDS", help="카카오톡 팝업 이미지를 OpenAI로 판독 테스트")
    parser.add_argument("--list-chat-windows", action="store_true", help="카카오톡 채팅창 후보 목록 출력")
    parser.add_argument("--dump-chat-ui", action="store_true", help="대상 카카오톡 채팅창의 UI Automation 텍스트 출력")
    parser.add_argument("--watch-chat", type=float, metavar="SECONDS", help="열린 카카오톡 채팅창 이미지 판독 테스트")
    parser.add_argument("--notification-access", action="store_true", help="Windows 알림 읽기 권한 상태 확인")
    parser.add_argument("--request-notification-access", action="store_true", help="Windows 알림 읽기 권한 요청")
    parser.add_argument("--limit", type=int, default=20, help="--list-recent 출력 개수")
    parser.add_argument("--test-popup", action="store_true", help="팝업 UI 테스트")
    parser.add_argument("--show-last-draft", action="store_true", help="마지막으로 저장된 답장 초안 팝업 표시")
    return parser


def main() -> int:
    enable_dpi_awareness()
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.init_config:
        write_default_config()
        print(f"설정 파일: {CONFIG_PATH}")
        return 0

    if not CONFIG_PATH.exists():
        write_default_config()
        print(f"기본 설정 파일을 만들었습니다: {CONFIG_PATH}")
        print("target_senders와 OPENAI_API_KEY를 설정한 뒤 다시 실행하세요.")
        return 0

    config = load_config()
    if args.list_recent:
        list_recent(config, args.limit)
        return 0

    if args.list_listener:
        list_listener(config, args.limit)
        return 0

    if args.watch_listener:
        watch_listener(config, args.watch_listener)
        return 0

    if args.diagnose:
        diagnose(config, args.limit)
        return 0

    if args.notification_access:
        print(notification_listener_status())
        return 0

    if args.request_notification_access:
        print(request_notification_listener_access())
        return 0

    if args.watch_windows:
        watch_windows(args.watch_windows)
        return 0

    if args.watch_ui:
        watch_ui(args.watch_ui)
        return 0

    if args.watch_vision:
        watch_vision(config, args.watch_vision)
        return 0

    if args.list_chat_windows:
        list_chat_windows(config)
        return 0

    if args.dump_chat_ui:
        dump_chat_ui(config)
        return 0

    if args.watch_chat:
        watch_chat(config, args.watch_chat)
        return 0

    if args.test_popup:
        test_popup()
        return 0

    if args.show_last_draft:
        show_last_draft(config)
        return 0

    KakaoReplyAssistant(config).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n종료했습니다.")
        raise SystemExit(130)
