"""How to route each Foundry SDK through Sentinel.

The only change in your app: replace the Azure endpoint host with the Sentinel
URL. Keys / Entra ID tokens are forwarded unchanged. Optionally add the
`x-sentinel-app` header so the dashboard groups traffic per app/agent.
"""
import os

SENTINEL = os.environ.get("SENTINEL_URL", "http://localhost:8080")
TAG = {"x-sentinel-app": "support-bot"}

# 1) Azure OpenAI chat completions -------------------------------------------
from openai import AzureOpenAI

client = AzureOpenAI(
    azure_endpoint=SENTINEL,                 # was https://<resource>.openai.azure.com
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-10-21",
    default_headers=TAG,
)
r = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
print(r.choices[0].message.content)

# 2) Responses API (OpenAI v1 surface on Azure) --------------------------------
from openai import OpenAI

v1 = OpenAI(base_url=f"{SENTINEL}/openai/v1/", api_key=os.environ["AZURE_OPENAI_API_KEY"], default_headers=TAG)
print(v1.responses.create(model="gpt-4.1", instructions="Be brief.", input="hello").output_text)

# 3) Azure AI Inference ---------------------------------------------------------
from azure.ai.inference import ChatCompletionsClient
from azure.core.credentials import AzureKeyCredential

inf = ChatCompletionsClient(endpoint=f"{SENTINEL}/models", credential=AzureKeyCredential(os.environ["AZURE_AI_KEY"]),
                            headers=TAG)
print(inf.complete(model="gpt-4o", messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)

# 4) Foundry Agent Service ------------------------------------------------------
# Project endpoint normally: https://<resource>.services.ai.azure.com/api/projects/<project>
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

# (to tag agent traffic, pass headers=TAG per call or add an azure-core HeadersPolicy)
agents = AgentsClient(endpoint=f"{SENTINEL}/api/projects/<project>", credential=DefaultAzureCredential())
agent = agents.create_agent(model="gpt-4o", name="hr", instructions="You are the HR policy agent.")
thread = agents.threads.create()
agents.messages.create(thread_id=thread.id, role="user", content="How many leave days do I get?")
# Prefer additional_instructions over instructions: `instructions` REPLACES the agent prompt (Sentinel rule R006)
run = agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id,
                                     additional_instructions="User is in the India office.")

# 5) Lint a prompt in CI without calling Azure ------------------------------------
import httpx

report = httpx.post(f"{SENTINEL}/sentinel/api/analyze", json={
    "path": "/openai/deployments/gpt-4o/chat/completions",
    "body": {"messages": [{"role": "system", "content": open("prompts/system.md").read()},
                          {"role": "user", "content": "test"}]},
}).json()
assert report["health_score"] >= 80, report["findings"]
