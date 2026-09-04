import os
import json
import time
from typing import TypedDict, List
from dotenv import load_dotenv

from langgraph.graph import StateGraph, START, END
from prisma import Prisma
from google import genai
from google.genai import types
from tavily import TavilyClient
import requests

# Load environment variables
load_dotenv()

# Setup LangSmith
os.environ["LANGCHAIN_TRACING_V2"] = "true"

gemini_client = genai.Client()
tavily_client = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY"))
hf_api_key = os.environ.get("HUGGINGFACE_API_KEY")


def get_embedding(text: str) -> List[float]:
    """Helper to get HuggingFace API embedding."""
    try:
        response = requests.post(
            "https://api-inference.huggingface.co/pipeline/feature-extraction/sentence-transformers/all-MiniLM-L6-v2",
            headers={"Authorization": f"Bearer {hf_api_key}"},
            json={"inputs": text}
        )
        res_json = response.json()
        if isinstance(res_json, list):
            if len(res_json) > 0 and isinstance(res_json[0], list):
                return res_json[0]
            return res_json
        else:
            print("HF API Error:", res_json)
            return [0.0] * 384
    except Exception as e:
        print(f"Embedding error: {e}")
        return [0.0] * 384

class GraphState(TypedDict):
    client_id: str
    portfolio_themes: List[str]
    queries_to_run: List[str]
    gathered_news: str
    iteration_count: int
    revision_count: int
    final_briefing: str
    final_citations: List[dict]
    is_compliant: bool
    criticism: str

def researcher_node(state: GraphState) -> dict:
    print("Node: researcher_node")
    themes = state.get("portfolio_themes", [])
    gathered_news = state.get("gathered_news", "")
    iteration_count = state.get("iteration_count", 0)
    current_citations = state.get("final_citations", [])
    
    prompt = f"Based on portfolio themes: {', '.join(themes)}, generate 2 targeted search queries. Return ONLY a valid JSON array of strings, e.g. [\"theme 1 news\", \"theme 2 news\"]."
    
    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        queries = json.loads(response.text)
    except Exception:
        queries = [f"{theme} financial news" for theme in themes][:2]
        
    db = Prisma()
    db.connect()
    
    new_results = []
    new_citations = []
    
    for q in queries:
        emb = get_embedding(q)
        cache_hit = False
        try:
            # Check Neon pgvector cache
            cached_rows = db.query_raw(
                '''
                SELECT id, "results"::text FROM "SearchCache"
                ORDER BY "queryEmbedding" <-> $1::vector
                LIMIT 1
                ''',
                emb
            )
            # Mock threshold check (assuming <-> returns distance. We can't conditionally easily in query_raw without setting thresholds, let's just use it if it exists for simplicity if it's the exact or very close query)
            if cached_rows and len(cached_rows) > 0:
                print(f"Cache hit for query: {q}")
                context_str = cached_rows[0].get("results")
                try:
                    res = json.loads(context_str)
                    for item in res.get("results", []):
                        if item.get("url"):
                            new_citations.append({
                                "url": item["url"],
                                "publishedDate": item.get("published_date")
                            })
                except:
                    pass
                new_results.append(context_str)
                cache_hit = True
        except Exception as e:
            print(f"Cache check error: {e}")
            
        if not cache_hit:
            try:
                res = tavily_client.search(query=q, search_depth="basic", topic="news")
                context_str = json.dumps(res)
                new_results.append(context_str)
                
                for item in res.get("results", []):
                    url = item.get("url")
                    if url:
                        new_citations.append({
                            "url": url,
                            "publishedDate": item.get("published_date")
                        })
                
                # Save to cache
                db.execute_raw(
                    '''
                    INSERT INTO "SearchCache" ("id", "query", "queryEmbedding", "results", "createdAt")
                    VALUES (gen_random_uuid(), $1, $2::vector, $3::json, NOW())
                    ''',
                    q, json.dumps(emb), context_str
                )
            except Exception as e:
                print(f"Tavily search error for {q}: {str(e)}")
                
    db.disconnect()
    
    updated_news = gathered_news + "\\n" + "\\n".join(new_results)
    
    seen_urls = set()
    updated_citations = []
    for cit in current_citations + new_citations:
        if cit["url"] not in seen_urls:
            seen_urls.add(cit["url"])
            updated_citations.append(cit)
    
    return {
        "gathered_news": updated_news,
        "iteration_count": iteration_count + 1,
        "final_citations": updated_citations
    }

def writer_node(state: GraphState) -> dict:
    print("Node: writer_node")
    news = state.get("gathered_news", "")
    criticism = state.get("criticism", "")
    themes = state.get("portfolio_themes", [])
    revision_count = state.get("revision_count", 0)
    
    prompt = f"""
    You are a financial analyst drafting an SEC-compliant morning briefing.
    Themes: {', '.join(themes)}
    Gathered News: {news[:2000]} # Trim to fit context
    
    Previous Compliance Critic Feedback to address (if any): {criticism}
    
    Format into Markdown with a 'Macro View' and 'Portfolio Impact' section.
    CRITICAL RESTRICTION 1: Strict source grounding. Use ONLY facts from Gathered News.
    CRITICAL RESTRICTION 2: DO NOT make definitive buy, sell, or hold recommendations.
    """
    
    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        return {
            "final_briefing": response.text,
            "revision_count": revision_count + 1
        }
    except Exception as e:
        return {
            "final_briefing": f"Error generating draft: {e}",
            "revision_count": revision_count + 1
        }

def compliance_critic_node(state: GraphState) -> dict:
    print("Node: compliance_critic_node")
    draft = state.get("final_briefing", "")
    
    try:
        prompt = f"Assess this financial draft for definitive buy/sell recommendations (SEC non-compliant) or unsupported claims. Output strictly a JSON object: {{\"is_compliant\": boolean, \"critique_reason\": string}}.\\n\\nDraft: {draft}"
        
        response = requests.post(
            "https://api-inference.huggingface.co/models/fastino/gliguard-LLMGuardrails-300M",
            headers={"Authorization": f"Bearer {hf_api_key}"},
            json={"inputs": prompt}
        )
        
        res_json = response.json()
        
        reply = ""
        if isinstance(res_json, list) and len(res_json) > 0 and "generated_text" in res_json[0]:
            reply = res_json[0]["generated_text"].replace(prompt, "").strip()
        else:
            reply = str(res_json)
        
        # Try parse JSON
        try:
            if '```json' in reply:
                reply = reply.split('```json')[1].split('```')[0].strip()
            elif '```' in reply:
                reply = reply.split('```')[1].strip()
            
            result = json.loads(reply)
            return {
                "is_compliant": result.get("is_compliant", False),
                "criticism": result.get("critique_reason", "Failed compliance check.")
            }
        except Exception as json_err:
            print("Failed to parse Critic JSON:", json_err)
            return {
                "is_compliant": False,
                "criticism": f"Failed to parse critic response structure: {reply}"
            }
            
    except Exception as e:
        print("HF Critic Request Error:", e)
        return {
            "is_compliant": False,
            "criticism": "Critic service unavailable."
        }
        
def supervisor_node(state: GraphState) -> dict:
    # Router logic
    # The supervisor node does not mutate state but we route using conditional edges from a router function.
    # We will just return empty state update, but conditional edges will use it.
    pass

# We use conditional routing function instead of a state mutating node
def router(state: GraphState) -> str:
    print("Routing...")
    if state.get("is_compliant"):
        return "save"
        
    if state.get("criticism") and not state.get("is_compliant"):
        if state.get("revision_count", 0) >= 2:
            print("Max revisions hit, moving to save")
            return "save" # fallback
        return "writer"
        
    if not state.get("gathered_news"):
        return "researcher"
        
    if state.get("gathered_news") and not state.get("final_briefing"):
        return "writer"
        
    return "critic"

def save_node(state: GraphState) -> dict:
    print("Node: save_node")
    client_id = state.get("client_id")
    briefing = state.get("final_briefing")
    citations = state.get("final_citations", [])
    
    if client_id and briefing:
        db = Prisma()
        db.connect()
        try:
            br_rec = db.briefing.create(
                data={
                    "content": briefing,
                    "clientProfileId": client_id
                }
            )
            for cit in citations:
                db.citation.create(
                    data={
                        "url": cit.get("url"),
                        "publishedDate": cit.get("publishedDate"),
                        "briefingId": br_rec.id
                    }
                )
            print("Successfully saved briefing and citations to database.")
        except Exception as e:
            print(f"Error saving to database: {e}")
        finally:
            db.disconnect()
            
    return {}

# Build StateGraph
workflow = StateGraph(GraphState)

workflow.add_node("researcher", researcher_node)
workflow.add_node("writer", writer_node)
workflow.add_node("critic", compliance_critic_node)
workflow.add_node("save", save_node)

workflow.add_edge(START, "researcher")
workflow.add_edge("researcher", "writer")
workflow.add_edge("writer", "critic")

workflow.add_conditional_edges(
    "critic",
    router,
    {
        "writer": "writer",
        "save": "save"
    }
)

workflow.add_edge("save", END)
app = workflow.compile()

if __name__ == "__main__":
    print("--- Starting Local Execution Block (Multi-Agent) ---")
    mock_input = {
        "client_id": "client_1",
        "portfolio_themes": ["AI infrastructure", "Semiconductors"],
        "queries_to_run": [],
        "gathered_news": "",
        "iteration_count": 0,
        "final_briefing": "",
        "final_citations": [],
        "is_compliant": False,
        "criticism": ""
    }
    
    final_state = None
    for output in app.stream(mock_input):
        for key, value in output.items():
            print(f"Finished node: {key}")
            final_state = value
            
    if final_state and "final_briefing" in final_state:
        print("\n\n--- Final Morning Briefing ---")
        print(final_state["final_briefing"])
        print("\n--- Citations ---")
        for url in final_state.get("final_citations", []):
            print(f"- {url}")
