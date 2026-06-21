@echo off
setlocal
cd /d "%~dp0"

rem Safety defaults: no private chat replies; group replies only when @Bot.
set "ONEBOT_ENABLE_PRIVATE=true"
set "ONEBOT_GROUP_TRIGGER=mention_only"

rem Optional whitelist. Set to true to only allow listed groups/users.
set "ONEBOT_ENABLE_WHITELIST=true"
set "ONEBOT_ALLOWED_GROUPS=QQ_group"
set "ONEBOT_ALLOWED_PRIVATE_USERS=qq_master"

rem Reply to the trigger message in groups.
set "ONEBOT_REPLY_TO_TRIGGER=true"

rem Optional: QQ custom emoji name index. One name per line, order must match NapCat fetch_custom_face.
rem The bot can then send [QQ_EMOJI:name] as a custom emoji image.
rem set "ONEBOT_CUSTOM_EMOJI_INDEX_PATH=data\bqbs.txt"

rem QQ queue: default is parallel and does not wait for a reply before accepting the next item.
set "ONEBOT_ENABLE_QQ_QUEUE=true"
set "ONEBOT_QQ_QUEUE_INTERVAL=2"
set "ONEBOT_QQ_QUEUE_REPLY_TIMEOUT=120"
set "ONEBOT_QQ_QUEUE_WORKERS=4"
set "ONEBOT_QQ_QUEUE_WAIT_FOR_REPLY=false"
rem Preempt is opt-in. Keep false to prevent stale running tasks from swallowing new QQ messages.
set "ONEBOT_ENABLE_PREEMPT=false"

python platform\onebot11_adapter.py
pause
