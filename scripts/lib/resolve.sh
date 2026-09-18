#!/bin/bash
# Shared resolution helpers for debug/setup scripts.
# Source this file at the top of any script:
#   SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
#   source "$SCRIPT_DIR/lib/resolve.sh"
#
# Provides:
#   REGION                 — from AWS_REGION or defaults to us-east-1
#   resolve_instance_id    — auto-discovers Connect instance ID
#   resolve_instance_alias — auto-discovers Connect instance alias (log group = /aws/connect/<alias>)
#   resolve_assistant_id   — reads Wisdom assistant ID from SSM
#   resolve_account_id     — from STS caller identity
#   resolve_bot_id         — Lex V2 bot ID by name (default: OptionEQinConnectBot)
#   resolve_bot_alias_id   — Lex V2 bot alias ID for a bot (default: first non-draft alias)

SSM_PREFIX="/connect-async-multi-response"
REGION="${AWS_REGION:-us-east-1}"

resolve_instance_id() {
  local id="${1:-}"
  if [ -n "$id" ]; then echo "$id"; return; fi
  aws connect list-instances --max-results 1 \
    --query 'InstanceSummaryList[0].Id' --output text --region "$REGION" 2>/dev/null
}

resolve_instance_alias() {
  local alias="${1:-}"
  if [ -n "$alias" ]; then echo "$alias"; return; fi
  aws connect list-instances --max-results 1 \
    --query 'InstanceSummaryList[0].InstanceAlias' --output text --region "$REGION" 2>/dev/null
}

resolve_assistant_id() {
  local id="${1:-}"
  if [ -n "$id" ]; then echo "$id"; return; fi
  local arn
  arn=$(aws ssm get-parameter \
    --name "$SSM_PREFIX/wisdom-assistant-arn" \
    --query 'Parameter.Value' --output text --region "$REGION" 2>/dev/null)
  echo "${arn##*/}"
}

resolve_account_id() {
  aws sts get-caller-identity --query 'Account' --output text 2>/dev/null
}

# Usage: resolve_bot_id [BOT_ID] [BOT_NAME]
#   Explicit BOT_ID wins; otherwise look up by name (default OptionEQinConnectBot,
#   the Option E Lex bot that must be created via the Connect console).
resolve_bot_id() {
  local id="${1:-}" name="${2:-OptionEQinConnectBot}"
  if [ -n "$id" ]; then echo "$id"; return; fi
  aws lexv2-models list-bots --region "$REGION" \
    --query "botSummaries[?botName=='$name'].botId | [0]" --output text 2>/dev/null
}

# Usage: resolve_bot_alias_id BOT_ID [ALIAS_ID]
resolve_bot_alias_id() {
  local bot_id="$1" alias_id="${2:-}"
  if [ -n "$alias_id" ]; then echo "$alias_id"; return; fi
  aws lexv2-models list-bot-aliases --bot-id "$bot_id" --region "$REGION" \
    --query "botAliasSummaries[?botAliasName!='TestBotAlias'].botAliasId | [0]" --output text 2>/dev/null
}
