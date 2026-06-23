# =========================================================
# 聞き流し用・長尺短編小説 1本を生成してYouTubeへ自動投稿
# GitHub Actions（毎日1回cron）から実行する想定。
# 【二段階生成】まずタイトル＋構成（各場面のあらすじ）を作り、
#   各場面を順に本文展開 → 長さと一貫性を安定させる。
# Gemini → gTTS（音声）→ MoviePy（動画）→ YouTube API
# 横型1920x1080・落ち着いたペース・作業用BGM対応・被り防止ログつき
# =========================================================
import os, re, json, time, gc
from google import genai
try:
    from google.genai import types as genai_types
except Exception:
    genai_types = None
from gtts import gTTS
from pydub import AudioSegment
from moviepy.editor import (
    ColorClip, ImageClip, TextClip, CompositeVideoClip,
    AudioFileClip, CompositeAudioClip
)
import moviepy.config as cf
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

cf.change_settings({"IMAGEMAGICK_BINARY": "/usr/bin/convert"})

# ----- 環境変数（GitHub Secrets） -----
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
YT_CLIENT_ID     = os.environ["YT_CLIENT_ID"]
YT_CLIENT_SECRET = os.environ["YT_CLIENT_SECRET"]
YT_REFRESH_TOKEN = os.environ["YT_REFRESH_TOKEN"]

PRIVACY = os.environ.get("PRIVACY", "public")
MODEL   = os.environ.get("MODEL", "gemini-2.5-flash")

VOICE_SPEED  = 1.12        # 聞き流し向けの自然なテンポ
OUT_DIR      = "out_long"
TMP_DIR      = "tmp_long"
LOG_PATH     = "long_used_log.json"
AVOID_RECENT = 30

NUM_CHAPTERS = 7           # 構成の場面数（6〜8が目安。多いほど長尺に）
CHAPTER_CHARS = "700〜1100字"

# BGM・背景：環境変数で明示されればそれを、なければ既定ファイル名を探す。
# どちらも「ファイルがあれば使う／無ければ使わない」挙動。
BGM_PATH = os.environ.get("BGM_PATH", "").strip() or "bgm.mp3"
BGM_PATH = BGM_PATH if os.path.exists(BGM_PATH) else None
BGM_VOLUME = 0.10

BG_IMAGE = os.environ.get("BG_IMAGE", "").strip() or "bg.jpg"
BG_IMAGE = BG_IMAGE if os.path.exists(BG_IMAGE) else None
BG_COLOR = (18, 20, 28)

client = genai.Client(api_key=GEMINI_API_KEY)

W, H = 1920, 1080
FPS = 10

FONT = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"
if not os.path.exists(FONT):
    FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

TEXT_COLOR = "white"
STROKE_COLOR = "#3A4663"
STROKE_WIDTH = 6
FONT_SIZE = 60
HEADER_FONT_SIZE = 38

GENRES = [
    "少し不思議な話", "奇妙な味わいの短編", "ほろりとする人情の話",
    "幻想的な寓話", "静かなSF", "日常にひそむ小さな謎",
    "ノスタルジックな思い出の話", "旅先での不思議な出会い",
]


# ----- 被り防止ログ -----
def load_log():
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_log(log):
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)


def pick_genre(log):
    counts = {g: sum(1 for e in log if e.get("genre") == g) for g in GENRES}
    return min(GENRES, key=lambda g: counts[g])


# ----- Gemini呼び出し（JSON取得・リトライ＆モデルフォールバック） -----
def gemini_json(prompt, max_retries=5):
    models = [MODEL, "gemini-2.5-flash-lite", "gemini-3.1-flash-lite"]
    cfg = None
    if genai_types:
        cfg = genai_types.GenerateContentConfig(max_output_tokens=8192, temperature=1.05)
    for attempt in range(max_retries):
        m = models[min(attempt, len(models) - 1)]
        try:
            if cfg:
                resp = client.models.generate_content(model=m, contents=prompt, config=cfg)
            else:
                resp = client.models.generate_content(model=m, contents=prompt)
            text = resp.text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(text)
        except Exception as e:
            msg = str(e)
            if ("503" in msg or "429" in msg or "UNAVAILABLE" in msg) and attempt < max_retries - 1:
                wait = 20 * (attempt + 1)
                print(f"  Gemini混雑中… {wait}秒待って再試行 ({attempt+1}/{max_retries})")
                time.sleep(wait)
            elif attempt < max_retries - 1:
                print(f"  生成失敗（{e}）。再試行")
                time.sleep(5)
            else:
                raise


# ----- 第1段階：タイトル＋構成（各場面のあらすじ）を作る -----
def generate_outline(genre, avoid_summaries):
    avoid_text = ""
    if avoid_summaries:
        joined = "\n".join(f"- {s}" for s in avoid_summaries)
        avoid_text = f"\n\n【これらと設定・オチが被らない新作にすること】\n{joined}"
    prompt = f"""あなたはプロの短編小説家です。ジャンル「{genre}」で、
聞き流して心地よいオリジナル短編の【構成】を作ってください。

・完全な創作。実在の事件・実在の人物・特定の固有地名は使わない。
・落ち着いた語り口で、過度に怖い/刺激的な内容は避ける。
・起承転結＋最後に小さな余韻や発見がある構成に。

以下のJSON形式のみで出力（前後に説明やマークダウン不要）:
{{
  "title": "作品タイトル（20文字以内・魅力的に）",
  "summary": "全体のあらすじを1行で（被り防止ログ用・50文字以内）",
  "chapters": [
    {{"heading": "場面の小見出し（15文字以内）", "outline": "その場面の展開（80〜150字）"}}
  ]
}}
※chapters はちょうど{NUM_CHAPTERS}要素。全体で一本の物語として流れるように。{avoid_text}
"""
    data = gemini_json(prompt)
    if not data.get("chapters"):
        raise ValueError("構成（chapters）が空")
    return data


# ----- 第2段階：各場面を本文に展開する -----
def generate_chapter_prose(title, genre, summary, chapters, k, prev_tail):
    outline_text = "\n".join(
        f"{i+1}. {c.get('heading','')}：{c.get('outline','')}" for i, c in enumerate(chapters))
    this = chapters[k]
    ctx = f"\n\n直前の場面の結び（自然に続けて）：\n「{prev_tail}」" if prev_tail else ""
    prompt = f"""あなたはプロの短編小説家です。ジャンル「{genre}」の短編『{title}』を執筆中。
全体のあらすじ：{summary}

全体の構成：
{outline_text}

いまから「第{k+1}場面：{this.get('heading','')}（{this.get('outline','')}）」を、
地の文の小説として執筆してください。
・聞き流しやすい落ち着いた語り口。過度に怖い/刺激的な描写は避ける。
・前後の場面と自然につながるように。この場面で{CHAPTER_CHARS}程度。{ctx}

以下のJSON形式のみで出力（前後に説明やマークダウン不要）:
{{"scenes": ["地の文（60〜140字）", "... この場面を6〜10要素に分割"]}}
"""
    data = gemini_json(prompt)
    scenes = data.get("scenes") or []
    return [s for s in scenes if s and s.strip()]


def generate_story(genre, avoid_summaries):
    """二段階生成で1本ぶんの {title, summary, scenes} を作る"""
    outline = generate_outline(genre, avoid_summaries)
    title = outline.get("title", "無題")
    summary = outline.get("summary", "")
    chapters = outline["chapters"]
    print(f"📖 『{title}』 {len(chapters)}場面の構成ができました")

    all_scenes = []
    prev_tail = ""
    for k in range(len(chapters)):
        scenes = generate_chapter_prose(title, genre, summary, chapters, k, prev_tail)
        if not scenes:
            print(f"  ⚠️ 場面{k+1}が空。スキップ")
            continue
        all_scenes.extend(scenes)
        prev_tail = "".join(scenes[-2:])[:200]
        print(f"  ✍️ 場面{k+1}/{len(chapters)} 完成（{sum(len(s) for s in scenes)}字）")
        time.sleep(2)   # レート制限のクッション
    return {"title": title, "summary": summary, "scenes": all_scenes}


# ----- gTTS音声 -----
def make_audio(text, filename):
    if not re.search(r'[ぁ-んァ-ヴ一-龯a-zA-Z0-9０-９]', text):
        AudioSegment.silent(duration=500).export(filename, format="mp3")
        return filename
    tmp = "tmp_" + filename
    gTTS(text=text, lang="ja", slow=False).save(tmp)
    seg = AudioSegment.from_mp3(tmp)
    if VOICE_SPEED and VOICE_SPEED != 1.0:
        seg = seg.speedup(playback_speed=VOICE_SPEED)
    seg = seg + AudioSegment.silent(duration=300)
    seg.export(filename, format="mp3")
    os.remove(tmp)
    return filename


# ----- 動画パーツ -----
def make_background(duration):
    if BG_IMAGE and os.path.exists(BG_IMAGE):
        return ImageClip(BG_IMAGE).resize((W, H)).set_duration(duration)
    return ColorClip(size=(W, H), color=BG_COLOR, duration=duration)


def make_outlined_clip(text, duration, fontsize):
    common = dict(font=FONT, fontsize=fontsize, method="caption",
                  size=(W - 260, None), align="center", interline=18)
    stroke = TextClip(text, color=STROKE_COLOR, stroke_color=STROKE_COLOR,
                      stroke_width=STROKE_WIDTH, **common).set_duration(duration)
    fill = TextClip(text, color=TEXT_COLOR, **common).set_duration(duration)
    return CompositeVideoClip(
        [stroke.set_position("center"), fill.set_position("center")],
        size=stroke.size
    ).set_duration(duration)


def make_scene(text, audio_file, title, fontsize=FONT_SIZE):
    narration = AudioFileClip(audio_file)
    duration = narration.duration + 0.4
    layers = [make_background(duration)]
    header = make_outlined_clip(f"『{title}』", duration, HEADER_FONT_SIZE).set_position(("center", int(H * 0.06)))
    layers.append(header)
    main = make_outlined_clip(text, duration, fontsize)
    layers.append(main.set_position(("center", "center")))
    scene = CompositeVideoClip(layers, size=(W, H)).set_duration(duration)
    if duration > narration.duration + 0.02:
        narration = CompositeAudioClip([narration]).set_duration(duration)
    return scene.set_audio(narration)


def render_one_scene(text, audio_file, title, out_path):
    scene = make_scene(text, audio_file, title)
    scene.write_videofile(out_path, fps=FPS, codec="libx264",
                          audio_codec="aac", preset="ultrafast", logger=None)
    try:
        if scene.audio is not None:
            scene.audio.close()
    except Exception:
        pass
    scene.close(); del scene; gc.collect()


def build_video(data):
    title = data.get("title", "短編小説")
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    safe = title
    for ch in r'\/:*?"<>|':
        safe = safe.replace(ch, "")
    output_path = os.path.join(OUT_DIR, f"{safe.strip()}.mp4")

    clip_paths = []
    idx = 0

    a = make_audio(f"{title}。", f"a_{idx}.mp3")
    p = f"{TMP_DIR}/clip_{idx:04d}.mp4"
    render_one_scene(title, a, title, p)
    clip_paths.append(p); os.remove(a); idx += 1

    scenes = data["scenes"]
    for i, line in enumerate(scenes):
        print(f"  [{i+1}/{len(scenes)}] {line[:24]}...")
        a = make_audio(line, f"a_{idx}.mp3")
        p = f"{TMP_DIR}/clip_{idx:04d}.mp4"
        render_one_scene(line, a, title, p)
        clip_paths.append(p); os.remove(a); idx += 1

    a = make_audio("おしまい。ご視聴ありがとうございました。", f"a_{idx}.mp3")
    p = f"{TMP_DIR}/clip_{idx:04d}.mp4"
    render_one_scene("おしまい", a, title, p)
    clip_paths.append(p); os.remove(a); idx += 1

    print(f"  🔗 {len(clip_paths)}シーンを連結...")
    list_file = f"{TMP_DIR}/list.txt"
    with open(list_file, "w") as f:
        for cp in clip_paths:
            f.write(f"file '{os.path.basename(cp)}'\n")
    master = f"{TMP_DIR}/master.mp4"
    os.system(f'cd {TMP_DIR} && ffmpeg -y -f concat -safe 0 -i list.txt '
              f'-c:v copy -c:a aac master.mp4 -loglevel error')

    if BGM_PATH and os.path.exists(BGM_PATH):
        print("  🎵 BGMを合成...")
        os.system(
            f'ffmpeg -y -i "{master}" -stream_loop -1 -i "{BGM_PATH}" '
            f'-filter_complex "[1:a]volume={BGM_VOLUME}[b];'
            f'[0:a][b]amix=inputs=2:duration=first:dropout_transition=0[a]" '
            f'-map 0:v -map "[a]" -c:v copy -c:a aac "{output_path}" -loglevel error'
        )
    else:
        os.replace(master, output_path)

    for cp in clip_paths:
        if os.path.exists(cp):
            os.remove(cp)
    for f in [list_file, f"{TMP_DIR}/master.mp4"]:
        if os.path.exists(f):
            os.remove(f)
    return output_path, title


# ----- YouTubeアップロード -----
def get_youtube():
    creds = Credentials(
        token=None, refresh_token=YT_REFRESH_TOKEN,
        client_id=YT_CLIENT_ID, client_secret=YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload(youtube, path, title):
    description = (
        f"作業用・睡眠用にどうぞ。オリジナルの短編小説をお届けします。\n"
        f"「{title}」\n\n#短編小説 #朗読 #作業用BGM #睡眠用 #物語"
    )
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": ["短編小説", "朗読", "作業用BGM", "睡眠用", "物語", "聞き流し"],
            "categoryId": "24",
            "defaultLanguage": "ja",
        },
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(path, chunksize=10 * 1024 * 1024, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    retry = 0
    while response is None:
        try:
            status, response = req.next_chunk()
            if status:
                print(f"  ⏫ {int(status.progress()*100)}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504):
                retry += 1
                if retry > 10:
                    raise
                time.sleep(min(2 ** retry, 60))
            else:
                raise
    return response


def main():
    log = load_log()
    genre = pick_genre(log)
    print(f"📖 ジャンル：{genre}")

    avoid = [e["summary"] for e in log][-AVOID_RECENT:]
    data = generate_story(genre, avoid)
    total = sum(len(s) for s in data["scenes"])
    print(f"📝 本文 合計 約{total}字 / {len(data['scenes'])}シーン（推定 約{total/330:.0f}分）")

    path, title = build_video(data)
    print(f"🎬 生成完了：{path}")

    youtube = get_youtube()
    res = upload(youtube, path, title)
    print(f"✅ 投稿成功： https://www.youtube.com/watch?v={res['id']}")
    print(f"   公開設定：{res['status']['privacyStatus']}")

    log.append({"genre": genre, "title": data.get("title", ""),
                "summary": data.get("summary", "")})
    save_log(log)
    print(f"📖 ログ更新（計{len(log)}件）")


if __name__ == "__main__":
    main()
