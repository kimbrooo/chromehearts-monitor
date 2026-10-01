"""크롬하츠 홈페이지 + 메뉴 페이지 감시 프로그램 (2판).

알려주는 것:
1) 새 메뉴 / 새 페이지
2) 메뉴 페이지에 새로 올라온 상품
3) 품절이었던 상품이 다시 살아난 것 (재입고)
4) 사라졌던 링크가 다시 나타난 것

매번 "지난번에 본 그대로"와 비교하기 때문에, 사라졌다가 다시 생기면 다시 알려줍니다.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

SITE = "https://www.chromehearts.com/"
STATE_FILE = "state.json"
MAX_PAGES = 30
SKIP_WORDS = ("login", "locations", "magazine", "cart", "account", "wishlist")
SOLD_OUT_RE = re.compile(r"sold\s*-?\s*out", re.I)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


class LinkParser(HTMLParser):
    """링크와, 각 링크 바로 뒤에 붙은 글자(품절 표시 등)를 모읍니다."""

    def __init__(self):
        super().__init__()
        self.names = {}     # 주소 -> 이름
        self.segments = {}  # 주소 -> 그 링크 뒤에 이어진 글자 전부
        self._current = None
        self._in_a = False
        self._name_buf = []
        self._skip = 0  # script/style 안의 글자는 무시

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            self._in_a = True
            self._name_buf = []
            if href and not href.startswith(("javascript:", "#", "mailto:", "tel:")):
                full = urllib.parse.urljoin(SITE, href).split("#")[0].split("?")[0]
                self._current = full if full.startswith(SITE) else None
            else:
                self._current = None
            if self._current:
                self.names.setdefault(self._current, "")
                self.segments.setdefault(self._current, "")

    def handle_data(self, data):
        if self._skip:
            return
        d = data.strip()
        if not d:
            return
        if self._in_a:
            self._name_buf.append(d)
        if self._current:
            self.segments[self._current] += " " + d

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag == "a":
            if self._current and not self.names[self._current]:
                self.names[self._current] = " ".join(self._name_buf)
            self._in_a = False
            # _current 는 다음 링크가 시작될 때까지 유지 → 링크 뒤의 "Sold Out" 글자도 잡음

    def result(self):
        out = {}
        for url, name in self.names.items():
            sold = bool(SOLD_OUT_RE.search(self.segments.get(url, "")))
            out[url] = {"t": name, "s": sold}
        return out


def fetch_links(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    parser = LinkParser()
    parser.feed(html)
    return parser.result()


def is_menu_page(url):
    path = url[len(SITE):].lower()
    if not path or path.endswith(".html"):
        return False
    return not any(w in path for w in SKIP_WORDS)


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("텔레그램 설정이 없어 메시지를 보내지 못했습니다:\n" + message)
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": message[:3900]}).encode()
    urllib.request.urlopen(url, data=data, timeout=30)


ALERT_REPEAT = 10  # 알림을 몇 번 연속으로 보낼지 (자고 있어도 깨도록)


def send_alert(message):
    """중요한 알림은 여러 번 연속으로 보냅니다."""
    for i in range(1, ALERT_REPEAT + 1):
        try:
            send_telegram(f"[{i}/{ALERT_REPEAT}] " + message)
        except Exception as e:
            print(f"{i}번째 알림 전송 실패: {e}")
        if i < ALERT_REPEAT:
            time.sleep(3)  # 텔레그램이 너무 빠른 전송을 막지 않도록 간격을 둠


def crawl():
    result = {}
    home = fetch_links(SITE)
    if len(home) < 3:
        raise RuntimeError("홈페이지 링크가 너무 적게 읽혔습니다. 사이트가 막았을 수 있습니다.")
    result[SITE] = home

    queue = [u for u in home if is_menu_page(u)]
    while queue and len(result) <= MAX_PAGES:
        page = queue.pop(0)
        time.sleep(1.5)
        try:
            result[page] = fetch_links(page)
        except Exception as e:
            print(f"메뉴 페이지 읽기 실패(건너뜀): {page} ({e})")
    return result


def load_previous():
    with open(STATE_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    prev = {}
    for page, links in raw.items():
        if isinstance(links, list):  # 예전 형식 호환
            prev[page] = {u: {"t": "", "s": False} for u in links}
        else:
            prev[page] = links
    return prev


def describe(url, info):
    name = info.get("t") or "(이름없음)"
    tag = " [품절]" if info.get("s") else ""
    return f"{name}{tag}\n  {url}"


def main():
    try:
        current = crawl()
    except Exception as e:
        print(f"접속 실패: {e}")
        sys.exit(1)

    if not os.path.exists(STATE_FILE):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        total = sum(len(v) for v in current.values())
        sold = sum(1 for v in current.values() for i in v.values() if i["s"])
        send_telegram(
            "✅ 크롬하츠 감시를 시작했습니다.\n"
            f"페이지 {len(current)}개, 링크 {total}개를 기억했습니다.\n"
            f"그중 '품절'로 읽힌 상품: {sold}개\n"
            "(실제 사이트의 품절 상품 수와 비교해 보세요.)"
        )
        print("처음 실행: 기준 목록 저장 완료")
        return

    previous = load_previous()
    messages = []
    new_state = {}

    for page, links in current.items():
        prev_links = previous.get(page)
        if prev_links is None:
            messages.append(f"🆕 새 페이지 발견: {page}")
            new_state[page] = links
            continue
        # 페이지가 덜 읽힌 경우(링크가 절반 미만)는 오류로 보고 이번엔 건너뜀
        if len(prev_links) >= 6 and len(links) < len(prev_links) * 0.5:
            print(f"링크가 갑자기 줄어 건너뜀: {page}")
            new_state[page] = prev_links
            continue

        where = "홈페이지" if page == SITE else page
        added, restocked, soldout_now = [], [], []
        for url, info in links.items():
            old = prev_links.get(url)
            if old is None:
                added.append(describe(url, info))
            elif old.get("s") and not info["s"]:
                restocked.append(describe(url, info))
            elif not old.get("s") and info["s"]:
                soldout_now.append(info.get("t") or url)

        if added:
            messages.append(f"🚨 {where} 새 링크 {len(added)}개\n" + "\n".join("• " + a for a in added[:15]))
        if restocked:
            messages.append(f"🔥 재입고 (품절 → 구매가능) {len(restocked)}개\n" + "\n".join("• " + r for r in restocked[:15]))
        if soldout_now:
            print(f"품절로 바뀜(알림 없음): {soldout_now}")
        new_state[page] = links

    # 이번에 못 읽은 페이지는 이전 기록 유지
    for page, links in previous.items():
        new_state.setdefault(page, links)

    if messages:
        send_alert("크롬하츠 변화 감지!\n\n" + "\n\n".join(messages))
        print(f"알림 전송: {len(messages)}건")
    else:
        print("변화 없음")

    if new_state != previous:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
