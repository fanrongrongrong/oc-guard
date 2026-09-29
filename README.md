# oc-guard

OC 图防盗：盲水印 + 以图搜图巡查。

## 手机版盲水印（推荐）
直接打开 **https://fanrongrongrong.github.io/oc-guard/** ，不用装任何东西，图片只在手机本地处理、不上传。
设一个私章口令（丢了就验不出来，记在备忘录里）→ 加印 → 长按保存图片；验印时选可疑图即可。
网页版和下面的 Python 版是两套独立算法，水印不互通。

## 电脑版（Python）

## 第一次用
需要 Python 3.10+。Mac 双击 `install-mac.command`（提示"无法打开"就右键 → 打开），Windows 双击 `install-windows.bat`。
装完之后命令里的 `.venv/bin/python guard.py` 可以简写成 `./oc.sh`（Mac）或 `oc`（Windows），先 cd 到这个文件夹。

安装会自动跑 init，生成 `config.json`，**里面两个 password 要备份**（丢了之前嵌的水印就提不出来）。`registry.json`（每个水印编号对应哪张图、发给谁）也要备份。

## 盲水印
```
.venv/bin/python guard.py embed 原图.png --to "约稿群" --note "小红书发的版本"
.venv/bin/python guard.py extract 可疑图.jpg
```
- 往外发的一律发 `_wm.png` 版本，**原图不带水印自己留着**，放 `originals/` 里（提取裁剪图、巡查都要用）。
- 每次 embed 生成独立编号，发给不同的人/平台各嵌一份，泄露了能查出是哪一份。
- 实测扛得住：JPEG 压到 q50、缩放、调亮度、裁掉一圈、截图后缩小。
- 扛不住：裁成很小的头像（原图的 1/4 以下再缩到 200px 左右）、大面积涂改/重绘、AI 重绘、镜像旋转。

## 巡查
1. saucenao.com 注册，account → api 拿 key，填进 `config.json`
2. `allowlist` 填自己的账号名/主页关键词（命中的不算盗图）
3. `public_urls` 可选：填每张图的公开直链，报告里的 Google Lens / Yandex / Bing 按钮就能一键搜；不填就是打开网站手动拖图
4. 把要巡查的原图放进 `originals/`，跑 `.venv/bin/python guard.py patrol`

SauceNAO 自动搜，结果出 HTML 报告，新发现的标 NEW 并弹通知。Google Lens/Yandex 没有官方接口，只能报告里点按钮人工看。

