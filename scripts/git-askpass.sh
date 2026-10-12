#!/bin/sh
# GIT_ASKPASS 辅助脚本：避免把令牌明文写进 remote URL / .git/config / 日志
# 依赖环境变量 DEPLOY_PASSWORD（用户名固定为 cnb）
case "$1" in
  *[Uu]sername*) printf '%s\n' "cnb" ;;
  *)             printf '%s\n' "${DEPLOY_PASSWORD:-}" ;;
esac
