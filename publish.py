"""Ежедневная автопубликация постов в Instagram и Telegram.

Каждый запуск публикует в каждую сеть ОДИН следующий пост, который туда ещё
не выходил (по списку в published.json). Если публикация в одну сеть не
удалась, другая всё равно выходит, а неудавшаяся повторится при следующем
запуске.

Нужные секреты (GitHub → Settings → Secrets and variables → Actions):
  IG_ACCESS_TOKEN  — ключ Instagram (Instagram API with Instagram Login)
  TG_BOT_TOKEN     — ключ бота от @BotFather
  TG_CHAT_ID       — канал, например @innadisk
Переменная окружения GITHUB_REPOSITORY задаётся GitHub сама.
"""
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).parent
STATE = ROOT / "published.json"


def find_post_dirs():
    """Находит папки постов (01, 02, …) где угодно в хранилище.

    Папкой поста считается папка с двузначным именем, в которой есть
    instagram.txt. Если одинаковых папок несколько, берётся та, где больше файлов.
    """
    found = {}
    for txt in ROOT.rglob("instagram.txt"):
        d = txt.parent
        if not re.fullmatch(r"\d\d", d.name) or ".git" in d.parts:
            continue
        if d.name not in found or len(list(d.iterdir())) > len(list(found[d.name].iterdir())):
            found[d.name] = d
    return found


POST_DIRS = find_post_dirs()
IG_API = "https://graph.instagram.com/v21.0"
IG_CAPTION_LIMIT = 2200
TG_CAPTION_LIMIT = 1024


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def all_posts():
    return sorted(POST_DIRS)


def next_post(state, network):
    done = set(state.get(network, []))
    for name in all_posts():
        if name not in done:
            return name
    return None


def slides(post):
    return sorted(POST_DIRS[post].glob("*.jpg"))


def read(post, filename):
    f = POST_DIRS[post] / filename
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


# ---------------- Instagram ----------------

def ig_call(method, path, **params):
    params["access_token"] = os.environ["IG_ACCESS_TOKEN"]
    r = requests.request(method, f"{IG_API}/{path}", data=params if method == "POST" else None,
                         params=params if method == "GET" else None, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Instagram {path}: {r.status_code} {r.text}")
    return r.json()


def ig_wait(container_id):
    for _ in range(30):
        status = ig_call("GET", container_id, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram: контейнер {container_id} в статусе {status}")
        time.sleep(5)
    raise RuntimeError(f"Instagram: контейнер {container_id} не обработан вовремя")


def split_caption(text, limit):
    """Делит текст по абзацам: первая часть ≤ limit, остаток — для комментария."""
    if len(text) <= limit:
        return text, ""
    paras = text.split("\n\n")
    first = []
    while paras and len("\n\n".join(first + [paras[0]])) <= limit:
        first.append(paras.pop(0))
    return "\n\n".join(first), "\n\n".join(paras)


def publish_instagram(post):
    repo = os.environ["GITHUB_REPOSITORY"]
    rel = POST_DIRS[post].relative_to(ROOT).as_posix()
    base = f"https://raw.githubusercontent.com/{repo}/main/{quote(rel)}"
    user_id = ig_call("GET", "me", fields="user_id,username")["user_id"]

    # последний абзац instagram.txt — хэштеги; они всегда остаются в подписи
    body, _, hashtags = read(post, "instagram.txt").rpartition("\n\n")
    text, overflow = split_caption(body, IG_CAPTION_LIMIT - len(hashtags) - 10)
    if overflow:
        text += "\n\n(продолжение в комментарии ⬇️)"
        print(f"  подпись длиннее {IG_CAPTION_LIMIT} знаков, окончание уйдёт первым комментарием")
    caption = f"{text}\n\n{hashtags}"

    children = []
    for img in slides(post):
        c = ig_call("POST", f"{user_id}/media", image_url=f"{base}/{img.name}", is_carousel_item="true")
        children.append(c["id"])
    for cid in children:
        ig_wait(cid)

    carousel = ig_call("POST", f"{user_id}/media", media_type="CAROUSEL",
                       children=",".join(children), caption=caption)
    ig_wait(carousel["id"])
    media = ig_call("POST", f"{user_id}/media_publish", creation_id=carousel["id"])
    if overflow:
        ig_call("POST", f"{media['id']}/comments", message=overflow)
    print(f"  Instagram: опубликовано, id {media['id']}")


# ---------------- Telegram ----------------

def tg_format(text, title, bold_first=True):
    """Экранирует HTML и выделяет жирным первый абзац и абзац-заголовок."""
    paras = text.split("\n\n")
    out = []
    for i, p in enumerate(paras):
        e = html.escape(p, quote=False)
        bold = (i == 0 and bold_first) or (title and p.strip() == title)
        out.append(f"<b>{e}</b>" if bold else e)
    return "\n\n".join(out)


def tg_call(method, files=None, **data):
    token = os.environ["TG_BOT_TOKEN"]
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=60)
    if not r.ok or not r.json().get("ok"):
        raise RuntimeError(f"Telegram {method}: {r.status_code} {r.text}")
    return r.json()


def tg_chat():
    """Принимает канал в любом виде: @innadisk, innadisk, https://t.me/innadisk, -100…"""
    chat = os.environ["TG_CHAT_ID"].strip()
    chat = re.sub(r"^(https?://)?(www\.)?(t|telegram)\.me/", "", chat).strip("/ ")
    if not chat.startswith("@") and not re.fullmatch(r"-?\d+", chat):
        chat = "@" + chat
    return chat


def publish_telegram(post):
    chat = tg_chat()
    folder_name = read(post, "source_folder.txt")
    title = folder_name.split(" — ", 1)[1] if " — " in folder_name else ""
    part1 = tg_format(read(post, "telegram_1.txt"), title)
    raw2 = read(post, "telegram_2.txt")
    part2 = tg_format(raw2, title, bold_first=False) if raw2 else ""
    cover = slides(post)[0]
    with open(cover, "rb") as f:
        tg_call("sendPhoto", files={"photo": f}, chat_id=chat, caption=part1, parse_mode="HTML")
    if part2:
        tg_call("sendMessage", chat_id=chat, text=part2, parse_mode="HTML")
    extra = read(post, "telegram_3.txt")  # необязательная доп. информация
    if extra:
        tg_call("sendMessage", chat_id=chat, text=html.escape(extra, quote=False), parse_mode="HTML")
    print("  Telegram: опубликовано")


# ---------------- main ----------------

def hide_secrets(text):
    """Убирает ключи из текста ошибки: он сохраняется в открытый файл last_run.txt."""
    for name in ("IG_ACCESS_TOKEN", "TG_BOT_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            text = text.replace(value, "***")
    return re.sub(r"bot\d+:[\w-]+", "bot***", text)


def which_network(raw):
    """Понимает слово из поля «network» в любом написании; None — если непонятно."""
    w = re.sub(r"[^a-zа-яё]", "", raw.lower())
    # набрано латиницей в русской раскладке («еудупкфь» = telegram)
    ru, en = "йцукенгшщзхъфывапролджэячсмитьбю", "qwertyuiop[]asdfghjkl;'zxcvbnm,."
    w_lat = "".join(en[ru.index(c)] if c in ru else c for c in w)
    # слово ищется в любом месте поля: «alltelegram» (дописали к all) = telegram
    for cand in (w, w_lat):
        tg = any(k in cand for k in ("tel", "тел", "tg", "тг"))
        ig = any(k in cand for k in ("ins", "инс", "ig"))
        if tg and ig:
            return "all"
        if tg:
            return "telegram"
        if ig:
            return "instagram"
        if cand in ("", "all", "все", "всё", "both", "оба"):
            return "all"
    return None


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else "all"
    log = [f"поле network: {raw!r}"]
    only = which_network(raw)
    if only is None:
        log.append("ОШИБКА: не понял, куда публиковать. Напишите telegram, instagram или all.")
        (ROOT / "last_run.txt").write_text(
            time.strftime("%d.%m.%Y %H:%M UTC\n", time.gmtime()) + "\n".join(log) + "\n", encoding="utf-8")
        sys.exit(log[-1])
    state = load_state()
    errors = []
    for network, func in (("instagram", publish_instagram), ("telegram", publish_telegram)):
        if only not in ("all", network):
            continue
        post = next_post(state, network)
        if not post:
            print(f"{network}: все посты уже опубликованы")
            continue
        print(f"{network}: публикую пост {post} ({read(post, 'source_folder.txt')})")
        try:
            func(post)
            state.setdefault(network, []).append(post)
            save_state(state)
        except Exception as e:  # noqa: BLE001
            msg = hide_secrets(str(e))
            print(f"  ОШИБКА: {msg}")
            log.append(f"{network}, пост {post}: ОШИБКА {msg}")
            errors.append(network)
        else:
            log.append(f"{network}, пост {post}: опубликовано")
    (ROOT / "last_run.txt").write_text(
        time.strftime("%d.%m.%Y %H:%M UTC\n", time.gmtime()) + "\n".join(log) + "\n", encoding="utf-8")
    if errors:
        sys.exit(f"Не удалось опубликовать: {', '.join(errors)}")


if __name__ == "__main__":
    main()
