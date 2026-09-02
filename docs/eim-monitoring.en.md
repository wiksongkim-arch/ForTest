English | [简体中文](eim-monitoring.md)

# EIM Monitoring Guide

> EIM still requires real G0 acceptance in an independent test organization. Public builds hide the entry and do not start its background runtime by default. Controlled testers can set `FORTEST_ENABLE_EIM=1` before launch; remove it or set it to `0`, then restart, to disable EIM again.

EIM Monitoring uses the bundled and hash-verified DWS v1.0.60 runtime to receive DingTalk group events continuously and archive them to DingTalk Docs, Sheets, or AI Tables. Every connection has an isolated ForTest configuration directory and never reads a user's global DWS sign-in state.

## Prerequisites

- Windows x64 and DingTalk are supported in the first release; Feishu and WeCom are not yet enabled.
- Use a DingTalk account that can sign in normally and see the source group.
- Prepare a writable target URL under `https://alidocs.dingtalk.com/i/...` that the same account can read.
- Archiving images, files, audio, video, and cards consumes local temporary storage and the target platform's upload quota.

The official DingTalk event stream filters messages sent by the currently authorized OAuth account. ForTest does not bypass this behavior with polling, desktop automation, or private databases: messages from other members and bots are monitored, while messages from the authorized account itself are outside the first-release scope.

## Connect and Create a Task

1. Open EIM Monitoring, then Connection and Runtime Settings.
2. Select Connect DingTalk, complete the official browser or QR authorization flow, and refresh the connection status.
3. Confirm that sign-in, credential validity, group discovery, message events, and Reaction events all pass.
4. Return to the overview, select Create Monitoring Task, and enter a task name, source group, and archive target URL.
5. Configure triggers, filters, context, field mappings, media policy, and failure policy in the workbench.
6. Add at least one sanitized sample and run it. After a successful build, start the task explicitly.

A running task is read-only. Stop it before editing, build and deploy a new immutable version, then start it again. A failed build never replaces the current ready version.

## Events and Destinations

The first release handles text, images, files, audio, video, cards, quoted messages, and Reaction add/remove events. Source events are normalized, deduplicated, and recorded in the local inbox/outbox before a trusted destination adapter writes them:

- Docs append content blocks with stable EIM markers and read back first when commit status is uncertain.
- Sheets accept only worksheets and valid columns resolved from the target URL, then read back the EIM idempotency key after appending.
- AI Tables accept only the resolved table and writable fields, upsert by event key, and read back the result.

A target must be an official DingTalk Docs HTTPS URL and must not contain credential parameters such as token, password, or secret.

## Permissions and Runtime State

The authorized account needs both source-group visibility and target read/write access. If the account signs out, OAuth expires, the group becomes invisible, destination fields change, or write access is revoked, the task stops, reports an error, or becomes Degraded instead of claiming an unverified delivery succeeded.

After a short disconnect, ForTest reads the update window allowed by the official API and deduplicates overlap. Gaps that cannot be reconciled reliably, including some Reaction gaps, are explicitly logged as degraded. Closing the main window to the tray keeps tasks running; explicitly exiting ForTest cleans up DWS child processes. Connection and Runtime Settings controls whether previous running intent is restored at the next startup.

## AI Cost and Data Boundaries

- Deterministic builds and deterministic runtime rules never call AI and incur no model cost.
- When an AI build configuration is selected, only configurations that pass the EIM compatibility check are used for rule building, correction, and sample validation.
- Runtime AI is used only when the DSL explicitly contains `ai_steps`. Each step has a timeout, a daily budget measured in tokens or calls, and a skip, archive_raw, retry, or stop action for exhausted budgets or unavailable models.
- `input_fields` explicitly controls the fields sent to a model; `redacted_fields` can exclude sensitive fields, and images are sent only when explicitly enabled.

Connections, tasks, deduplication state, logs, and temporary media are stored under `%LOCALAPPDATA%\ForTest\UserData`. Logs are retained for 30 days by default. Media for completed deliveries follows the task policy, 24 hours by default; media with incomplete delivery is kept for at most 30 days. Configuration exports contain no connection credentials, and imported tasks must be rebound to a source group and destination while stopped.

## Troubleshooting

- Disconnected or Authorization Expired: authorize again and refresh. ForTest does not reuse global DWS credentials.
- Group not listed: confirm the account has joined and can view the group. Groups with the same name remain separate.
- The authorized user's own message is missing: this is the documented DingTalk self-loop boundary; verify monitoring with another member or a bot.
- Task is Degraded: open Runtime Logs and check disconnect gaps, destination permissions, schema changes, media upload, or readback failures. Stop and restart after correcting the cause.
- Invalid destination URL: use an official `alidocs.dingtalk.com/i/` HTTPS link for a Doc, Sheet, or AI Table, with no credential parameters.
- AI configuration unavailable: complete its connection and EIM build compatibility check under Settings → AI Settings.
- Reporting an issue: export sanitized runtime logs first. Never attach accounts, group IDs, business messages, target URLs, tokens, or local paths.

Advanced mode accepts only strictly validated EIM DSL. It never executes Python, JavaScript, or Shell, and AI cannot modify connectors, destination adapters, or arbitrary network destinations.
