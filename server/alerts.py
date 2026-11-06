"""Motion alerts to your phone: ntfy (Android/iOS push app, free, open source) and Telegram.
Plain urllib, no extra packages. Every function returns (ok, message) and never raises."""
import json
import secrets
import urllib.parse
import urllib.request

UA = "OldPhone-CCTV"


def _ascii(s):
    return s.encode("ascii", "replace").decode("ascii")


def send_ntfy(url, title, message, jpeg=None, click=None):
    """url: full topic URL, e.g. https://ntfy.sh/my-secret-topic (or your own ntfy server)."""
    if not url:
        return False, "no ntfy topic set"
    headers = {"User-Agent": UA, "Title": _ascii(title), "Tags": "rotating_light", "Priority": "high"}
    if click:
        headers["Click"] = click
    if jpeg:
        headers.update({"Filename": "motion.jpg", "Message": _ascii(message), "Content-Type": "image/jpeg"})
        req = urllib.request.Request(url, data=jpeg, method="PUT", headers=headers)
    else:
        headers["Content-Type"] = "text/plain; charset=utf-8"
        req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300, f"ntfy {r.status}"
    except Exception as e:
        return False, f"ntfy: {e}"


def send_telegram(token, chat_id, caption, jpeg=None):
    if not token or not chat_id:
        return False, "no Telegram bot token / chat id set"
    base = f"https://api.telegram.org/bot{token}/"
    try:
        if jpeg:
            boundary = "----oldphonecctv" + secrets.token_hex(8)
            parts = []
            for k, v in (("chat_id", str(chat_id)), ("caption", caption)):
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="motion.jpg"\r\n'
                          f"Content-Type: image/jpeg\r\n\r\n").encode() + jpeg + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            req = urllib.request.Request(base + "sendPhoto", data=b"".join(parts), method="POST",
                                         headers={"User-Agent": UA,
                                                  "Content-Type": f"multipart/form-data; boundary={boundary}"})
        else:
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": caption}).encode()
            req = urllib.request.Request(base + "sendMessage", data=data, method="POST", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = json.loads(r.read() or b"{}").get("ok", False)
            return ok, "telegram ok" if ok else "telegram refused"
    except Exception as e:
        return False, f"telegram: {e}"


def send_all(alerts_cfg, title, message, jpeg=None, click=None):
    """Sends to every configured channel. Returns a list of (ok, message)."""
    results = []
    if alerts_cfg.get("ntfy_url"):
        results.append(send_ntfy(alerts_cfg["ntfy_url"], title, message, jpeg, click))
    if alerts_cfg.get("telegram_token") and alerts_cfg.get("telegram_chat"):
        results.append(send_telegram(alerts_cfg["telegram_token"], alerts_cfg["telegram_chat"],
                                     f"{title}\n{message}", jpeg))
    return results
