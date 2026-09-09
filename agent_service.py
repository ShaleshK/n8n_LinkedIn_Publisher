# implement an AutoGen-style multi-agent workflow that generates consistent, on-brand 
# LinkedIn posts with minimal human effort, while retaining oversight through approval 
# controls. 

# Tasks
# • Design and implement the AutoGen-style microservice using FastAPI, exposing 
#       an endpoint that accepts brand configuration and context, then returns JSON 
#       containing ideas, draft, confidence score, and hashtags 
# • Deploy and test the microservice using curl or HTTP, ensuring it accepts brand and context 
#       information via an API and returns structured JSON output
# • Create a new workflow in the n8n dashboard, with a schedule trigger set to a 
#       suitable cadence for LinkedIn posting 
# • Add nodes for brand configuration and pass this data to the microservice using 
#       an HTTP Request node with JSON parameters
# • Parse the microservice response to assemble the final text and hashtags using a 
#       Set node (no JavaScript required) 
# • Implement an approval gate that compares the confidence score against a 
#       minimum threshold and checks a dry-run flag to route posts to Slack for review 
#       or LinkedIn for direct publishing 
# • Configure Slack integration via incoming webhook or OAuth (optional) to send 
#       draft posts for human review 
# • Add logging by appending run details (for example, timestamp, draft text, 
#       confidence) to a database or spreadsheet to monitor performance and tune 
#       thresholds 
# • Test the complete flow end-to-end and adjust parameters (for example, 
#       confidence threshold) for optimal balance between automation and oversight 

import asyncio
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
from typing import Any, Tuple, Dict
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient

# Initialize the main FastAPI application core
app = FastAPI(
    title="AutoGen Multi-Agent Content Microservice Gateway",
    description="Production-grade async API proxy layer for brand content generation via Azure AI Foundry",
    version="1.0.0"
)

# Enable CORS cross-origin access rules so your front-end scripts or n8n can talk to it
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def try_load_dotenv() -> None:
    """Load variables from .env when python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    custom_path = os.getenv("DOTENV_PATH")
    if custom_path:
        load_dotenv(dotenv_path=custom_path, override=True)
        return
    load_dotenv(override=True)
    script_env = pathlib.Path(__file__).resolve().parent / ".env"
    if script_env.exists():
        load_dotenv(dotenv_path=script_env, override=True)

def get_azure_config() -> dict:
    """Build Azure OpenAI configuration from environment variables."""
    try_load_dotenv()
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("AZURE_OPENAI_BASE_URL")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT")
    model_name = os.getenv("AZURE_OPENAI_MODEL", "gpt-5-mini-2025-08-07")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    
    missing = [
        name for name, value in {
            "AZURE_OPENAI_API_KEY": api_key,
            "AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_BASE_URL": endpoint,
            "AZURE_OPENAI_DEPLOYMENT or AZURE_OPENAI_CHAT_DEPLOYMENT": deployment,
        }.items() if not value
    ]
    if missing:
        missing_names = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variables: {missing_names}.")
        
    return {
        "api_key": api_key,
        "api_type": "azure",
        "model": model_name,
        "deployment": deployment,
        "base_url": endpoint,
        "api_version": api_version,
    }

SLEEP_BETWEEN_CALLS_SEC = float(os.environ.get("RATE_SLEEP_SEC", "1.0"))
SENSITIVE_ENV_KEYS = ("AZURE_OPENAI_API_KEY", "OPENAI_API_KEY", "AZURE_STORAGE_KEY", "AZURE_CLIENT_SECRET")
SENSITIVE_KEYWORDS = ("api_key", "authorization", "token", "secret", "password", "connection_string", "access_key")

RUNS_ROOT = pathlib.Path(__file__).resolve().parent / "Runs"
RUN_DIR = RUNS_ROOT / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
RUN_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = RUN_DIR / "log.jsonl"

def _looks_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    return any(keyword in key_lower for keyword in SENSITIVE_KEYWORDS)

def _secret_literals() -> list[str]:
    values: list[str] = []
    for env_name in SENSITIVE_ENV_KEYS:
        env_value = os.getenv(env_name)
        if env_value: values.append(env_value)
    return sorted(values, key=len, reverse=True)

def _redact_string(value: str) -> str:
    redacted = value
    for literal in _secret_literals():
        redacted = redacted.replace(literal, "[REDACTED]")
    return redacted

def _redact_value(value: Any, parent_key: str | None = None) -> Any:
    if parent_key and _looks_sensitive_key(parent_key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(key): _redact_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, str):
        return _redact_string(value)
    return value

def log_event(kind: str, payload: dict) -> None:
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "kind": kind,
        **_redact_value(payload),
    }
    with LOG_FILE.open("a", encoding="utf-8") as file_handle:
        file_handle.write(json.dumps(rec, ensure_ascii=False) + "\n")

# Modified block selector regex targeting clean JSON extraction
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

def extract_json_payload(text: str) -> dict:
    """Extract fenced or naked JSON structural payloads from agent text outputs."""
    cleaned = text.strip()
    match = JSON_BLOCK_RE.search(cleaned)
    if match:
        cleaned = match.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Emergency heuristic cleanup if the agent emits malformed structures
        try:
            fixed_text = cleaned.split('{', 1)[1].rsplit('}', 1)[0]
            return json.loads("{" + fixed_text + "}")
        except Exception:
            return {"error": "Failed to parse agent output into valid JSON framework", "raw_content": text}

async def _ask_async(agent: AssistantAgent, message: str, label: str) -> str:
    """Send one message to an agent and normalize the returned content."""
    await asyncio.sleep(SLEEP_BETWEEN_CALLS_SEC)
    result = await agent.on_messages(
        messages=[TextMessage(content=message, source="user")],
        cancellation_token=None,
    )
    if hasattr(result, "chat_message") and getattr(result.chat_message, "content", None) is not None:
        content = result.chat_message.content
    elif hasattr(result, "messages") and result.messages:
        content = getattr(result.messages[-1], "content", "") or ""
    else:
        content = str(result)
        
    log_event("llm_reply", {
        "agent": getattr(agent, "name", "assistant"),
        "label": label,
        "content": content,
    })
    return content

# ------------------------------------------------------------------------
# 📋 STEP 1: DEFINE BRAND SCHEMAS & MULTI-AGENT PROMPTS
# ------------------------------------------------------------------------
class BrandConfiguration(BaseModel):
    brand_name: str
    target_audience: str
    tone: str
    core_context: str
    length_limit: int = 280  # Optimized default for Slack/LinkedIn templates

def build_content_agents(model_client: AzureOpenAIChatCompletionClient) -> tuple[AssistantAgent, AssistantAgent]:
    """Build the multi-agent orchestration team optimized for structured creative workflows."""
    
    ideator = AssistantAgent(
        name="IdeatorAgent",
        system_message=(
            "You are IdeatorAgent, an expert corporate communications planner.\n"
            "Your task is to take a brand configuration and context, then generate exactly 3 highly engaging "
            "content angles, a full text draft based on the best angle, a baseline numeric evaluation score, "
            "and a list of relevant trending hashtags.\n\n"
            "You MUST reply with ONLY a perfectly formatted JSON structure matching this blueprint exactly:\n"
            "{\n"
            "  \"ideas\": [\"Angle 1...\", \"Angle 2...\", \"Angle 3...\"],\n"
            "  \"draft\": \"Complete text post body draft here...\",\n"
            "  \"confidence_score\": 0.95,\n"
            "  \"hashtags\": [\"#FirstTag\", \"#SecondTag\"]\n"
            "}\n"
            "Do not include markdown chat filler text. Return ONLY the JSON or a ```json codeblock."
        ),
        model_client=model_client,
    )
    
    validator = AssistantAgent(
        name="ValidatorAgent",
        system_message=(
            "You are ValidatorAgent, a senior brand compliance editor.\n"
            "Your job is to analyze the JSON structure emitted by the IdeatorAgent. "
            "Ensure that the 'ideas' array contains exactly 3 entries, the 'draft' fits the specified context limits, "
            "the 'confidence_score' is a valid floating point number, and 'hashtags' are present.\n"
            "If the payload is correct, return the JSON intact. If modifications are required, fix the fields "
            "natively and emit the final polished JSON payload.\n"
            "Return ONLY a clean, valid JSON structure."
        ),
        model_client=model_client,
    )
    
    return ideator, validator

# ------------------------------------------------------------------------
# 🚀 STEP 2: ENDPOINT ROUTING ORCHESTRATION
# ------------------------------------------------------------------------
@app.post("/generate/content")
async def generate_brand_content(config: BrandConfiguration):
    """Exposes an endpoint that accepts brand context and returns structured workflow JSON."""
    try:
        azure_cfg = get_azure_config()
        
        # Initialize native Azure AI completion proxy client
        model_client = AzureOpenAIChatCompletionClient(
            api_key=azure_cfg["api_key"],
            azure_endpoint=azure_cfg["base_url"],
            api_version=azure_cfg["api_version"],
            model=azure_cfg["model"],
            azure_deployment=azure_cfg["deployment"],
        )
        
        ideator, validator = build_content_agents(model_client)
        
        # Build strict payload routing context prompt string
        agent_prompt = (
            f"Brand Name: {config.brand_name}\n"
            f"Target Audience: {config.target_audience}\n"
            f"Tone/Voice Guide: {config.tone}\n"
            f"Context/Corporate Update: {config.core_context}\n"
            f"Length Restriction constraint: Max {config.length_limit} characters."
        )
        log_event("workflow_request", config.model_dump())

        # Step 2.1: Multi-Agent Collaboration Chain
        ideator_output = await _ask_async(ideator, agent_prompt, "ideator_generation")
        validator_prompt = f"Verify, polish, and optimize this generated campaign payload structure:\n{ideator_output}"
        validator_output = await _ask_async(validator, validator_prompt, "validator_compliance")

        # Step 2.2: Extract and structure the raw generation down into a structured matrix output
        final_json_payload = extract_json_payload(validator_output)
        log_event("workflow_complete", {"final_payload": final_json_payload})
        return final_json_payload
    except Exception as e:
        log_event("workflow_error", {"detail": str(e)})
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # Local runtime initialization gateway
    uvicorn.run("agent_service:app", host="127.0.0.1", port=8000, reload=True)
