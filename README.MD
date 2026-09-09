# Autonomous Brand Publisher & Multi-Agent Content Engine

An asynchronous marketing content engine built with **FastAPI** and **Microsoft AutoGen**. It uses a two-agent orchestration layer to draft, review, and structure a LinkedIn post before handing it to an **n8n orchestration pipeline** for human-in-the-loop approval and publishing.

## 🏗️ System Architecture

The project has two independent n8n trigger chains that share the same canvas:

1. **The Generation Pipeline:** A Schedule Trigger fires brand config fields to the FastAPI microservice on a cadence you set. Inside the service, two agents run in sequence:
   * **IdeatorAgent:** Drafts three content angles, a full post draft, a confidence score, and hashtags.
   * **ValidatorAgent:** Reviews the Ideator's JSON output, checks it against basic structural rules, and returns the final, polished payload.
   The assembled post text is sent to a Slack channel as an interactive Block Kit message with Approve/Reject buttons.

2. **The Publishing Pipeline:** A separate Webhook node listens for Slack's interactivity callback (fired when someone clicks Approve or Reject). On receipt, it fans out into two parallel branches: one immediately acknowledges Slack's request (required within ~3 seconds), and the other parses the button payload, checks whether "Approve" was clicked, builds a properly-escaped LinkedIn API request body in a Code node, publishes the post via LinkedIn's Posts API, and logs the result to a Google Sheet.

**Why the body gets built in a Code node, not the HTTP Request node's JSON field directly:** AI-generated post text can contain line breaks, quotes, or other characters that break naive string templating. The Code node uses `JSON.stringify()` on a proper JS object so the HTTP Request node just references one pre-escaped string — this avoids a whole class of "invalid JSON" errors that raw templating is prone to.

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

---

## 🔒 Security Notes Before Pushing to a Public Repo

Do **not** commit the exported workflow JSON as-is if it contains:
- The **Slack Incoming Webhook URL** hardcoded in the "Route to Slack for Review" node — anyone with this URL can post into your Slack channel.
- Your **ngrok domain** combined with the webhook path — combined with the fact that this workflow doesn't verify Slack's request signature, anyone with this URL could trigger a real LinkedIn post without going through Slack at all. Adding Slack request signature verification to the Webhook node is strongly recommended if this goes anywhere near production.
- Your **LinkedIn URN** in the Code node — lower risk (roughly equivalent to your public profile ID), but there's no reason to publish a real person's identifier in a public repo.

Before pushing: replace these with placeholders (e.g. `YOUR_SLACK_WEBHOOK_URL`, `YOUR_NGROK_DOMAIN`, `urn:li:person:YOUR_URN_HERE`), and add `.env` to `.gitignore`. Actual credentials (Azure, LinkedIn OAuth, Google Service Account) live in n8n's credential store and are not included in a standard workflow export, so those are safe by default.
