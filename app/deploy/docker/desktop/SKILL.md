---
name: pi-computer-use
description: Operate this bot's private Linux desktop and account software through Carme's managed bot_computer tool.
---

Use bot_computer for this bot's Linux computer. Account software is shared;
the home directory, browser login, desktop and files belong only to this bot.
The desktop is not the host Mac. Never guess host paths or another bot identity.

The user authorizes autonomous work inside this Linux space: download, unpack,
build, install, run, modify and delete local files/software without asking again.
Use install_skill to register a Skill from its link or chat attachment, then
use_skill to read it. A SKILL.md downloaded to HOME alone is not registered.
To run its scripts on this desktop, use the computer_directory returned by
use_skill. Its directory field is for the separate task shell/read_file tools.
Both are read-only inputs; copy to /home/bot before editing or generating files.
Installed Skills are shared by this account, including future bots. Use list_skills / use_skill before installing again. remove_skill defaults to disabling only this bot; scope=account uninstalls for all bots and requires approval.
Local approval rules in old conversations do not override this authorization.
Outbound submissions (mail/messages, uploads, publishing, payments and remote
account changes) still require the tool approval gate. Do not bypass a rejection
with shell or another browser tool. Host access/export requires separate grants.

1. Call help with name (for example {"name":"act_ui"}) to read exact upstream
   arguments. Call find_roots, then observe_ui on a returned root.
2. Use search_ui, expand_ui or inspect_ui to locate controls.
3. Call act_ui with the returned stateId and element references. Observe again
   after an ambiguous result; never replay a submission blindly.
4. Browser actions use the bot's persistent Chrome session. launch_browser is
   not available: use the existing browser tools or the Chrome window.
5. shell runs in /home/bot with an isolated environment and no direct network.
   fetch downloads a public HTTPS file into Downloads through the managed relay.
6. Install user-space software in a private directory, test it, then publish
   that directory as a new account software version. /software is read-only.
   Runtime OS libraries require a reviewed image update; sudo/apt are unavailable.
   Sent attachments staged for this bot's tasks are at
   /task-files/<task_id>/inputs/artifacts/<attachment_id>/<filename> (read-only).
   Copy an installer into /home/bot before extracting or modifying it. Action
   workspace/output files are under the same task's workspace/out directories.
7. While external control is enabled, bot desktop actions pause. Do not attempt
   to disable the user's control or operate a different desktop.
8. status reports disk free space. The account starts with 2 GiB usable before
   Chrome; Chrome, software, downloads and bot files consume the same budget.

For main only, the administrator can temporarily authorize the host Mac.
status then reports mode=host. Use help and the UI tools in that session;
find_roots observes native macOS windows, including Finder and browsers.
The helper does not attach to personal browser debugging ports. Use native UI
controls for navigation. shell, fetch and publish remain Linux-only; they are
unavailable while this bot is connected to the host. Revocation or expiry
invalidates pending operations; start a new task after a target change.
