# AutoClip

AIが生成した漫画ページ（PNG/JPG）を、**CLIP STUDIO PAINT EX の本物のコマ枠フォルダー**へ移送する Windows 用のツールです。AIエージェント（Codex、Claude Code など）に頼むと、質問しながら作業を進めます。

- 元の漫画のコマ群を、クリスタの**基本枠**いっぱいに配置し、1ページ目に作った基本枠を分割して各ページへ配布します。
- 拡大にはAI（RealESRGAN x4）を使えます。組み込みの環境、既存の ComfyUI / StabilityMatrix の読み取り利用、拡大なし、から選べます。
- 校正はComputer Useを標準とし、AIエージェントがローカルのクリスタの画面を見て現在の操作位置を調べます。接続可否を先に確認します。F8は不要です。設定と校正後は `run all` の1コマンドで解析から移送まで進み、途中であなたが解析結果を確認します。
- ページ数が増えてもAIエージェントの会話量はほぼ増えません。解析・拡大・投入は1つのコマンドで全ページを処理します。
- 人間の確認が必要です。AIが確認を代行することはありません。

> 動作確認の範囲：Windows 11、CLIP STUDIO PAINT EX（日本語UI）、1920×1080・100%、用紙182×257mm・350dpi（基本枠125×180mm）、RTX GPU。矩形で直線的に分割できるコマ割りが対象です。斜め枠・開放枠・重なり・見開き、縦横比を保って余白を作る配置、SVGのセリフ貼り付けは未対応です。

## 使い方

選べること・できないことの全体図は [docs/OPTIONS.html](docs/OPTIONS.html) にあります。

1. AIが既存の ComfyUI / StabilityMatrix などの有無をあなたに尋ねます。環境検索はあなたが了承してから行います。解析・操作用の環境をつくり、`input\` に漫画ページと `.cmc`/`.clip` を置きます。通常のセットアップではtorchを導入しません → [docs/SETUP.html](docs/SETUP.html)
2. AIエージェントでこのフォルダーを開き、「漫画をクリスタに移送したい」と伝えます。エージェントは [AGENTS.md](AGENTS.md) に従って質問し、次の順に進めます。

| 段階 | 内容 |
| --- | --- |
| 調査・質問 | `scan` で `input\` を調べ、縦横比・拡大・画面と既存の画像生成環境の有無を質問します。環境検索は了承後だけ `scan --discover-env` で行います |
| 校正 | [docs/PROFILE.html](docs/PROFILE.html)。Computer Useで画面を確認・操作し、観測座標をまとめて登録して色見本の分割で検証します |
| 準備・解析 | 基本枠を作って全ページへ配布し、コマを自動検出します |
| 確認 | あなたが `output\<作業名>\index.html` を見て、確認したと伝えます |
| 拡大・投入 | 全ページを自動で処理します（1ページ約30秒）→ [docs/RUN.html](docs/RUN.html) |

中断したときは [docs/RECOVERY.html](docs/RECOVERY.html)。

## コマンド（エージェントが実行します）

```powershell
.\autoclip.cmd scan                       # input\ だけを調べる。環境検索は了承後に --discover-env
.\autoclip.cmd configure --name 作業名 --images input\作品 --project input\作品\作品.cmc --fit stretch --upscale none
.\autoclip.cmd profile computer --step prepare # Computer Useで校正する練習用ページを準備
# エージェントがComputer Useで操作し、観測結果を保存
.\autoclip.cmd profile computer --step finish  # 登録した座標とネイティブ構造を検証
.\autoclip.cmd run calibrate              # 基本枠を作り、全ページへ配布
.\autoclip.cmd run analyze                # 全ページのコマを検出
.\autoclip.cmd run approve                # 確認後に実行。確認者名は「ユーザー」
.\autoclip.cmd run upscale
.\autoclip.cmd run prepare ; .\autoclip.cmd run check ; .\autoclip.cmd run import
.\autoclip.cmd status                     # 現在地と次の操作（いつでも）
# 設定後、一括実行（途中に本人の解析結果確認が入ります）
.\autoclip.cmd run all                    # 確認者名は「ユーザー」（指定する場合だけ --reviewer 名前）
```

出力はページ数によらず数行です。詳細は `work\<作業名>\logs\` に保存されます。

## 安全の方針

- `input\` は変更しません。作業は `work\` に複製して行い、元のバックアップとハッシュを残します。
- `.cmc`/`.clip` を直接書き換えません。コマ枠の作成・分割・保存はクリスタの正規の操作です。基本枠のファイルコピーは、元ページを退避してから行い、ハッシュで検証します。
- 解析と投入は別の段階です。未確認・低信頼・寸法の不一致・画面設定の不一致の場合は止まります。
- 既存の ComfyUI / StabilityMatrix は読み取り利用だけで、変更しません。
- `profile\`、`work\`、`output\`、`input\`、`models\`、`.env` は公開されません（`.gitignore`）。

## 開発

```powershell
.venv-tools\Scripts\python.exe -m unittest discover -s tests
```

テストは合成ページだけを使い、クリスタを必要としません。ライセンスは [MIT](LICENSE)。サードパーティの表示は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
