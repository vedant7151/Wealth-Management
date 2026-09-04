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

# Load environment variables
load_dotenv()

# Initialize API clients
gemini_client = genai.Client()
tavily_client = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY", "dummy"))

def generate_content_with_retry(prompt, config=None, retries=3):
    for i in range(retries):
        try:
            return gemini_client.models.generate_content(
                model='gemini-3.6-flash',
                contents=prompt,
                config=config
            )
        except Exception as e:
            if '503' in str(e) and i < retries - 1:
                print(f"[Warn] 503 Unavailable. Retrying in {2 ** i}s...")
                time.sleep(2 ** i)
            else:
                raise

class GraphState(TypedDict):
    client_id: str
    portfolio_themes: List[str]
    queries_to_run: List[str]
    gathered_news: str
    iteration_count: int
    final_briefing: str

def plan_search(state: GraphState) -> dict:
    print("Node: plan_search")
    themes = state.get("portfolio_themes", [])
    prompt = f"Based on the following portfolio themes: {', '.join(themes)}, generate 2-3 highly targeted search queries to fetch real-time financial news. Respond ONLY with a valid JSON array of strings."
    
    response = generate_content_with_retry(
        prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    
    try:
        queries = json.loads(response.text)
    except Exception:
        # Fallback if invalid json
        queries = [f"{theme} financial news" for theme in themes][:2]
        
    return {"queries_to_run": queries}

def execute_search(state: GraphState) -> dict:
    print("Node: execute_search")
    queries = state.get("queries_to_run", [])
    gathered_news = state.get("gathered_news", "")
    iteration_count = state.get("iteration_count", 0)
    
    new_results = []
    for q in queries:
        try:
            res = tavily_client.search(query=q, search_depth="basic")
            new_results.append(json.dumps(res))
        except Exception as e:
            new_results.append(f"Search error for {q}: {str(e)}")
            
    updated_news = gathered_news + "\n" + "\n".join(new_results)
    
    return {
        "gathered_news": updated_news,
        "iteration_count": iteration_count + 1
    }

def evaluate_and_route(state: GraphState) -> str:
    print("Node: evaluate_and_route")
    iteration_count = state.get("iteration_count", 0)
    if iteration_count >= 2:
        return "draft"
        
    themes = state.get("portfolio_themes", [])
    news = state.get("gathered_news", "")
    
    prompt = f"""
    You are evaluating financial news data against portfolio themes: {', '.join(themes)}.
    Data:
    {news[:2000]}  # limit context for evaluation
    
    Does this data have sufficient financial depth and relevance to the themes? 
    Reply ONLY with 'yes' or 'no'.
    """
    
    response = generate_content_with_retry(prompt)
    
    answer = response.text.strip().lower()
    if 'yes' in answer:
        return "draft"
    return "search"

def draft_briefing(state: GraphState) -> dict:
    print("Node: draft_briefing")
    news = state.get("gathered_news", "")
    
    instruction = """
    You are a financial analyst drafting an SEC-compliant morning briefing. 
    You MUST format the data into a Markdown briefing with a 'Macro View' and 'Portfolio Impact' section.
    
    CRITICAL RESTRICTION 1: You must ONLY use facts present in the provided gathered news. No hallucinations.
    
    CRITICAL RESTRICTION 2: Never make definitive buy, sell, or hold recommendations. Phrase all impacts strictly as potential risks or potential tailwinds based on historical correlations.
    """
    
    prompt = f"Instruction: {instruction}\n\nGathered News:\n{news}"
    
    response = generate_content_with_retry(prompt)
    
    return {"final_briefing": response.text}

def save_briefing(state: GraphState) -> dict:
    print("Node: save_briefing")
    client_id = state.get("client_id")
    briefing = state.get("final_briefing")
    
    if client_id and briefing:
        db = Prisma()
        db.connect()
        try:
            db.briefing.create(
                data={
                    "content": briefing,
                    "clientProfileId": client_id
                }
            )
            print("Successfully saved briefing to database.")
        except Exception as e:
            print(f"Error saving briefing to database: {str(e)}")
        finally:
            db.disconnect()
            
    return {}

# Build StateGraph
workflow = StateGraph(GraphState)

workflow.add_node("plan_search", plan_search)
workflow.add_node("execute_search", execute_search)
workflow.add_node("draft_briefing", draft_briefing)
workflow.add_node("save_briefing", save_briefing)

workflow.add_edge(START, "plan_search")
workflow.add_edge("plan_search", "execute_search")

workflow.add_conditional_edges(
    "execute_search",
    evaluate_and_route,
    {
        "search": "plan_search",
        "draft": "draft_briefing"
    }
)

workflow.add_edge("draft_briefing", "save_briefing")
workflow.add_edge("save_briefing", END)

app = workflow.compile()

if __name__ == "__main__":
    print("--- Starting Local Execution Block (Mock Data) ---")
    mock_input = {
        "client_id": "client_1",
        "portfolio_themes": ["AI infrastructure", "Semiconductors"],
        "queries_to_run": [],
        "gathered_news": "",
        "iteration_count": 0,
        "final_briefing": ""
    }
    
    final_state = None
    for output in app.stream(mock_input):
        for key, value in output.items():
            print(f"Finished node: {key}")
            final_state = value
            
    # Final Result
    if final_state and "final_briefing" in final_state:
        print("\n\n--- Final Morning Briefing ---")
        print(final_state["final_briefing"])
