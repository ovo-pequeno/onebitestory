# =========================================================
# スカッと/意味怖 Shorts 1本を生成してYouTubeへ自動投稿
# GitHub Actions（毎日cron）から実行される想定。
# Gemini（お題＆本文）→ gTTS（音声）→ MoviePy（動画）→ YouTube API
# 設定はすべて環境変数（GitHub Secrets）から受け取る。
# =========================================================
import os, re, json, time
from google import genai
from gtts import gTTS
from pydub import AudioSegment
from moviepy.editor import (
    ColorClip, ImageClip, TextClip, CompositeVideoClip,
    AudioFileClip, CompositeAudioClip, concatenate_videoclips, afx
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

GEN_TYPE = os.environ.get("GEN_TYPE", "ミックス")   # "ミックス"/"スカッと"/"意味怖"
PRIVACY  = os.environ.get("PRIVACY", "public")      # public/unlisted/private
MODEL    = os.environ.get("MODEL", "gemini-2.5-flash")

VOICE_SPEED  = 1.4
OUT_DIR      = "out"
LOG_PATH     = "used_log.json"     # リポジトリにコミットして永続化
AVOID_RECENT = 40

client = genai.Client(api_key=GEMINI_API_KEY)

W, H = 1080, 1920
FPS = 10

FONT_SERIF = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"
if not os.path.exists(FONT_SERIF):
    FONT_SERIF = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_POP = "/usr/share/fonts/truetype/custom/MochiyPopOne-Regular.ttf"
if not os.path.exists(FONT_POP):
    FONT_POP = FONT_SERIF

THEME = {
    "意味怖": dict(label="意味が分かると怖い話", font=FONT_SERIF,
                 bg_color=(12, 12, 16), stroke="#AA1E1E"),
    "スカッと": dict(label="スカッとする話", font=FONT_POP,
                  bg_color=(18, 52, 86), stroke="#2E9BD6"),
}
META = {
    "意味怖": dict(
        title_tag="【意味怖】",
        description="意味が分かると怖い話。あなたは気づけますか？\n\n#意味が分かると怖い話 #意味怖 #怖い話 #ゾッとする話 #Shorts",
        tags=["意味が分かると怖い話", "意味怖", "怖い話", "ゾッとする話", "考察"]),
    "スカッと": dict(
        title_tag="【スカッと】",
        description="スカッとする話をお届け。\n\n#スカッとする話 #スカッと #因果応報 #Shorts",
        tags=["スカッとする話", "スカッと", "因果応報", "スカッと話"]),
}

TEXT_COLOR = "white"
MAIN_STROKE_WIDTH = 10
FONT_SIZE = 72
HEADER_FONT_SIZE = 44
HEADER_Y = 0.02
CATEGORY_ID = "24"   # エンタメ


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


def pick_type(log):
    if GEN_TYPE in ("スカッと", "意味怖"):
        return GEN_TYPE
    # ミックス：これまでの本数が少ない方を選んでバランスをとる
    k = sum(1 for e in log if e.get("type") == "意味怖")
    s = sum(1 for e in log if e.get("type") == "スカッと")
    return "意味怖" if k <= s else "スカッと"


# ----- Geminiでお題＋本文を生成 -----
def generate_story(story_type, avoid_summaries, max_retries=5):
    models = [MODEL, "gemini-2.5-flash-lite", "gemini-3.1-flash-lite"]
    avoid_text = ""
    if avoid_summaries:
        joined = "\n".join(f"- {s}" for s in avoid_summaries)
        avoid_text = f"\n\n【これらと内容・オチが被らない新作にすること】\n{joined}"

    if story_type == "意味怖":
        rule = """あなたは「意味が分かると怖い話」のプロ作家です。
一見ふつうの短い話だが、よく読むとゾッとする「隠された意味」がある話を1つ創作してください。
・完全な創作。実在の事件・実在の人物・特定地名は使わない。
・露骨なグロ・暴力描写は避け、想像力でゾッとさせる。
・最後の解説で「実は…」と種明かしする。"""
        extra = '"reveal": "オチの解説。「実は…」で始める（70文字以内）",'
    else:
        rule = """あなたは「スカッとする話」のプロ作家です。
理不尽な相手や嫌な状況が、最後に痛快に逆転・因果応報になる話を1つ創作してください。
・完全な創作。実在の事件・人物・企業は使わない。
・過度な暴力や違法な仕返しはNG。あくまで痛快でスッキリする結末に。
・最後に「スカッと」する逆転のヤマ場を持ってくる。"""
        extra = '"reveal": "スカッとする逆転・結末のヤマ場（70文字以内）",'

    prompt = f"""{rule}

以下のJSON形式のみで出力（前後に説明やマークダウン不要）:
{{
  "youtube_title": "思わずタップしたくなるタイトル（25文字以内）",
  "summary": "この話の要約を1行で（被り防止ログ用・40文字以内）",
  "hook": "冒頭の掴み・引き込む一文（30文字以内）",
  "story_lines": ["本文を短く区切った1コマ（40文字以内）", "（3〜5コマに分ける）"],
  {extra}
  "ending": "視聴者への締め・コメント誘導（25文字以内）"
}}
※story_lines は3〜5要素。各40文字以内。
※全体で読み上げ40〜60秒になる分量に。{avoid_text}
"""
    for attempt in range(max_retries):
        m = models[min(attempt, len(models) - 1)]
        try:
            resp = client.models.generate_content(model=m, contents=prompt)
            text = resp.text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(text)
        except Exception as e:
            msg = str(e)
            if ("503" in msg or "429" in msg or "UNAVAILABLE" in msg) and attempt < max_retries - 1:
                wait = 20 * (attempt + 1)
                print(f"Gemini混雑中… {wait}秒待って再試行 ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                raise


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
    seg = seg + AudioSegment.silent(duration=200)
    seg.export(filename, format="mp3")
    os.remove(tmp)
    return filename


# ----- 動画パーツ -----
def make_background(duration, theme):
    return ColorClip(size=(W, H), color=theme["bg_color"], duration=duration)


def make_outlined_clip(text, duration, fontsize, font, stroke_color,
                       stroke_width=MAIN_STROKE_WIDTH, interline=12):
    common = dict(font=font, fontsize=fontsize, method="caption",
                  size=(W - 90, None), align="center", interline=interline)
    shadow = (TextClip(text, color="black", stroke_color="black",
                       stroke_width=stroke_width + 4, **common)
              .set_duration(duration).set_opacity(0.5))
    stroke = TextClip(text, color=stroke_color, stroke_color=stroke_color,
                      stroke_width=stroke_width, **common).set_duration(duration)
    fill = TextClip(text, color=TEXT_COLOR, **common).set_duration(duration)
    return CompositeVideoClip(
        [shadow.set_position(("center", 6)),
         stroke.set_position("center"),
         fill.set_position("center")],
        size=stroke.size
    ).set_duration(duration)


def make_scene(text, audio_file, theme, force_duration=None, fontsize=FONT_SIZE):
    narration = AudioFileClip(audio_file)
    duration = force_duration if force_duration else narration.duration + 0.4
    layers = [make_background(duration, theme)]

    # 本文（画面中央）
    main = make_outlined_clip(text, duration, fontsize, theme["font"], theme["stroke"])
    layers.append(main.set_position(("center", "center")))

    # ヘッダー（上部に小さく1行で固定）。本文と被らないよう画面上から7%に置く
    header = make_outlined_clip(theme["label"], duration, HEADER_FONT_SIZE,
                                theme["font"], theme["stroke"],
                                stroke_width=8)
    layers.append(header.set_position(("center", int(H * 0.07))))

    scene = CompositeVideoClip(layers, size=(W, H)).set_duration(duration)
    if duration > narration.duration + 0.02:
        narration = CompositeAudioClip([narration]).set_duration(duration)
    return scene.set_audio(narration)


def build_video(data, story_type):
    theme = THEME[story_type]
    os.makedirs(OUT_DIR, exist_ok=True)
    safe = (META[story_type]["title_tag"] + (data.get("youtube_title") or story_type))
    for ch in r'\/:*?"<>|':
        safe = safe.replace(ch, "")
    output_path = os.path.join(OUT_DIR, f"{safe.strip()}.mp4")

    scenes = []
    n = 0
    a = make_audio(data["hook"], f"a_{n}.mp3"); n += 1
    scenes.append(make_scene(data["hook"], a, theme))
    for line in data["story_lines"]:
        a = make_audio(line, f"a_{n}.mp3"); n += 1
        scenes.append(make_scene(line, a, theme))
    if story_type == "意味怖":
        a = make_audio("意味が、分かりましたか？", f"a_{n}.mp3"); n += 1
        dur = AudioFileClip(a).duration + 1.6
        scenes.append(make_scene("意味が分かりましたか？", a, theme, force_duration=dur))
    a = make_audio(data["reveal"], f"a_{n}.mp3"); n += 1
    scenes.append(make_scene(data["reveal"], a, theme))
    a = make_audio(data["ending"], f"a_{n}.mp3"); n += 1
    scenes.append(make_scene(data["ending"], a, theme))

    final = concatenate_videoclips(scenes, method="compose")
    final.write_videofile(output_path, fps=FPS, codec="libx264", audio_codec="aac")

    for i in range(n):
        f = f"a_{i}.mp3"
        if os.path.exists(f):
            os.remove(f)
    return output_path


# ----- YouTubeアップロード（リフレッシュトークンで無人認証） -----
def get_youtube():
    creds = Credentials(
        token=None,
        refresh_token=YT_REFRESH_TOKEN,
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())   # アクセストークンを取得
    return build("youtube", "v3", credentials=creds)


def upload(youtube, path, story_type, data):
    meta = META[story_type]
    title = (meta["title_tag"] + (data.get("youtube_title") or story_type))[:100]
    body = {
        "snippet": {
            "title": title,
            "description": meta["description"][:5000],
            "tags": meta["tags"],
            "categoryId": CATEGORY_ID,
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
    story_type = pick_type(log)
    print(f"📝 種別：{story_type} で生成")

    avoid = [e["summary"] for e in log if e.get("type") == story_type][-AVOID_RECENT:]
    data = generate_story(story_type, avoid)
    print(f"   タイトル：{data.get('youtube_title')}")

    path = build_video(data, story_type)
    print(f"🎬 生成完了：{path}")

    youtube = get_youtube()
    res = upload(youtube, path, story_type, data)
    vid = res["id"]
    print(f"✅ 投稿成功： https://www.youtube.com/watch?v={vid}")
    print(f"   公開設定：{res['status']['privacyStatus']}")

    log.append({"type": story_type,
                "title": data.get("youtube_title", ""),
                "summary": data.get("summary", "")})
    save_log(log)
    print(f"📝 ログ更新（計{len(log)}件）")


if __name__ == "__main__":
    main()
