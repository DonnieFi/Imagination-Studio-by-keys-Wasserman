# A raw video prompt with no spoken line is given the Studio's own ambient line.
# Sourced by scripts/video.sh and spark/video.sh. The user's sentences stay.
# app/negation.py ensure_ambient holds the rule; this only applies it.
# PROMPT and PROMPT_FILE are the script's own. REF_VIDEO_SOUND=1 leaves both as they are.

ambient_prompt() {
  local app="$1"
  local py extra="" before after
  py="$(command -v python3 || true)"
  [ -n "$py" ] || { echo "python3 is needed to prepare a video prompt" >&2; exit 2; }
  [ "${REF_VIDEO_SOUND:-0}" = 1 ] && extra="--video-sound"
  if [ -n "${PROMPT_FILE:-}" ]; then
    [ -f "$PROMPT_FILE" ] || { echo "no prompt file at $PROMPT_FILE" >&2; exit 2; }
    before="$(cat "$PROMPT_FILE")"
    # shellcheck disable=SC2086
    after="$("$py" "$app/negation.py" $extra "$PROMPT_FILE")"
    [ "$after" = "$before" ] && return 0
    PROMPT="$after"
    PROMPT_FILE=""
  else
    before="$PROMPT"
    # shellcheck disable=SC2086
    after="$(printf '%s' "$PROMPT" | "$py" "$app/negation.py" $extra)"
    [ "$after" = "$before" ] && return 0
    PROMPT="$after"
  fi
  echo "[tensorfold] prompt: no spoken line and no overall_soundscape heading, so the clip is told: Only ambient sound is heard."
}
