#!/bin/zsh

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
if ! /bin/zsh "$APP_DIR/start.command" --update; then
  print "更新未完成，请查看上方提示。"
  status_code=1
fi
if [[ -t 0 ]]; then
  read 'reply?按回车关闭。'
fi
exit "${status_code:-0}"
