#!/usr/bin/env python3
"""OC 防盗工具：盲水印嵌入/提取 + 以图搜图巡查。

  python guard.py init                               # 生成密钥和配置
  python guard.py embed 图.png --to "发给谁" [-o out]  # 嵌入盲水印（每个接收人一个独立编号）
  python guard.py extract 可疑图.jpg                  # 提取水印、查是哪张/发给谁的
  python guard.py patrol                             # 巡查 originals/ 里的图，出 HTML 报告
"""
import argparse
import datetime as dt
import hashlib
import html
import itertools
import json
import os
import secrets
import struct
import subprocess
import sys
import time
import urllib.parse
import zlib
from pathlib import Path

import cv2
import numpy as np
import requests
from blind_watermark import WaterMark, bw_notes

bw_notes.close()  # 关掉库自带的打印
cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
REGISTRY = ROOT / "registry.json"
SEEN = ROOT / "seen_hits.json"
ORIGINALS = ROOT / "originals"
REPORTS = ROOT / "reports"

MAGIC = b"OG"
PAYLOAD_BITS = 64  # 2 字节 magic + 4 字节编号 + 2 字节校验，越短越抗压缩
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def load(path, default):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def config():
    if not CONFIG.exists():
        sys.exit("先运行 python guard.py init")
    return load(CONFIG, {})


# ---------- 盲水印 ----------

def make_payload(wm_id: int) -> list[bool]:
    body = MAGIC + struct.pack(">I", wm_id)
    raw = body + struct.pack(">H", zlib.crc32(body) & 0xFFFF)
    return [b == "1" for b in "".join(f"{x:08b}" for x in raw)]


def parse_payload(bits) -> int | None:
    raw = bytes(int("".join("1" if b > 0.5 else "0" for b in bits[i:i + 8]), 2)
                for i in range(0, PAYLOAD_BITS, 8))
    body, crc = raw[:6], struct.unpack(">H", raw[6:])[0]
    if body[:2] != MAGIC or zlib.crc32(body) & 0xFFFF != crc:
        return None
    return struct.unpack(">I", body[2:])[0]


def wm(cfg):
    return WaterMark(password_img=cfg["password_img"], password_wm=cfg["password_wm"])


def cmd_init(_):
    if CONFIG.exists():
        sys.exit("config.json 已存在。别重新生成——换了密钥之前嵌的水印就全提不出来了。")
    save(CONFIG, {
        "password_img": secrets.randbelow(2**31),
        "password_wm": secrets.randbelow(2**31),
        "saucenao_api_key": "",
        "min_similarity": 70,
        "allowlist": ["填你自己的主页/账号关键词，命中的结果不算盗图"],
        "public_urls": {"示例.png": "https://你发过这张图的公开直链（给 Google Lens / Yandex 用）"},
    })
    ORIGINALS.mkdir(exist_ok=True)
    print("已生成 config.json 和 originals/。config.json 里的两个密码一定要备份，丢了水印就提不出来。")


def cmd_embed(args):
    cfg = config()
    src = Path(args.image)
    img = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
    if img is None:
        sys.exit(f"读不了 {src}")
    h, w = img.shape[:2]
    if h * w < 256 * 256:
        sys.exit("图太小了（<256×256），盲水印塞不下")

    reg = load(REGISTRY, {})
    wm_id = secrets.randbelow(2**32)
    while str(wm_id) in reg:
        wm_id = secrets.randbelow(2**32)

    out = Path(args.output) if args.output else src.with_name(f"{src.stem}_wm{args.suffix or ''}.png")
    bwm = wm(cfg)
    bwm.read_img(img=img)
    bwm.read_wm(make_payload(wm_id), mode="bit")
    bwm.embed(str(out))

    marked = cv2.imread(str(out), cv2.IMREAD_UNCHANGED)
    psnr = cv2.PSNR(img[..., :3].astype(np.uint8), marked[..., :3].astype(np.uint8))
    reg[str(wm_id)] = {
        "source": src.name,
        "source_path": str(src.resolve()),
        "output": out.name,
        "to": args.to or "",
        "note": args.note or "",
        "date": dt.date.today().isoformat(),
        "size": [w, h],
        "sha256_original": hashlib.sha256(src.read_bytes()).hexdigest(),
    }
    save(REGISTRY, reg)
    print(f"✓ {out}  编号 {wm_id:08x}  画质 PSNR {psnr:.1f}dB（>35 肉眼基本看不出）")


def try_extract(cfg, img, size):
    if (img.shape[1], img.shape[0]) != tuple(size):
        img = cv2.resize(img, tuple(size), interpolation=cv2.INTER_CUBIC)
    try:
        bits = wm(cfg).extract(embed_img=img, wm_shape=PAYLOAD_BITS, mode="bit")
    except Exception:
        return None
    return parse_payload(bits)


def cmd_extract(args):
    cfg = config()
    reg = load(REGISTRY, {})
    img = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if img is None:
        sys.exit(f"读不了 {args.image}")
    # 先按原尺寸试，再按登记过的每个尺寸缩放回去试（应对被压缩/缩放过的图）
    sizes = [(img.shape[1], img.shape[0])] + sorted({tuple(v["size"]) for v in reg.values()})
    for size in dict.fromkeys(sizes):
        wm_id = try_extract(cfg, img, size)
        if wm_id is not None:
            info = reg.get(str(wm_id))
            print(f"✓ 找到水印，编号 {wm_id:08x}")
            if info:
                for k, label in [("source", "原图"), ("to", "发给"), ("date", "日期"), ("note", "备注"),
                                 ("sha256_original", "原图SHA256")]:
                    if info.get(k):
                        print(f"  {label}: {info[k]}")
            else:
                print("  （校验通过，但本机 registry.json 里没有这条记录）")
            return
    # 还不行就当成截图/裁剪：拿本机原图做模板匹配，把可疑图贴回原位再提
    for wm_id, v in find_by_crop(cfg, img, reg):
        print(f"✓ 找到水印（从裁剪/截图中恢复），编号 {wm_id:08x}")
        for k, label in [("source", "原图"), ("to", "发给"), ("date", "日期"), ("note", "备注")]:
            if v.get(k):
                print(f"  {label}: {v[k]}")
        return
    print("✗ 没提取到水印（没嵌过 / 裁得太狠或涂改太多 / 密钥不对 / 本机找不到原图）")


def locate(orig_gray, sus_gray):
    """在原图里找可疑图的位置和缩放，返回 (score, x, y, w, h)，坐标按原图算。"""
    d = min(1.0, 500 / max(orig_gray.shape))
    small = cv2.resize(orig_gray, None, fx=d, fy=d, interpolation=cv2.INTER_AREA)
    best = (-1, 0, 0, 0, 0)
    sh, sw = sus_gray.shape
    max_k = min(small.shape[0] / sh, small.shape[1] / sw)
    for k in np.linspace(max_k * 0.15, max_k, 80):
        tw, th = round(sw * k), round(sh * k)
        if tw < 24 or th < 24:
            continue
        t = cv2.resize(sus_gray, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(small, t, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        if score > best[0]:
            best = (score, x / d, y / d, tw / d, th / d)
    if best[0] < 0.5 or d == 1.0:
        return best
    # 粗搜在缩小图上做的，差几个像素水印就对不上，这里在原分辨率上局部精修
    _, x0, y0, w0, _ = best
    pad = round(2 / d) + 4
    x1, y1 = max(0, round(x0) - pad), max(0, round(y0) - pad)
    fine = (-1, 0, 0, 0, 0)
    for k in np.linspace(w0 / sw * 0.97, w0 / sw * 1.03, 31):
        tw, th = round(sw * k), round(sh * k)
        roi = orig_gray[y1:round(y0) + th + pad, x1:round(x0) + tw + pad]
        if roi.shape[0] < th or roi.shape[1] < tw:
            continue
        res = cv2.matchTemplate(roi, cv2.resize(sus_gray, (tw, th), interpolation=cv2.INTER_CUBIC),
                                cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        if score > fine[0]:
            fine = (score, x1 + x, y1 + y, tw, th)
    return fine if fine[0] > 0 else best


def find_by_crop(cfg, img, reg):
    sus_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    for wm_id, v in reg.items():
        path = Path(v.get("source_path", ""))
        if not path.exists():
            path = ORIGINALS / v["source"]
        orig = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if orig is None:
            continue
        score, x, y, w, h = locate(cv2.cvtColor(orig, cv2.COLOR_BGR2GRAY), sus_gray)
        if score < 0.6:
            continue
        # 水印按像素块对齐，差 1px 就提不出，所以在定位结果周围逐个微调
        x, y, w, h = map(round, (x, y, w, h))
        tries = sorted(itertools.product((-1, 0, 1), repeat=4), key=lambda t: sum(map(abs, t)))
        for dx, dy, dw, dh in tries:
            cx, cy, cw, ch = max(0, x + dx), max(0, y + dy), w + dw, h + dh
            canvas = np.zeros_like(orig)
            patch = cv2.resize(img, (cw, ch), interpolation=cv2.INTER_CUBIC)
            canvas[cy:cy + ch, cx:cx + cw] = patch[:orig.shape[0] - cy, :orig.shape[1] - cx]
            found = try_extract(cfg, canvas, (orig.shape[1], orig.shape[0]))
            if found is not None and str(found) in reg:
                yield found, reg[str(found)]
                return


# ---------- 以图搜图巡查 ----------

def shrink_for_upload(path: Path) -> bytes:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    scale = 1200 / max(img.shape[:2])
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def saucenao(cfg, path: Path):
    r = requests.post(
        "https://saucenao.com/search.php",
        params={"output_type": 2, "api_key": cfg["saucenao_api_key"], "db": 999, "numres": 16},
        files={"file": ("q.jpg", shrink_for_upload(path), "image/jpeg")},
        timeout=60,
    )
    if r.status_code == 429:
        raise RuntimeError("SauceNAO 额度用完了（免费号每天约 100 次），明天再跑")
    r.raise_for_status()
    hits = []
    for res in r.json().get("results", []):
        sim = float(res["header"]["similarity"])
        if sim < cfg["min_similarity"]:
            continue
        d = res.get("data", {})
        urls = d.get("ext_urls") or ([d["source"]] if str(d.get("source", "")).startswith("http") else [])
        who = d.get("member_name") or d.get("author_name") or d.get("creator") or ""
        hits.append({
            "similarity": sim,
            "url": urls[0] if urls else "",
            "site": res["header"].get("index_name", ""),
            "who": who if isinstance(who, str) else ", ".join(who),
            "thumb": res["header"].get("thumbnail", ""),
        })
    return hits


def allowed(cfg, hit):
    text = f"{hit['url']} {hit['who']}".lower()
    return any(a.lower() in text for a in cfg.get("allowlist", []) if a and not a.startswith("填"))


def manual_links(cfg, name):
    url = cfg.get("public_urls", {}).get(name, "")
    if not url.startswith("http"):
        return {"Google Lens": "https://lens.google.com/", "Yandex": "https://yandex.com/images/",
                "Bing": "https://www.bing.com/visualsearch"}, False
    q = urllib.parse.quote(url, safe="")
    return {
        "Google Lens": f"https://lens.google.com/uploadbyurl?url={q}",
        "Yandex": f"https://yandex.com/images/search?rpt=imageview&url={q}",
        "Bing": f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{q}",
    }, True


def cmd_patrol(args):
    cfg = config()
    if not cfg.get("saucenao_api_key"):
        sys.exit("config.json 里 saucenao_api_key 还没填（saucenao.com 注册后在 account → api 里拿）")
    images = sorted(p for p in ORIGINALS.iterdir() if p.suffix.lower() in IMG_EXT)
    if not images:
        sys.exit("originals/ 里没图")
    seen = set(load(SEEN, []))
    rows, new_count = [], 0
    for i, p in enumerate(images):
        print(f"[{i + 1}/{len(images)}] {p.name}", flush=True)
        try:
            hits = [h for h in saucenao(cfg, p) if not allowed(cfg, h)]
            err = ""
        except Exception as e:
            hits, err = [], str(e)
        for h in hits:
            h["new"] = h["url"] not in seen
            new_count += h["new"]
            seen.add(h["url"])
        rows.append((p, hits, err))
        if i < len(images) - 1:
            time.sleep(6)  # 免费号限速 30 秒 6 次
    save(SEEN, sorted(seen))

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"patrol_{dt.datetime.now():%Y%m%d_%H%M}.html"
    out.write_text(render_report(cfg, rows, new_count), "utf-8")
    print(f"报告：{out}（新发现 {new_count} 条）")
    if new_count and sys.platform == "darwin":
        subprocess.run(["osascript", "-e",
                        f'display notification "发现 {new_count} 条新的疑似盗图" with title "OC 巡查"'])
    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", str(out)])


def render_report(cfg, rows, new_count):
    e = html.escape
    cards = []
    for p, hits, err in rows:
        links, direct = manual_links(cfg, p.name)
        link_html = " ".join(f'<a class="btn" href="{e(u)}" target="_blank">{e(n)}</a>' for n, u in links.items())
        if not direct:
            link_html += '<span class="muted">（没填公开链接，点开后手动拖图进去）</span>'
        if err:
            body = f'<p class="err">SauceNAO 出错：{e(err)}</p>'
        elif hits:
            body = "".join(
                f'<div class="hit{" new" if h["new"] else ""}">'
                f'<img src="{e(h["thumb"])}" loading="lazy">'
                f'<div><b>{h["similarity"]:.0f}%</b> {"<span class=tag>NEW</span>" if h["new"] else ""}<br>'
                f'{e(h["site"])}<br>{e(h["who"])}<br>'
                f'<a href="{e(h["url"])}" target="_blank">{e(h["url"][:80])}</a></div></div>'
                for h in sorted(hits, key=lambda h: (-h["new"], -h["similarity"])))
        else:
            body = '<p class="muted">SauceNAO 没搜到可疑结果</p>'
        cards.append(
            f'<section><div class="head"><img class="orig" src="../originals/{e(urllib.parse.quote(p.name))}">'
            f'<div><h2>{e(p.name)}</h2><div class="links">{link_html}</div></div></div>{body}</section>')
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OC 巡查报告</title>
<style>
:root{{--bg:#faf9f7;--card:#fff;--fg:#222;--muted:#888;--line:#e6e3de;--accent:#c2410c;--newbg:#fff4ec}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16161a;--card:#1f1f24;--fg:#eee;--muted:#999;--line:#333;--accent:#fb923c;--newbg:#2a1d14}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,"PingFang SC",sans-serif;max-width:900px;margin:0 auto;padding:24px 16px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}}
.head{{display:flex;gap:16px;align-items:center}} .orig{{width:96px;height:96px;object-fit:cover;border-radius:8px}}
h2{{font-size:16px;margin:0 0 8px}} a{{color:var(--accent);word-break:break-all}}
.btn{{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:2px 10px;margin:2px 4px 2px 0;text-decoration:none}}
.hit{{display:flex;gap:12px;padding:10px;border-top:1px solid var(--line);margin-top:10px}} .hit img{{width:80px;height:80px;object-fit:cover;border-radius:6px}}
.hit.new{{background:var(--newbg);border-radius:8px}} .tag{{background:var(--accent);color:#fff;border-radius:4px;padding:0 6px;font-size:12px}}
.muted{{color:var(--muted)}} .err{{color:#dc2626}}
</style>
<h1>OC 巡查报告</h1><p class="muted">{dt.datetime.now():%Y-%m-%d %H:%M} · {len(rows)} 张图 · 新发现 {new_count} 条</p>
{''.join(cards)}</html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    p = sub.add_parser("embed")
    p.add_argument("image")
    p.add_argument("-o", "--output")
    p.add_argument("--to", help="这份发给谁（泄露时能查出是谁的那份）")
    p.add_argument("--note")
    p.add_argument("--suffix", help="输出文件名后缀")
    p.set_defaults(fn=cmd_embed)
    p = sub.add_parser("extract")
    p.add_argument("image")
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("patrol")
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(fn=cmd_patrol)
    args = ap.parse_args()
    os.chdir(ROOT)
    args.fn(args)


if __name__ == "__main__":
    main()
