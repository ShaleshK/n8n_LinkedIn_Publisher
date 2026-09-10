# Autonomous Brand Publisher & Multi-Agent Content Engine

An asynchronous marketing content engine built with **FastAPI** and **Microsoft AutoGen**. It uses a two-agent orchestration layer to draft, review, and structure a LinkedIn post before handing it to an **n8n orchestration pipeline** for automated or human-in-the-loop publishing, depending on how confident the agents are in the draft.

## 🏗️ System Architecture

The project has two independent n8n trigger chains that share the same canvas, plus a confidence gate that decides whether a human ever sees the draft before it publishes.

1. **The Generation Pipeline:** A Schedule Trigger fires brand config fields to the FastAPI microservice on a cadence you set. Inside the service, two agents run in sequence:
   * **IdeatorAgent:** Drafts three content angles, a full post draft, a confidence score, and hashtags.
   * **ValidatorAgent:** Reviews the Ideator's JSON output, checks it against basic structural rules, and returns the final, polished payload.

2. **The Confidence & Dry-Run Gate:** Right after the draft is assembled, an IF node checks two things: whether `confidence_score` clears `MIN_CONFIDENCE_THRESHOLD`, and whether `DRY_RUN_FLAG` is `false`. Both must hold for the post to skip human review:
   * **High confidence + not dry-run → auto-publish.** The post goes straight to the shared "Build LinkedIn Post Body" node and publishes immediately, no Slack message at all.
   * **Low confidence or dry-run → Slack review.** The post is sent to a Slack channel as an interactive Block Kit message with Approve/Reject buttons, and nothing publishes until a human clicks Approve.

   `DRY_RUN_FLAG` is a manual safety switch independent of confidence — set it to `true` any time you want every post to go through human review regardless of how confident the agents are (useful while testing changes to the pipeline).

3. **The Publishing Pipeline (Slack-approved path):** A separate Webhook node listens for Slack's interactivity callback. On receipt, it fans out into two parallel branches: one immediately acknowledges Slack's request (required within ~3 seconds), and the other parses the button payload, checks whether "Approve" was clicked, and — if so — feeds into the *same* "Build LinkedIn Post Body" node used by the auto-publish path, then publishes and logs to Google Sheets.

**Why both paths share one "Build LinkedIn Post Body" Code node:** the auto-publish path hands off a `final_post_text` field, while the Slack-approved path hands off `post_text` (from parsing the button payload). The shared node uses a fallback — `$input.item.json.post_text || $input.item.json.final_post_text` — so one node handles both cases instead of duplicating the LinkedIn body-building logic twice. This same node also stamps an `origin` field (`"auto_published"` or `"slack_approved"`) so downstream logging can tell the two paths apart.

**Why the body gets built in a Code node, not the HTTP Request node's JSON field directly:** AI-generated post text can contain line breaks, quotes, or other characters that break naive string templating. The Code node uses `JSON.stringify()` on a proper JS object so the HTTP Request node just references one pre-escaped string — this avoids a whole class of "invalid JSON" errors that raw templating is prone to.

---

## 📊 Google Sheets Logging

Every publish — auto or Slack-approved — appends a row to the "Ticket Log" sheet via the "Append row in sheet" node, with these columns:

| Column | Source | Notes |
|---|---|---|
| `timestamp` | `$now.toFormat(...)` | When the row was logged |
| `sender` | Static | "Slack Interactive Webhook" |
| `subject` | `Build LinkedIn Post Body` node | First 50 characters of the published post text |
| `priority` | `Build LinkedIn Post Body` node | `"Low Triage"` if `confidence_score >= 0.95`, else `"Review Required"` |
| `status` | `Build LinkedIn Post Body` node | `"Published Live (Auto)"` or `"Published via Slack Approval"`, based on the `origin` field |
| `confidence_score` | `Build LinkedIn Post Body` node | The raw numeric score, for tracking how the threshold is performing over time |

To get `confidence_score` to survive the Slack-approved path, it's embedded directly in the Slack message text (`*Confidence Score:* {{ $json.confidence_score }}`, appended after the post copy) and re-extracted from the button-click payload in "Parse Slack Approval Payload," alongside the existing post-text extraction.

This log is what the original assignment brief means by "adjust parameters for optimal balance between automation and oversight" — review `priority`/`confidence_score` over time to see whether `MIN_CONFIDENCE_THRESHOLD` is calibrated well, or whether too many low-confidence drafts are clogging up Slack review.

---

## ⚠️ n8n Gotchas

### Boolean Conditions and Type Coercion
Avoid writing a condition as a single hand-computed boolean expression checked against n8n's "is true" operator, e.g.:
```
{{ (parseFloat($json.confidence_score) > parseFloat($('Brand Config & Controls').item.json.MIN_CONFIDENCE_THRESHOLD)) }}
```
n8n's condition UI can round-trip this result through a string internally, and in JavaScript the *string* `"false"` is truthy — so a genuinely false comparison can silently evaluate as true. `.toBoolean()` does not reliably fix this either, since it isn't meant to be called on an already-boolean value.

**The fix:** use n8n's native typed comparison fields instead of a hand-rolled expression. Set the condition's type to **Number**, put the raw values in the left/right fields, and pick **"greater than"** as the operator (or **Boolean** type with **"not equal"** for flag checks) — let n8n's own comparison engine do the work rather than pre-computing a boolean yourself. This is the same pattern already used in "Approval Gate Routing," and it's the one that actually behaves correctly.

### Google Sheets and Leading `=`
When mapping columns via cross-node references (`{{ $('Node Name').item.json.field }}`), the value box needs its own leading `=` to be treated as an expression. But if that `=` doesn't get consumed cleanly and ends up as part of the literal text sent to Google Sheets, the Sheets API interprets any cell value starting with `=` as a formula — and since our expression syntax isn't valid formula syntax, the cell shows a parse error instead of your actual text. If this happens: check the cell's formula bar directly in Sheets to confirm, then try the expression without the leading `=` in that specific field. Whether the leading `=` is needed varies by field in practice, so if one column throws a parse error and another with a similar expression doesn't, this is the first thing to check.

---

## 🛠️ Microservice Interface Schema

### POST `/generate/content`
**Request Payload (JSON):**
```json
{
  "brand_name": "OpenClaw AI Solutions",
  "target_audience": "Marketing Managers at mid-size SaaS firms",
  "tone": "Trustworthy, data-informed, highly innovative",
  "core_context": "Promoting our new automated runtime validation engine",
  "length_limit": 250
}
```

**Response Payload (JSON):**
```json
{
  "ideas": ["Angle 1...", "Angle 2...", "Angle 3..."],
  "draft": "Full text of the recommended LinkedIn post.",
  "confidence_score": 0.92,
  "hashtags": ["#MarTech", "#SaaS"]
}
```

---

## 🚀 Local Installation & Execution

### 1. Set Up Your Python Environment
```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Create a local `.env` file in the root directory (**never commit this file** — see Security Notes below) with your Azure OpenAI details:
```env
AZURE_OPENAI_API_KEY=your_azure_api_key_here
AZURE_OPENAI_ENDPOINT=https://<your-resource-name>.openai.azure.com/
AZURE_OPENAI_CHAT_DEPLOYMENT=your-deployment-name
AZURE_OPENAI_API_VERSION=2025-01-01-preview
```

### 3. Start the FastAPI Microservice
```bash
uvicorn agent_service:app --host 127.0.0.1 --port 8000 --reload
```

### 4. Open a Tunnel to Your Local n8n Instance
Slack needs a public URL to deliver the interactivity callback to your local n8n. Using ngrok as an example:
```bash
ngrok http 5678 --domain=<your-ngrok-domain>
```

### 5. Point Slack at Your Webhook
In your Slack App's **Interactivity & Shortcuts** settings, set the Request URL to:
```
https://<your-ngrok-domain>/webhook/slack-interactivity-receiver
```
This must be the **production** webhook path (`/webhook/...`), not the test path (`/webhook-test/...`), and your n8n workflow must be **Active** (and, if your n8n instance uses draft/publish versioning, **Published**) for it to respond.

### 6. LinkedIn Setup
- Create a LinkedIn Developer App and enable **"Sign In with LinkedIn using OpenID Connect"** at minimum; publishing requires a product granting `w_member_social` (may require app review).
- In n8n, use the built-in **LinkedIn OAuth2 API** credential type. Turn off "Legacy" (uses OpenID Connect scopes matching the product above) and "Organization Support" (unless posting as a Company Page).
- Standard LinkedIn apps issue access tokens valid for **60 days with no refresh token** — you'll need to reconnect the credential periodically.
- Set the `LinkedIn-Version` header on the publish request to a current 6-digit `YYYYMM` value; LinkedIn deprecates versions after roughly a year, so this needs occasional updates. Check LinkedIn's versioning docs for the current supported value.

### 7. Tune the Confidence Gate
In the "Brand Config & Controls" node, set:
- `MIN_CONFIDENCE_THRESHOLD` (Number) — the minimum `confidence_score` required to auto-publish without human review. Start conservative (e.g. `0.90`+) until you trust the agents' calibration.
- `DRY_RUN_FLAG` (Boolean) — set `true` to force every post through Slack review regardless of confidence; set `false` to allow auto-publishing once you're confident in the pipeline.

---

## 🔒 Security Notes Before Pushing to a Public Repo

Do **not** commit the exported workflow JSON as-is if it contains:
- The **Slack Incoming Webhook URL** hardcoded in the "Route to Slack for Review" node — anyone with this URL can post into your Slack channel. (Note: if this URL is ever exposed, rotate it in Slack's app settings — deleting the old webhook immediately invalidates it, and the "Route to Slack for Review" node will need updating with the new one.)
- Your **ngrok domain** combined with the webhook path — combined with the fact that this workflow doesn't verify Slack's request signature, anyone with this URL could trigger a real LinkedIn post without going through Slack at all. Adding Slack request signature verification to the Webhook node is strongly recommended if this goes anywhere near production.
- Your **LinkedIn URN** in the "Build LinkedIn Post Body" Code node — lower risk (roughly equivalent to your public profile ID), but there's no reason to publish a real person's identifier in a public repo.

Before pushing: export the workflow JSON, replace these with placeholders in the exported copy (e.g. `YOUR_SLACK_WEBHOOK_URL`, `YOUR_NGROK_DOMAIN`, `urn:li:person:YOUR_URN_HERE`), and commit that sanitized copy rather than the raw export. Add `.env` to `.gitignore`. Actual credentials (Azure, LinkedIn OAuth, Google Service Account) live in n8n's credential store and are not included in a standard workflow export, so those are safe by default.