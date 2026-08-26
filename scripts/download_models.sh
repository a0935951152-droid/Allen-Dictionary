#!/usr/bin/env bash
# 把架構需要、但 /nas-data/allen/models 尚缺的模型一次拉到 NAS（之後 runtime 全離線）。
# 模型選型對齊姊妹專案 SpeakIn（/nas-data/allen-speakin 的 stages 單元）：
#   STT      = faster-whisper large-v3-turbo（SpeakIn stt_whisperlive 預設）
#   LLM      = Qwen3.8-27B Q2_K_L GGUF，固定從 QWEN_MODEL_PATH 載入 → 本腳本不另下載
#   TTS      = CosyVoice2-0.5B（已在 NAS models/tts/cosyvoice2-0.5b）→ 不另下載
#   NER      = ckiplab/bert-base-chinese-ner（§3 篩選擋常用詞）
#   g2pW     = alextomcat/G2PWModel（§3 ①IPA 注音/多音字消歧）
#
# 用法：bash scripts/download_models.sh [stt|ner|g2pw|all]
# 注意：只用既有 hf CLI 下載（唯讀），不 pip 安裝/升級（避免動到 MISA 的 ~/.local）。
set -euo pipefail

MODELS_ROOT="${MODELS_ROOT:-/nas-data/allen/models}"
HF="$(command -v hf || echo "$HOME/.local/bin/hf")"
[ -x "$HF" ] || { echo "找不到 hf CLI（請先備妥 huggingface_hub[cli]，本腳本不自行 pip 安裝）"; exit 1; }
dl(){ echo "▶ 下載 $1 → $2"; "$HF" download "$1" --local-dir "$2"; }   # repo, dest

target="${1:-all}"
case "$target" in
  stt|all)
    # large-v3-turbo：faster-whisper 把 "large-v3-turbo" 解析到此 CT2 repo
    dl "mobiuslabsgmbh/faster-whisper-large-v3-turbo" "$MODELS_ROOT/stt/faster-whisper-large-v3-turbo"
    ;;&
  ner|all)
    dl "ckiplab/bert-base-chinese-ner" "$MODELS_ROOT/ner/ckip-bert-base-chinese-ner"
    ;;&
  g2pw|all)
    dl "alextomcat/G2PWModel" "$MODELS_ROOT/g2pw/G2PWModel"
    ;;&
  ""|stt|ner|g2pw|all) : ;;
  *) echo "未知目標：$target（可用 stt|ner|g2pw|all）"; exit 1 ;;
esac

echo "✓ 完成。LLM 使用既有 Qwen3.8-27B Q2_K_L GGUF；TTS 重用 $MODELS_ROOT/tts/cosyvoice2-0.5b。"
echo "  離線使用：設 HF_HUB_OFFLINE=1，模型路徑指向 $MODELS_ROOT 下對應目錄。"
